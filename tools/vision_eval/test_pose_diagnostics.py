"""Verify kinematics, geometric ambiguities, and training-only diagnostics."""

import cv2
import numpy as np
import pytest
from scipy.spatial.transform import Rotation

from calibration_plan import fk
from handeye import transform
from pose_diagnostics import (ArmChain, diagnostic_decision, fit_geometry, initial_vector,
                              JOINT_NAMES, sensitivity)
from refine_handeye import unpack


@pytest.fixture
def scene():
    """Build independently excited arm poses with exact synthetic observations."""
    specs = [('joint1', 'base_link', 'a', '0 0 .12', '0 0 1'),
             ('joint2', 'a', 'b', '0 0 .07', '0 1 0'),
             ('joint3', 'b', 'c', '.024 0 .128', '0 1 0'),
             ('joint4', 'c', 'd', '.124 0 0', '0 1 0')]
    xml = '<robot name="test">'
    for name, parent, child, xyz, axis in specs:
        xml += (f'<joint name="{name}" type="revolute"><parent link="{parent}"/>'
                f'<child link="{child}"/><origin xyz="{xyz}" rpy="0 0 0"/>'
                f'<axis xyz="{axis}"/><limit lower="-3" upper="3"/></joint>')
    xml += ('<joint name="tip" type="fixed"><parent link="d"/>'
            '<child link="end_effector_link"/><origin xyz=".126 0 0"/></joint></robot>')
    chain = ArmChain(xml)
    mount = transform(Rotation.from_euler('xyz', [.1, -.05, .08]).as_matrix(),
                      [.02, -.03, .05])
    boards = [transform(np.eye(3), [.25, 0, .9]),
              transform(Rotation.from_euler('xyz', [.03, -.04, .02]).as_matrix(),
                        [.27, .01, .92])]
    k = np.array([[900., 0, 640], [0, 900, 360], [0, 0, 1]])
    d = np.zeros(5)
    obj = np.array([[x * .025, y * .025, 0.] for y in range(1, 5) for x in range(1, 7)])
    rng = np.random.default_rng(1911)
    q = rng.uniform(-.2, .2, (20, 4))
    poses = chain.forward(q)
    rows = []
    for i, pose in enumerate(poses):
        board = boards[i // 10]
        camera_board = np.linalg.inv(pose @ mount) @ board
        pixels = cv2.projectPoints(obj, cv2.Rodrigues(camera_board[:3, :3])[0],
                                   camera_board[:3, 3], k, d)[0].reshape(-1, 2)
        rows.append({'role': 'train', 'session_index': i // 10, 'q': q[i],
                     'objects': obj, 'pixels': pixels})
    return xml, chain, rows, k, d, mount, boards


def test_vectorized_fk_matches_independent_chain_evaluator(scene):
    """Compare the cached batch implementation with the existing scalar evaluator."""
    xml, chain, rows, _, _, _, _ = scene
    poses = chain.forward(np.array([r['q'] for r in rows]))
    for row, pose in zip(rows, poses):
        assert np.allclose(pose, fk(xml, dict(zip(JOINT_NAMES, row['q']))), atol=1e-12)


def test_wrist_zero_is_indistinguishable_from_free_mount(scene):
    """Prove wrist bias can be absorbed into a constant camera transform."""
    _, chain, rows, _, _, mount, _ = scene
    q = np.array([r['q'] for r in rows])
    nominal = chain.forward(q)
    shifted = chain.forward(q + [0, 0, 0, .05])
    compensating_mount = np.linalg.inv(shifted[0]) @ nominal[0] @ mount
    assert np.allclose(shifted @ compensating_mount, nominal @ mount, atol=1e-12)


def test_base_zero_is_indistinguishable_from_free_board_pose(scene):
    """Prove base yaw bias can be absorbed into unknown world board poses."""
    _, chain, rows, _, _, mount, boards = scene
    q = np.array([r['q'] for r in rows])
    nominal = chain.forward(q)
    shifted = chain.forward(q + [.04, 0, 0, 0])
    world_change = shifted[0] @ np.linalg.inv(nominal[0])
    for row, a, b in zip(rows, nominal, shifted):
        board = boards[row['session_index']]
        assert np.allclose(np.linalg.inv(a @ mount) @ board,
                           np.linalg.inv(b @ mount) @ world_change @ board, atol=1e-12)


def test_all_joint_bias_jacobian_has_two_unobservable_directions(scene):
    """Recover both exact ambiguities even with independently excited joints."""
    _, chain, rows, k, d, mount, boards = scene
    selected = (0, 1, 2, 3)
    result = sensitivity(initial_vector(mount, boards, selected), rows, chain, k, d, selected)
    assert result['parameter_count'] == 22
    assert result['rank_at_relative_1e_7'] == 20


def test_shared_geometry_recovers_truth_and_rejects_validation_fitting(scene):
    """Recover a known mount without using held-out observations for fitting."""
    _, chain, rows, k, d, mount, boards = scene
    guess = mount.copy()
    guess[:3, 3] += [.003, -.002, .004]
    result = fit_geometry(rows, chain, k, d, guess, boards)
    assert result['training_corner_rms_px'] < 1e-6
    assert np.allclose(unpack(np.array(result['parameters'][:6])), mount, atol=1e-7)
    validation = [dict(row, role='validation') for row in rows]
    with pytest.raises(ValueError, match='Training-only'):
        fit_geometry(validation, chain, k, d, mount, boards)


def test_diagnostic_gate_reports_ambiguity_and_never_exports_runtime_settings():
    """Expose boundary and ambiguity findings without suggesting live correction."""
    report = {'models': {'factory': {
        'all_joint_bias_observability': {'parameter_count': 22, 'rank_at_relative_1e_7': 20},
        'bias': {'bias_hit_5deg_bound': True,
                 'identifiability': {'relative_singular_values': [1., 5e-5]}}}}}
    result = diagnostic_decision(report)
    assert len(result['findings']) == 3
    assert result['status'] == 'diagnostic_only_do_not_apply'
    assert not result['runtime_calibration_exported']
    assert not diagnostic_decision({'models': {}})['runtime_calibration_exported']
