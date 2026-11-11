"""Re-evaluate saved corner observations with independently measured print dimensions."""

import argparse
import hashlib
import json
from pathlib import Path

import cv2
import numpy as np
from scipy.spatial.transform import Rotation

from handeye import board, consistency, solve, transform
from refine_handeye import refine


def measured_points(ids, width_mm, height_mm):
    """Scale board metric coordinates by measured width and height separately."""
    if not (150 <= width_mm <= 200 and 100 <= height_mm <= 150):
        raise ValueError('Printed dimensions outside the expected A4 target range')
    points = board().chessboardCorners[np.asarray(ids)].astype(float)
    points[:, 0] *= width_mm / 175
    points[:, 1] *= height_mm / 125
    return points


def recalculate(record, width_mm, height_mm):
    """Keep pixel observations unchanged and recompute PnP from corrected metric points."""
    obj = measured_points(record['corner_ids'], width_mm, height_mm)
    img = np.asarray(record['corners_px'], dtype=float)
    k = np.asarray(record['color_info']['k']).reshape(3, 3)
    d = np.asarray(record['color_info']['d'])
    ok, rvec, tvec = cv2.solvePnP(obj, img, k, d)
    if not ok:
        raise ValueError('Metric PnP failed')
    rotation = cv2.Rodrigues(rvec)[0]
    if np.min((rotation @ obj.T + tvec)[2]) <= 0:
        raise ValueError('Metric board behind camera')
    projected = cv2.projectPoints(obj, rvec, tvec, k, d)[0].reshape(-1, 2)
    rms = float(np.sqrt(np.mean(np.sum((projected - img) ** 2, axis=1))))
    if rms > 1 or not np.isfinite(rms):
        raise ValueError('Metric reprojection error above existing screening limit')
    return transform(rotation, tvec), obj, rms


def evaluate_session(root, width_mm, height_mm):
    """Fit only training poses; reuse saved validation poses as a diagnostic check."""
    paths = sorted(root.glob('train_*/observation.json'))
    held_paths = sorted(root.glob('validation_*/observation.json'))
    train = [json.loads(p.read_text()) for p in paths]
    held = [json.loads(p.read_text()) for p in held_paths]
    if len(train) < 8 or len(held) < 3:
        raise ValueError('Incomplete training/validation dataset')
    for r in train + held:
        if (r['color_info'] != train[0]['color_info']
                or r['robot_description_sha256'] != train[0]['robot_description_sha256']
                or r['session_id'] != train[0]['session_id']):
            raise ValueError('Mixed camera, model or session')
    corrected = [recalculate(r, width_mm, height_mm) for r in train + held]
    targets = [v[0] for v in corrected[:len(train)]]
    held_targets = [v[0] for v in corrected[len(train):]]
    ee = [r['T_base_ee'] for r in train]
    held_ee = [r['T_base_ee'] for r in held]
    mount, singular = solve(ee, targets)
    training, reference = consistency(ee, targets, mount)
    refined, fixed_board, refinement = refine(
        ee, [v[1] for v in corrected[:len(train)]],
        [np.asarray(r['corners_px']) for r in train],
        np.asarray(train[0]['color_info']['k']).reshape(3, 3), np.asarray(train[0]['color_info']['d']),
        mount, reference)
    methods = {}
    for name, x, ref in [('park', mount, reference), ('reprojection', refined, fixed_board)]:
        train_stats, _ = consistency(ee, targets, x, ref)
        held_stats, _ = consistency(held_ee, held_targets, x, ref)
        camera = x @ np.linalg.inv(train[0]['T_camera_optical'])
        center = ref @ np.array([width_mm / 2000, height_mm / 2000, 0, 1])
        methods[name] = {
            'T_ee_optical': x.tolist(), 'T_ee_camera': camera.tolist(),
            'T_base_board': ref.tolist(), 'camera_xyz_mm': (camera[:3, 3] * 1000).tolist(),
            'camera_rpy_deg': Rotation.from_matrix(camera[:3, :3]).as_euler('xyz', degrees=True).tolist(),
            'board_center_above_support_mm': float((center[2] + .01) * 1000),
            'training': train_stats, 'reused_validation': held_stats,
        }
    return {
        'session_id': train[0]['session_id'], 'status': 'metric_recomputation_not_applied',
        'printed_width_mm': width_mm, 'printed_height_mm': height_mm,
        'geometry_assumption': 'Uniform scale within each axis; ruler uncertainty not supplied.',
        'validation_note': 'Same held-out images reused after independent ruler evidence; no new captures.',
        'methods': methods, 'refinement': refinement, 'rotation_singular_values': singular,
        'per_pose_corrected': {
            p.parent.name: {'T_optical_board': v[0].tolist(), 'reprojection_rms_px': v[2]}
            for p, v in zip(paths + held_paths, corrected)
        },
        'inputs_sha256': {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths + held_paths},
    }


def main():
    """Write separate evidence, preserving all original captures and candidates."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--session', type=Path, required=True)
    parser.add_argument('--width-mm', type=float, required=True)
    parser.add_argument('--height-mm', type=float, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    result = evaluate_session(args.session, args.width_mm, args.height_mm)
    with args.output.open('x') as stream:
        stream.write(json.dumps(result, indent=2) + '\n')
    for name, method in result['methods'].items():
        print(name, 'XYZ mm', method['camera_xyz_mm'],
              'board height mm', method['board_center_above_support_mm'],
              'train/validation max mm', method['training']['max_translation_mm'],
              method['reused_validation']['max_translation_mm'])


if __name__ == '__main__':
    main()
