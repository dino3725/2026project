"""Verify the HOME diagnostic cannot retry or open other motion paths."""

import threading
from types import SimpleNamespace
from unittest.mock import Mock

from control_pkg.control_node import ArmArrivalUnverified, OperationCancelled
from home_tracking_control import HomeTrackingControl


def harness(tmp_path):
    return SimpleNamespace(
        home_pose=[0, -1, 0.5, 0.7], directory=tmp_path,
        _send_joint_pose=Mock(), record=Mock(), _set_state=Mock(),
        get_logger=Mock(return_value=Mock()), _sleep_cancelable=Mock(),
        _ensure_active=Mock(), _current_joint_positions=[0, -1, 0.5, 0.7],
        _lock=threading.RLock(), _preparing=True, _fail_operation=Mock())


def test_arrival_failure_records_without_retry_or_recovery(tmp_path):
    node = harness(tmp_path)
    node._send_joint_pose.side_effect = ArmArrivalUnverified('1.58 deg')
    HomeTrackingControl.sequence(node, 1)
    node._send_joint_pose.assert_called_once_with(node.home_pose, 1, 'HOME')
    node._sleep_cancelable.assert_called_once_with(15.0, 1)
    assert '1.58 deg' in (tmp_path / 'result.json').read_text()
    node._fail_operation.assert_not_called()
    assert not node._preparing


def test_cancel_does_not_observe_or_retry(tmp_path):
    node = harness(tmp_path)
    node._send_joint_pose.side_effect = OperationCancelled()
    HomeTrackingControl.sequence(node, 1)
    node._sleep_cancelable.assert_not_called()
    assert 'Cancelled' in (tmp_path / 'result.json').read_text()


def test_refuses_second_move_and_recognition():
    node = SimpleNamespace(
        _lock=threading.RLock(), blocked_reason=lambda: '', used=True,
        _start_operation=Mock())
    assert not HomeTrackingControl.execute(node, None, SimpleNamespace()).success
    assert not HomeTrackingControl.prepare(node, None, SimpleNamespace()).success
    node._start_operation.assert_not_called()
