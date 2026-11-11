"""Check indexed motion gates without ROS hardware or real action goals."""

from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock
import threading

import numpy as np
import pytest

from calibration_control import CalibrationControl, validate_plan
from calibration_plan import EXPANDED_OFFSETS_DEG


def plan():
    """Build a small structurally valid test plan."""
    center = np.deg2rad([0, -30, -40, 115]).tolist()
    return {'center_positions_rad': center, 'motion_duration_sec': 4.0,
            'poses': [{'index': i + 1, 'positions_rad': center.copy()} for i in range(15)]}


@pytest.mark.parametrize('change', ['yaw', 'shoulder', 'wrist', 'nan', 'center'])
def test_plan_rejects_unreviewed_changes(change):
    candidate = plan()
    if change == 'center':
        candidate['center_positions_rad'][0] = 1
    else:
        index, delta = {'yaw': (0, 0.3), 'shoulder': (1, -0.1),
                        'wrist': (3, 0.05), 'nan': (0, float('nan'))}[change]
        candidate['poses'][0]['positions_rad'][index] += delta
    with pytest.raises(ValueError):
        validate_plan(candidate)


def harness():
    """Supply only the state used by the indexed Trigger callback."""
    candidate = plan()
    return SimpleNamespace(
        _lock=threading.RLock(), _next_index=0, _preparing=False,
        _current_joint_positions=deepcopy(candidate['center_positions_rad']), plan=candidate,
        blocked_reason=Mock(return_value=''), _start_operation=Mock(), _set_state=Mock(),
        move_sequence=Mock(),
    )


@pytest.mark.parametrize('reason', ['stop gate', 'stale joystick', 'model mismatch'])
def test_pose_inherits_safety_gate(reason):
    node = harness()
    node.blocked_reason.return_value = reason
    result = CalibrationControl.move_pose(node, 0, None, SimpleNamespace())
    assert not result.success
    node._start_operation.assert_not_called()


def test_out_of_order_pose_is_rejected():
    node = harness()
    result = CalibrationControl.move_pose(node, 1, None, SimpleNamespace())
    assert not result.success
    node._start_operation.assert_not_called()


def test_unexpected_current_pose_is_rejected():
    node = harness()
    node._current_joint_positions[0] += 0.1
    result = CalibrationControl.move_pose(node, 0, None, SimpleNamespace())
    assert not result.success
    node._start_operation.assert_not_called()


def test_explicit_next_request_starts_one_operation():
    node = harness()
    result = CalibrationControl.move_pose(node, 0, None, SimpleNamespace())
    assert result.success
    assert node._preparing
    node._start_operation.assert_called_once()


def test_original_prepare_cannot_bypass_indexed_plan():
    node = harness()
    result = CalibrationControl.prepare(node, None, SimpleNamespace())
    assert not result.success
    node._start_operation.assert_not_called()


def expanded_plan():
    """Build the exact reviewed expanded sequence for validation tests."""
    candidate = plan()
    candidate['phase'] = 'expanded_v2'
    candidate['start_positions_rad'] = candidate['center_positions_rad'].copy()
    center = np.array(candidate['center_positions_rad'])
    for pose, offset in zip(candidate['poses'], EXPANDED_OFFSETS_DEG):
        pose['positions_rad'] = (center + np.deg2rad(offset)).tolist()
    return candidate


def test_reviewed_expanded_sequence_is_allowed():
    assert validate_plan(expanded_plan())['phase'] == 'expanded_v2'


def test_edited_expanded_sequence_is_rejected():
    candidate = expanded_plan()
    candidate['poses'][8]['positions_rad'][3] += np.deg2rad(1)
    with pytest.raises(ValueError, match='exact list'):
        validate_plan(candidate)


def test_expanded_start_outside_prior_neighborhood_is_rejected():
    candidate = expanded_plan()
    candidate['start_positions_rad'][0] += np.deg2rad(20)
    with pytest.raises(ValueError, match='start differs'):
        validate_plan(candidate)
