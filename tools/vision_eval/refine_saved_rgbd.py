"""Compare fixed-arm RGB/RGB-D candidates with grouped saved-data cross-validation."""

import argparse
from datetime import datetime
import hashlib
import json
from pathlib import Path
import tarfile

import cv2
import numpy as np
from scipy.optimize import least_squares
from scipy.spatial.transform import Rotation

from diagnose_pose_residuals import load_observations, sha
from handeye import transform
from refine_handeye import pack, unpack
from rgbd_refinement import (evaluate, extract_depth, fit_fixed_arm, grouped_folds,
                             initialize, objective, score, summarize)


def write_json(path, data):
    """Save each review artifact once, with finite JSON numbers."""
    with path.open('x') as stream:
        stream.write(json.dumps(data, indent=2, allow_nan=False) + '\n')


def verify_depth_backups(roots, backup_directory):
    """Compare depth PNG bytes to the already-preserved session archives."""
    hashes = {}
    for root in roots:
        archive = backup_directory / (root.name + '.tar.gz')
        hashes[str(archive)] = sha(archive)
        with tarfile.open(archive, 'r:gz') as tar:
            members = {m.name: m for m in tar.getmembers() if m.isfile()}
            for path in sorted(root.glob('*/depth.png')):
                suffix = root.name + '/' + str(path.relative_to(root))
                matches = [n for n in members if n.endswith(suffix)]
                if len(matches) != 1:
                    raise ValueError('No unique archived depth image: ' + str(path))
                archived_hash = hashlib.sha256(tar.extractfile(matches[0]).read()).hexdigest()
                if archived_hash != sha(path):
                    raise ValueError('Depth differs from preserved archive: ' + str(path))
                hashes[str(path)] = archived_hash
    return hashes


def mount_stability(full, folds):
    """Measure sensitivity to excluded pose groups, not a statistical confidence interval."""
    reference = unpack(full[:6])
    mounts = [unpack(f[:6]) for f in folds]
    return {'max_translation_difference_mm': max(float(np.linalg.norm(
        m[:3, 3] - reference[:3, 3]) * 1000) for m in mounts),
        'max_rotation_difference_deg': max(float(Rotation.from_matrix(
            reference[:3, :3].T @ m[:3, :3]).magnitude() * 180 / np.pi) for m in mounts)}


def main():
    """Freeze a comparison protocol, rank training CV, and then inspect reused validation."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--sessions', type=Path, nargs=2, required=True)
    parser.add_argument('--model', type=Path, required=True)
    parser.add_argument('--intrinsics-session', type=Path, required=True)
    parser.add_argument('--backups', type=Path, default=Path('log/backups'))
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    cv2.setNumThreads(1)
    reference_path = args.intrinsics_session / 'handeye_intrinsics_comparison.json'
    reference = json.loads(reference_path.read_text())
    if (reference['printed_dimensions_mm'] != [177, 125]
            or reference['base_link_height_above_support_mm'] != 10):
        raise ValueError('Expected reviewed 177x125 mm board and 10 mm base height')
    candidate_path = args.intrinsics_session / 'intrinsic_candidates.json'
    if sha(candidate_path) != reference['candidate_sha256']:
        raise ValueError('Intrinsic candidate changed')
    candidates = json.loads(candidate_path.read_text())
    data, audit, hashes = load_observations(args.sessions, args.model.read_text(), reference)
    hashes.update(verify_depth_backups(args.sessions, args.backups))
    depth_audit = []
    for row in data:
        record = row['record']
        if (record['depth_encoding'] != '16UC1' or record['pair_delta_ms'] > 20
                or record['aligned_depth_info'] != record['color_info']):
            raise ValueError('Unsupported aligned depth metadata')
        path = args.sessions[row['session_index']] / row['name'].split('/')[1] / 'depth.png'
        row['plane'], row['depth_corners'] = extract_depth(
            cv2.imread(str(path), cv2.IMREAD_UNCHANGED), row['pixels'],
            record['aligned_depth_info'])
        row['ee'] = np.array(record['T_base_ee'])
        row['inverse_ee'] = np.linalg.inv(row['ee'])
        depth_audit.append({'name': row['name'], **{
            k: v.tolist() if isinstance(v, np.ndarray) else v for k, v in row['plane'].items()}})
    training = [r for r in data if r['role'] == 'train']
    validation = [r for r in data if r['role'] == 'validation']
    folds = grouped_folds(training)
    specifications = [
        {'name': 'factory_rgb_l2', 'intrinsics': 'factory', 'loss': 'linear', 'depth_mm': None},
        {'name': 'factory_rgb_robust', 'intrinsics': 'factory',
         'loss': 'soft_l1', 'depth_mm': None},
        {'name': 'factory_rgbd_2mm', 'intrinsics': 'factory', 'loss': 'soft_l1', 'depth_mm': 2},
        {'name': 'factory_rgbd_5mm', 'intrinsics': 'factory', 'loss': 'soft_l1', 'depth_mm': 5},
        {'name': 'radial2_rgb_l2', 'intrinsics': 'radial2', 'loss': 'linear', 'depth_mm': None},
        {'name': 'radial2_rgb_robust', 'intrinsics': 'radial2',
         'loss': 'soft_l1', 'depth_mm': None},
    ]
    protocol = {'created': datetime.now().astimezone().isoformat(),
                'candidate_specifications': specifications,
                'cv_excluded_view_names': [[training[i]['name'] for i in f] for f in folds],
                'grouping': 'Connected poses within 3 degrees per joint, across both sessions.',
                'ranking': 'sqrt(mean((pixel_RMS^2 + (plane_RMS_mm/3)^2)/2)) on grouped CV.',
                'ranking_scope': 'Factory-compatible candidates only; radial2 RGB diagnostic.',
                'held_out_policy': 'Previously inspected six validation views reused AFTER '
                'selection; not a new independent holdout.',
                'fixed': ['arm kinematics and recorded image-time TF', 'joint zeros',
                          'depth scale', 'intrinsic candidates'],
                'free': ['shared camera mount (6)', 'one board pose per session (6+6)'],
                'no_height_constraint': True,
                'review_thresholds_not_accuracy_guarantees': {
                    'cv_max_corner_rms_mm': 5, 'validation_max_corner_rms_mm': 5,
                    'all_view_max_abs_depth_centroid_height_mm': 5,
                    'fold_mount_translation_mm': 5, 'fold_mount_rotation_deg': 1}}
    write_json(args.output / 'protocol.json', protocol)
    optical = np.array(data[0]['record']['T_camera_optical'])
    nominal = transform(np.eye(3), [-.065, .0195, .052]) @ optical
    results, parameters = {}, {}
    for spec in specifications:
        intr = candidates['factory'] if spec['intrinsics'] == 'factory' else (
            candidates['models']['radial2'])
        k, d = np.array(intr['k']), np.array(intr['d'])
        predicted, fold_parameters = [], []
        for index, excluded in enumerate(folds):
            included = [r for i, r in enumerate(training) if i not in excluded]
            fit = fit_fixed_arm(included, k, d, nominal, spec['loss'], spec['depth_mm'])
            predicted.extend(evaluate(fit, [training[i] for i in excluded], k, d))
            fold_parameters.append(fit)
            print(f'{spec["name"]}: fold {index + 1}/{len(folds)}', flush=True)
        fitted = fit_fixed_arm(training, k, d, nominal, spec['loss'], spec['depth_mm'])
        parameters[spec['name']] = fitted
        results[spec['name']] = {
            'specification': spec, 'cv_score': score(predicted),
            'cv_summary': summarize(predicted),
            'cv_per_view': predicted, 'parameters': fitted.tolist(),
            'fold_parameters': [f.tolist() for f in fold_parameters],
            'fold_stability': mount_stability(fitted, fold_parameters),
            'training': evaluate(fitted, training, k, d)}
    eligible = [s['name'] for s in specifications if s['intrinsics'] == 'factory']
    selected = min(eligible, key=lambda name: results[name]['cv_score'])
    write_json(args.output / 'selected_frozen.json', {
        'selected': selected, 'parameters': parameters[selected].tolist(),
        'selection': 'minimum grouped training CV score among factory-compatible methods',
        'protocol_sha256': sha(args.output / 'protocol.json'), 'applied': False})
    for spec in specifications:
        intr = candidates['factory'] if spec['intrinsics'] == 'factory' else (
            candidates['models']['radial2'])
        k, d = np.array(intr['k']), np.array(intr['d'])
        held = evaluate(parameters[spec['name']], validation, k, d)
        results[spec['name']]['validation'] = held
        results[spec['name']]['validation_summary'] = summarize(held)
    k, d = np.array(candidates['factory']['k']), np.array(candidates['factory']['d'])
    initial = initialize(training, k, d, nominal)
    nominal_board_fit = least_squares(
        lambda p: objective(np.r_[pack(nominal), p], training, k, d, None), initial[6:],
        x_scale='jac', max_nfev=300)
    if not nominal_board_fit.success:
        raise ValueError('Nominal-mount board fit did not converge')
    nominal_rows = evaluate(np.r_[pack(nominal), nominal_board_fit.x], data, k, d)
    chosen = results[selected]
    chosen_rows = chosen['training'] + chosen['validation']
    limits = protocol['review_thresholds_not_accuracy_guarantees']
    checks = {
        'grouped_cv_corner_consistency': chosen['cv_summary']['corner_3d_rms_mm']['max'] <= 5,
        'reused_validation_corner_consistency': (
            chosen['validation_summary']['corner_3d_rms_mm']['max'] <= 5),
        'approximate_support_plane_height': max(
            abs(r['depth_corner_centroid_height_mm']) for r in chosen_rows) <= 5,
        'fold_mount_translation': chosen['fold_stability']['max_translation_difference_mm'] <= 5,
        'fold_mount_rotation': chosen['fold_stability']['max_rotation_difference_deg'] <= 1}
    camera = unpack(parameters[selected][:6]) @ np.linalg.inv(optical)
    profile = {'status': 'offline_candidate_requires_independent_validation', 'applied': False,
               'model_sha256': sha(args.model),
               'selected_method': selected, 'review_checks': checks, 'review_thresholds': limits,
               'T_ee_optical': unpack(parameters[selected][:6]).tolist(),
               'T_ee_camera': camera.tolist(), 'camera_xyz_m': camera[:3, 3].tolist(),
               'camera_rpy_rad': Rotation.from_matrix(camera[:3, :3]).as_euler('xyz').tolist(),
               'intrinsics': candidates['factory'], 'joint_biases_applied': False,
               'runtime_export_enabled': False, 'absolute_accuracy_certified': False}
    report = {'protocol': protocol, 'selected': selected, 'results': results,
              'nominal_mount_per_view': nominal_rows, 'depth_audit': depth_audit,
              'fk_tf_audit': audit, 'input_sha256': hashes,
              'model_sha256': sha(args.model), 'candidate_sha256': sha(candidate_path),
              'reference_sha256': sha(reference_path),
              'implementation_sha256': {n: sha(Path(__file__).with_name(n)) for n in [
                  'rgbd_refinement.py', 'refine_saved_rgbd.py', 'diagnose_pose_residuals.py',
                  'pose_diagnostics.py', 'handeye.py', 'intrinsics_observation.py',
                  'refine_handeye.py']}}
    write_json(args.output / 'comparison.json', report)
    write_json(args.output / 'candidate_profile.json', profile)
    print(json.dumps({'selected': selected, 'cv': chosen['cv_summary'],
                      'validation': chosen['validation_summary'],
                      'review_checks': checks}, indent=2))


if __name__ == '__main__':
    main()
