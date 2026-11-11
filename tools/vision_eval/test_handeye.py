"""Exercise transform direction, degeneracy and independent validation."""

from copy import deepcopy
import xml.etree.ElementTree as ET

import cv2
import numpy as np
import pytest
from scipy.spatial.transform import Rotation

from handeye import BOARD_SPEC, board, detect, solve, transform, validate_transform
from prepare_measured_model import patch_mount
from solve_handeye import solve_records


def synthetic():
    """Construct consistent observations with known camera mount and fixed board."""
    rng = np.random.default_rng(42)
    mount = transform(Rotation.from_euler('xyz', [0.1, -0.15, 0.05]).as_matrix(),
                      [-0.065, 0.0195, 0.052])
    fixed_board = transform(
        Rotation.from_euler('xyz', [0.3, -0.2, 0.1]).as_matrix(), [0.3, 0, 0.08])
    camera_optical = transform(
        Rotation.from_euler('xyz', [-1.57, 0, -1.57]).as_matrix(), [0, 0.015, 0])
    result = []
    for i in range(15):
        # Two rotation axes, as available on the four-axis manipulator.
        yaw, pitch = rng.uniform(-0.5, 0.5, 2)
        ee = transform(Rotation.from_euler('zy', [yaw, pitch]).as_matrix(),
                       rng.uniform(-0.1, 0.1, 3))
        optical_board = np.linalg.inv(ee @ mount @ camera_optical) @ fixed_board
        result.append({
            'status': 'accepted_observation_not_calibration', 'session_id': 'synthetic',
            'role': 'train' if i < 12 else 'validation', 'board': BOARD_SPEC,
            'robot_description_sha256': 'synthetic-model-fingerprint',
            'color_info': {'k': [600, 0, 320, 0, 600, 240, 0, 0, 1]},
            'T_camera_optical': camera_optical.tolist(),
            'T_base_link1': transform(np.eye(3), [-0.092, 0, 0.116]).tolist(),
            'T_base_ee': ee.tolist(), 'T_optical_board': optical_board.tolist(),
            'reprojection_rms_px': 0.1,
        })
    return result, mount


def test_recovers_mount_and_converts_optical_frame():
    records, expected = synthetic()
    result = solve_records(records)
    assert result['status'] == 'candidate_requires_physical_review'
    assert result['applied'] is False
    assert np.allclose(result['T_ee_camera'], expected, atol=1e-8)
    assert result['held_out']['max_translation_mm'] < 1e-6


def test_validation_board_motion_is_rejected():
    records, _ = synthetic()
    records[-1]['T_optical_board'][0][3] += 0.03
    assert solve_records(records)['status'] == 'rejected_candidate'


def test_validation_is_not_used_to_fit():
    records, _ = synthetic()
    first = solve_records(records)
    records[-1]['T_optical_board'][0][3] += 0.03
    second = solve_records(records)
    assert first['T_ee_camera'] == second['T_ee_camera']


def test_parallel_rotations_are_rejected():
    poses = [transform(Rotation.from_euler('z', a).as_matrix(), [a, 0, 0])
             for a in np.linspace(-0.4, 0.4, 12)]
    with pytest.raises(ValueError, match='parallel'):
        solve(poses, [np.linalg.inv(p) for p in poses])


def test_fixed_pose_repeats_are_rejected():
    with pytest.raises(ValueError, match='diversity'):
        solve([np.eye(4)] * 8, [np.eye(4)] * 8)


def test_holdout_cannot_repeat_training_pose():
    records, _ = synthetic()
    records[-1]['T_base_ee'] = deepcopy(records[0]['T_base_ee'])
    with pytest.raises(ValueError, match='near-identical'):
        solve_records(records)


def test_mixed_camera_intrinsics_are_rejected():
    records, _ = synthetic()
    records[-1]['color_info']['k'][0] += 1
    with pytest.raises(ValueError, match='Mixed'):
        solve_records(records)


def test_missing_holdout_is_rejected():
    records, _ = synthetic()
    with pytest.raises(ValueError, match='held-out'):
        solve_records(records[:12])


@pytest.mark.parametrize('bad', [
    np.full((4, 4), np.nan), np.diag([-1, 1, 1, 1]), np.diag([2, 1, 1, 1]),
])
def test_invalid_transform_is_rejected(bad):
    with pytest.raises(ValueError):
        validate_transform(bad)


def test_generated_board_is_detected():
    image = board().draw((1750, 1250))
    image = cv2.copyMakeBorder(image, 100, 100, 100, 100, cv2.BORDER_CONSTANT, value=255)
    found = detect(image, [[1800, 0, 975], [0, 1800, 725], [0, 0, 1]], np.zeros(5))
    assert found['corner_count'] == 24
    assert found['reprojection_rms_px'] < 0.1
    assert found['T_optical_board'][2][3] == pytest.approx(0.18, abs=0.0001)


def test_blank_image_is_rejected():
    with pytest.raises(ValueError, match='No ChArUco'):
        detect(np.full((480, 640), 255, dtype=np.uint8), np.eye(3), np.zeros(5))


def test_mount_patch_changes_only_one_attribute():
    xml = '''<robot name="test"><joint name="base_fixed" type="fixed">
      <parent link="base_link"/><child link="link1"/>
      <origin xyz="-0.092 0 0.091" rpy="0 0 0"/></joint>
      <joint name="joint2" type="revolute"><limit velocity="0.1"/></joint></robot>'''
    original = ET.fromstring(xml)
    updated = ET.fromstring(patch_mount(xml))
    original.find("joint[@name='base_fixed']/origin").set('xyz', '-0.092 0.0 0.116')
    assert ET.tostring(original) == ET.tostring(updated)
    with pytest.raises(ValueError, match='differs'):
        patch_mount(ET.tostring(updated, encoding='unicode'))
