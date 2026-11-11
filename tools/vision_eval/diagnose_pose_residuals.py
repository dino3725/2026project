"""Run saved-data pose diagnostics and write review artifacts without robot access."""

import argparse
import csv
from datetime import datetime
import hashlib
import json
from pathlib import Path

import cv2
import numpy as np
from scipy.spatial.transform import Rotation

from handeye import transform
from intrinsics_observation import extract
from pose_diagnostics import (ArmChain, diagnostic_decision, evaluate_geometry, fit_geometry,
                              initial_vector, JOINT_NAMES, sensitivity)


def sha(path):
    """Fingerprint exact inputs and implementations."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_observations(roots, xml, reference):
    """Read source-verified observations and re-detect pixels without camera K/D."""
    data, audit, hashes = [], [], {}
    chain = ArmChain(xml)
    expected_model = hashlib.sha256(xml.encode()).hexdigest()
    for index, root in enumerate(roots):
        previous = reference['comparisons'][index]
        for path, expected in previous['inputs_sha256'].items():
            if sha(Path(path)) != expected:
                raise ValueError('Previously analyzed input changed: ' + path)
        paths = sorted(root.glob('train_*/observation.json'))
        paths += sorted(root.glob('validation_*/observation.json'))
        if [p.parent.name for p in paths] != previous['view_names']:
            raise ValueError('Observation set differs from reviewed session')
        for path in paths:
            record = json.loads(path.read_text())
            if record['session_id'] != previous['session_id']:
                raise ValueError('Session order differs from reviewed comparison')
            if record['role'] != path.parent.name.split('_')[0]:
                raise ValueError('Observation role differs from directory role')
            if record['robot_description_sha256'] != expected_model:
                raise ValueError('URDF does not match captured robot description')
            q = np.array([record['joints_latest'][name] for name in JOINT_NAMES])
            model_pose = chain.forward(q[None])[0]
            image_pose = np.asarray(record['T_base_ee'])
            image_path = path.parent / 'rgb.png'
            if sha(image_path) != record['image_sha256']:
                raise ValueError('Original RGB hash mismatch')
            found = extract(cv2.imread(str(image_path)), 177, 125)
            data.append({'name': f'{index + 1}/{path.parent.name}', 'session_index': index,
                         'role': record['role'], 'q': q,
                         'objects': np.array(found['object_points_m'], dtype=float),
                         'pixels': np.array(found['corners_px'], dtype=float),
                         'ids': np.array(found['corner_ids']), 'record': record})
            audit.append({'name': data[-1]['name'],
                          'fk_tf_position_difference_mm': float(np.linalg.norm(
                              model_pose[:3, 3] - image_pose[:3, 3]) * 1000),
                          'fk_tf_rotation_difference_deg': float(Rotation.from_matrix(
                              model_pose[:3, :3].T @ image_pose[:3, :3]).magnitude()
                              * 180 / np.pi),
                          'rgb_depth_delta_ms': record['pair_delta_ms']})
            hashes[str(path)] = sha(path)
            hashes[str(image_path)] = sha(image_path)
    first = data[0]['record']
    for row in data:
        if (row['record']['color_info'] != first['color_info']
                or row['record']['T_camera_optical'] != first['T_camera_optical']):
            raise ValueError('Mixed camera settings or optical transform')
    return data, audit, hashes


def summarize(rows):
    """Keep train and reused-validation error summaries separate."""
    result = {}
    for role in ['train', 'validation']:
        subset = [r for r in rows if r['role'] == role]
        result[role] = {
            'views': len(subset),
            'view_pixel_rms_mean': float(np.mean([r['pixel_rms_px'] for r in subset])),
            'max_position_error_mm': max(r['position_error_mm'] for r in subset),
            'mean_position_error_mm': float(np.mean([r['position_error_mm'] for r in subset])),
        }
    return result


def associations(rows):
    """Describe within-session associations, explicitly retaining confounding."""
    output = []
    for session in sorted({r['session_index'] for r in rows}):
        subset = [r for r in rows if r['session_index'] == session and r['role'] == 'train']
        q = np.array([r['q_deg'] for r in subset])
        errors = np.array([r['position_residual_mm'] for r in subset])
        correlations = []
        for i in range(4):
            correlations.append([
                float(np.corrcoef(q[:, i], errors[:, j])[0, 1])
                if np.std(q[:, i]) > 0.05 and np.std(errors[:, j]) > 1e-8 else None
                for j in range(3)])
        output.append({'session_index': session, 'joint_spans_deg': np.ptp(q, axis=0).tolist(),
                       'joint3_joint4_correlation': float(np.corrcoef(q[:, 2], q[:, 3])[0, 1]),
                       'joint_vs_xyz_residual_correlations': correlations,
                       'interpretation': 'Association only; joints, view angle '
                       'and image position co-vary.'})
    return output


def close_pose_pairs(data):
    """Compare repeated cross-session views before attributing change to hardware."""
    pairs = []
    for a in data:
        for b in data:
            if a['session_index'] >= b['session_index']:
                continue
            delta = np.max(np.abs(np.rad2deg(a['q'] - b['q'])))
            if delta > 0.35:
                continue
            common, ia, ib = np.intersect1d(a['ids'], b['ids'], return_indices=True)
            pairs.append({'first': a['name'], 'second': b['name'],
                          'max_joint_difference_deg': float(delta), 'common_corners': len(common),
                          'pixel_displacement_rms': float(np.sqrt(np.mean(np.sum(
                              (a['pixels'][ia] - b['pixels'][ib]) ** 2, axis=1))))})
    return pairs


def main():
    """Audit, fit diagnostic hypotheses, and keep all runtime settings untouched."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--sessions', type=Path, nargs=2, required=True)
    parser.add_argument('--model', type=Path, required=True)
    parser.add_argument('--intrinsics-session', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    cv2.setNumThreads(1)
    reference_path = args.intrinsics_session / 'handeye_intrinsics_comparison.json'
    reference = json.loads(reference_path.read_text())
    if (reference['printed_dimensions_mm'] != [177, 125]
            or reference['base_link_height_above_support_mm'] != 10):
        raise ValueError('This diagnostic expects the reviewed 177x125 mm board and 10 mm base')
    candidate_path = args.intrinsics_session / 'intrinsic_candidates.json'
    if sha(candidate_path) != reference['candidate_sha256']:
        raise ValueError('Intrinsic candidate changed')
    candidates = json.loads(candidate_path.read_text())
    xml = args.model.read_text()
    chain = ArmChain(xml)
    data, audit, hashes = load_observations(args.sessions, xml, reference)
    training = [r for r in data if r['role'] == 'train']
    report = {'created': datetime.now().astimezone().isoformat(), 'applied': False,
              'status': 'offline_diagnosis_not_a_physical_calibration',
              'model_sha256': sha(args.model), 'candidate_sha256': sha(candidate_path),
              'reference_sha256': sha(reference_path),
              'inputs_sha256': hashes, 'fk_tf_audit': audit,
              'cross_session_close_pose_pairs': close_pose_pairs(data), 'models': {},
              'validation_caveat': 'Previously inspected views reused; '
              'diagnostic, not new holdout.',
              'bias_caveat': 'Bounded +/-5 degree hypotheses, not motor zero corrections.',
              'physical_frame_convention': 'base_link is 10 mm above wheel support plane.'}
    for name, intrinsics in [('factory', candidates['factory']),
                             ('radial2', candidates['models']['radial2'])]:
        initial_methods = [s['comparisons']['neutral_redetection'][name]['methods']['reprojection']
                           for s in reference['comparisons']]
        mounts = np.array([m['T_ee_optical'] for m in initial_methods])
        mount = transform(Rotation.from_matrix(mounts[:, :3, :3]).mean().as_matrix(),
                          mounts[:, :3, 3].mean(axis=0))
        boards = [np.array(m['T_base_board']) for m in initial_methods]
        k, d = np.array(intrinsics['k']), np.array(intrinsics['d'])
        base = fit_geometry(training, chain, k, d, mount, boards)
        report['models'][name] = {'baseline_shared_mount': base}
        for label, selected, start in [('bias23_zero_start', (1, 2), 0),
                                       ('bias23_positive_start', (1, 2), 2),
                                       ('bias23_negative_start', (1, 2), -2)]:
            print(f'Fitting {name} {label}', flush=True)
            fitted = fit_geometry(training, chain, k, d, unpack_mount(base),
                                  unpack_boards(base), selected, start)
            report['models'][name][label] = fitted
        for label, fit in report['models'][name].items():
            p = np.array(fit['parameters'])
            selected = tuple(fit['selected_joint_indices'])
            rows = evaluate_geometry(p, data, chain, k, d, selected)
            fit['per_view'] = rows
            fit['summary'] = summarize(rows)
            fit['training_associations'] = associations(rows)
            fit['board_center_heights_mm'] = [float((
                b @ np.array([.0885, .0625, 0, 1]))[2] * 1000 + 10)
                for b in unpack_boards(fit)]
            print(name, label, json.dumps(fit['summary']),
                  'bias_deg', fit['bias_hypothesis_deg'], flush=True)
        full = initial_vector(unpack_mount(base), unpack_boards(base), (0, 1, 2, 3))
        report['models'][name]['all_joint_bias_observability'] = sensitivity(
            full, training, chain, k, d, (0, 1, 2, 3))
    report['implementation_sha256'] = {
        name: sha(Path(__file__).with_name(name))
        for name in ['pose_diagnostics.py', 'diagnose_pose_residuals.py', 'handeye.py',
                     'intrinsics_observation.py', 'refine_handeye.py']}
    (args.output / 'analysis.json').write_text(
        json.dumps(report, indent=2, allow_nan=False) + '\n')
    (args.output / 'decision.json').write_text(
        json.dumps(diagnostic_decision(report), indent=2, allow_nan=False) + '\n')
    rows = report['models']['factory']['baseline_shared_mount']['per_view']
    with (args.output / 'pose_residuals.csv').open('w') as stream:
        writer = csv.writer(stream)
        writer.writerow(['view', 'role', *JOINT_NAMES, 'dx_mm', 'dy_mm', 'dz_mm', 'pixel_rms'])
        for row in rows:
            writer.writerow([row['name'], row['role'], *row['q_deg'],
                             *row['position_residual_mm'], row['pixel_rms_px']])


def unpack_mount(fit):
    """Read the shared mount from a diagnostic fit."""
    from refine_handeye import unpack
    return unpack(np.asarray(fit['parameters'][:6]))


def unpack_boards(fit):
    """Read both session board poses from a diagnostic fit."""
    from refine_handeye import unpack
    return [unpack(np.asarray(fit['parameters'][6 + 6 * i:12 + 6 * i])) for i in range(2)]


if __name__ == '__main__':
    main()
