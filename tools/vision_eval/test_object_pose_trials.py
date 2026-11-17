"""Check trial-only observation and retained stop/failure gates."""

import math
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from object_pose_trials import ObjectPoseTrials, summarize_pose
from control_pkg.control_node import ArmArrivalUnverified, OperationCancelled


def test_signed_mean_does_not_hide_absolute_error():
    rows = [[math.radians(2), 0, 0, 0], [math.radians(-2), 0, 0, 0]]
    result = summarize_pose([0] * 4, rows)
    assert result['mean_error_deg'][0] == 0
    assert result['mean_absolute_error_deg'][0] == 2


def observer(monkeypatch, fresh=True):
    clock = [0.0]
    monkeypatch.setattr('object_pose_trials.time.monotonic', lambda: clock[0])
    node = SimpleNamespace(
        trial=1, _lock=threading.RLock(), _current_joint_positions=[0, 0, 0.04, 0],
        _joint_stamp_ns=10_000_000_000, _joint_received_at=0,
        arrival_max_age=0.5, arrival_joint_tolerance=math.radians(1),
        _ensure_active=Mock(), record=Mock(), get_logger=Mock(return_value=Mock()),
        get_clock=lambda: SimpleNamespace(now=lambda: SimpleNamespace(
            nanoseconds=int((10 + clock[0]) * 1e9))))

    def sleep(duration, operation_id):
        clock[0] += duration
        if fresh:
            node._joint_stamp_ns = int((10 + clock[0]) * 1e9)
            node._joint_received_at = clock[0]

    node._sleep_cancelable = sleep
    return node


def test_angle_deviation_is_recorded_without_raising(monkeypatch):
    node = observer(monkeypatch)
    ObjectPoseTrials._verify_arm_arrival(node, [0] * 4, 1, 'PICK')
    assert node.record.call_args.kwargs['nominal_pass'] is False
    assert node.record.call_args.kwargs['mean_error_deg'][2] > 2


def test_stale_data_still_raises(monkeypatch):
    node = observer(monkeypatch, fresh=False)
    with pytest.raises(ArmArrivalUnverified):
        ObjectPoseTrials._verify_arm_arrival(node, [0] * 4, 1, 'PICK')


def test_stop_still_cancels(monkeypatch):
    node = observer(monkeypatch)
    node._ensure_active.side_effect = OperationCancelled()
    with pytest.raises(OperationCancelled):
        ObjectPoseTrials._verify_arm_arrival(node, [0] * 4, 1, 'PICK')


def test_sixth_attempt_is_rejected():
    response = SimpleNamespace()
    result = ObjectPoseTrials.start_trial(
        SimpleNamespace(trial=5), None, response)
    assert result.success is False


def test_action_failure_does_not_proceed_to_grasp_or_cargo():
    node = SimpleNamespace(
        trial=0, trial_target='coke', record=Mock(), _publish_target=Mock(), _set_state=Mock(),
        home_pose=[0] * 4, _send_joint_pose=Mock(side_effect=RuntimeError('failed')),
        _send_gripper=Mock(), _send_cartesian_pose=Mock(),
        _fail_operation=Mock(), _lock=threading.RLock())
    ObjectPoseTrials.trial_sequence(node, 1)
    node._send_gripper.assert_not_called()
    node._send_cartesian_pose.assert_not_called()
    assert node._fail_operation.call_args.kwargs['auto_home'] is False
