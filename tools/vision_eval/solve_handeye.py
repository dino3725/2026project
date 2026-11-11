"""Solve a candidate mount from training captures and check held-out captures."""

import argparse
import hashlib
import json
from pathlib import Path

import numpy as np
from scipy.spatial.transform import Rotation

from handeye import BOARD_SPEC, consistency, solve, validate_transform


def solve_records(records):
    """Require a common fixed-board session and separate pose validation."""
    if not records or any(r['status'] != 'accepted_observation_not_calibration' for r in records):
        raise ValueError('Only accepted physical observations may be used')
    first = records[0]
    if not first['session_id']:
        raise ValueError('Missing fixed-board session ID')
    for record in records:
        if (record['session_id'] != first['session_id'] or record['board'] != BOARD_SPEC
                or record['robot_description_sha256'] != first['robot_description_sha256']
                or record['color_info'] != first['color_info']
                or not np.allclose(record['T_camera_optical'], first['T_camera_optical'])
                or not np.allclose(record['T_base_link1'], first['T_base_link1'])):
            raise ValueError('Mixed board/session/intrinsics/robot model')
        if not np.allclose(validate_transform(record['T_base_link1'])[:3, 3],
                           [-0.092, 0, 0.116], atol=1e-6, rtol=0):
            raise ValueError('Measured arm model is required')
        if record.get('role') not in ['train', 'validation']:
            raise ValueError('Invalid capture role')
        if not np.isfinite(record['reprojection_rms_px']) or record['reprojection_rms_px'] > 1:
            raise ValueError('Unacceptable reprojection error')
    train = [r for r in records if r['role'] == 'train']
    held = [r for r in records if r['role'] == 'validation']
    if len(held) < 3:
        raise ValueError('Need at least 3 held-out validation poses')
    ee = [validate_transform(r['T_base_ee']) for r in train]
    targets = [validate_transform(r['T_optical_board']) for r in train]
    # Repeated frames of a pose do not constitute independent calibration views.
    all_ee = [validate_transform(r['T_base_ee']) for r in records]
    for i, a in enumerate(all_ee):
        for b in all_ee[i + 1:]:
            if (np.linalg.norm(a[:3, 3] - b[:3, 3]) < 0.005
                    and Rotation.from_matrix(a[:3, :3].T @ b[:3, :3]).magnitude()
                    < np.deg2rad(2)):
                raise ValueError('Repeated or near-identical poses; collect independent views')
    mount, singular = solve(ee, targets)
    training, reference = consistency(ee, targets, mount)
    validation, _ = consistency([r['T_base_ee'] for r in held],
                                [r['T_optical_board'] for r in held], mount, reference)
    camera_mount = mount @ np.linalg.inv(validate_transform(first['T_camera_optical']))
    passed = all(s['max_translation_mm'] <= 5 and s['max_rotation_deg'] <= 1
                 for s in [training, validation])
    return {
        'status': 'candidate_requires_physical_review' if passed else 'rejected_candidate',
        'applied': False, 'session_id': first['session_id'],
        'T_ee_optical': mount.tolist(), 'T_ee_camera': camera_mount.tolist(),
        'camera_mount_xyz_m': camera_mount[:3, 3].tolist(),
        'camera_mount_rpy_rad': Rotation.from_matrix(
            camera_mount[:3, :3]).as_euler('xyz').tolist(),
        'training': training, 'held_out': validation, 'rotation_singular_values': singular,
        'provisional_screening_limits': {'max_translation_mm': 5, 'max_rotation_deg': 1},
        'limits_note': ('Internal consistency screening, '
                        'not proof of absolute robot or depth accuracy.'),
    }


def main():
    """Write a candidate report only; do not modify TF or camera calibration."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('observations', nargs='+', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    records = []
    for path in args.observations:
        record = json.loads(path.read_text())
        image = path.parent / 'rgb.png'
        if hashlib.sha256(image.read_bytes()).hexdigest() != record['image_sha256']:
            raise ValueError(f'Image hash mismatch: {path}')
        records.append(record)
    result = solve_records(records)
    result['input_sha256'] = {str(p): hashlib.sha256(p.read_bytes()).hexdigest()
                              for p in args.observations}
    with args.output.open('x') as stream:
        stream.write(json.dumps(result, indent=2) + '\n')
    print(result['status'])


if __name__ == '__main__':
    main()
