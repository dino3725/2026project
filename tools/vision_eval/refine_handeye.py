"""Refine a hand-eye candidate using training image corners only, without ROS."""

import argparse
import hashlib
import json
from pathlib import Path

import cv2
import numpy as np
from scipy.optimize import least_squares
from scipy.spatial.transform import Rotation

from handeye import board, transform, validate_transform


def pack(matrix):
    """Represent a rigid transform with a rotation vector and translation."""
    matrix = validate_transform(matrix)
    return np.r_[Rotation.from_matrix(matrix[:3, :3]).as_rotvec(), matrix[:3, 3]]


def unpack(vector):
    """Recover a child-to-parent rigid transform from six parameters."""
    return transform(Rotation.from_rotvec(vector[:3]).as_matrix(), vector[3:6])


def refine(ee_poses, object_points, image_points, k, d, mount_initial, board_initial):
    """Jointly fit camera mount and fixed board pose to training reprojections."""
    if not (len(ee_poses) == len(object_points) == len(image_points) and len(ee_poses) >= 8):
        raise ValueError('Need at least eight paired training views')
    inverse_ee = [np.linalg.inv(validate_transform(e)) for e in ee_poses]

    def residual(parameters):
        inverse_mount = np.linalg.inv(unpack(parameters[:6]))
        world_board = unpack(parameters[6:])
        errors = []
        for e, obj, img in zip(inverse_ee, object_points, image_points):
            optical_board = inverse_mount @ e @ world_board
            predicted = cv2.projectPoints(
                obj, cv2.Rodrigues(optical_board[:3, :3])[0], optical_board[:3, 3], k, d)[0]
            errors.append((predicted.reshape(-1, 2) - img.reshape(-1, 2)).ravel())
        return np.concatenate(errors)

    initial = np.r_[pack(mount_initial), pack(board_initial)]
    fit = least_squares(residual, initial, x_scale='jac', max_nfev=400,
                        ftol=1e-10, xtol=1e-10, gtol=1e-10)
    if not fit.success or not np.isfinite(fit.x).all():
        raise ValueError(f'Reprojection refinement failed: {fit.message}')
    mount, fixed_board = unpack(fit.x[:6]), unpack(fit.x[6:])
    for e, obj in zip(inverse_ee, object_points):
        ob = np.linalg.inv(mount) @ e @ fixed_board
        if np.min((ob[:3, :3] @ obj.T + ob[:3, 3, None])[2]) <= 0:
            raise ValueError('Refined board is behind camera')
    return mount, fixed_board, {
        'initial_corner_rms_px': float(np.sqrt(np.mean(residual(initial) ** 2) * 2)),
        'refined_corner_rms_px': float(np.sqrt(np.mean(fit.fun ** 2) * 2)),
        'iterations_nfev': int(fit.nfev),
        'jacobian_singular_values': np.linalg.svd(fit.jac, compute_uv=False).tolist(),
        'objective': 'All training pixel residuals, unweighted; no plane-height constraint.',
    }


def main():
    """Save an offline diagnostic candidate; never replace a live calibration."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--training-frozen', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    frozen = json.loads(args.training_frozen.read_text())
    records = []
    for filename, digest in frozen['training_inputs_sha256'].items():
        path = Path(filename)
        if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise ValueError('Training input changed after freezing')
        record = json.loads(path.read_text())
        if record['role'] != 'train':
            raise ValueError('Held-out capture cannot enter refinement')
        records.append(record)
    first = records[0]
    for record in records:
        if (record['color_info'] != first['color_info']
                or record['robot_description_sha256'] != first['robot_description_sha256']
                or record['session_id'] != first['session_id']):
            raise ValueError('Mixed camera/model/session')
    target = board()
    mount, fixed, metrics = refine(
        [r['T_base_ee'] for r in records],
        [target.chessboardCorners[np.asarray(r['corner_ids'])].astype(float) for r in records],
        [np.asarray(r['corners_px']) for r in records],
        np.asarray(first['color_info']['k']).reshape(3, 3), np.asarray(first['color_info']['d']),
        frozen['T_ee_optical'], frozen['training_fixed_board'])
    camera = mount @ np.linalg.inv(first['T_camera_optical'])
    center = fixed @ np.array([.0875, .0625, 0, 1])
    result = {
        'status': 'offline_refinement_diagnostic_not_applied', 'applied': False,
        'T_ee_optical': mount.tolist(), 'T_ee_camera': camera.tolist(),
        'T_base_board': fixed.tolist(), 'metrics': metrics,
        'camera_xyz_mm': (camera[:3, 3] * 1000).tolist(),
        'camera_rpy_deg': Rotation.from_matrix(
            camera[:3, :3]).as_euler('xyz', degrees=True).tolist(),
        'board_center_above_support_mm': float((center[2] + .01) * 1000),
        'training_inputs_sha256': frozen['training_inputs_sha256'],
        'validation_note': ('Existing held-out results were inspected; '
                            'fresh views are needed for final validation.'),
    }
    with args.output.open('x') as stream:
        stream.write(json.dumps(result, indent=2) + '\n')
    print(json.dumps({k: result[k] for k in [
        'status', 'camera_xyz_mm', 'camera_rpy_deg', 'board_center_above_support_mm', 'metrics',
    ]}, indent=2))


if __name__ == '__main__':
    main()
