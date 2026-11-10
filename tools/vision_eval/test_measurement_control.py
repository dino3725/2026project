"""Check measurement-only movement gates without starting a ROS node."""

import threading
import time
from types import MethodType, SimpleNamespace
from unittest.mock import Mock

import pytest

from control_pkg.task_state import TaskState
from measurement_control import MeasurementControl


def harness():
    now = time.monotonic()
    node = SimpleNamespace(
        _lock=threading.RLock(), _joy_at=now, _joint_at=now,
        _stop_seen=False, _y_down=False, _preparing=False,
        _stop_event=threading.Event(), state=TaskState.IDLE,
        emergency_button_index=3, _handle_emergency_stop=Mock(),
        _publish_command_status=Mock(), _set_state=Mock(),
        _start_operation=Mock(), prepare_sequence=Mock(),
        ready_publisher=Mock(), get_logger=Mock(return_value=Mock()),
    )
    node.blocked_reason = MethodType(MeasurementControl.blocked_reason, node)
    return node


@pytest.mark.parametrize('condition', ['no_y', 'held_y', 'old_joy', 'old_joint'])
def test_prepare_requires_verified_stop_and_fresh_inputs(condition):
    node = harness()
    node._stop_seen = condition != 'no_y'
    node._y_down = condition == 'held_y'
    if condition == 'old_joy':
        node._joy_at = 0
    if condition == 'old_joint':
        node._joint_at = 0
    result = MeasurementControl.prepare(node, None, SimpleNamespace())
    assert not result.success
    node._start_operation.assert_not_called()


def test_prepare_starts_only_an_explicit_recognition_request():
    node = harness()
    node._stop_seen = True
    result = MeasurementControl.prepare(node, None, SimpleNamespace())
    assert result.success
    node._start_operation.assert_called_once_with(node.prepare_sequence)
    assert node._preparing


def test_joystick_only_y_triggers_stop():
    node = harness()
    MeasurementControl.joy_callback(
        node, SimpleNamespace(buttons=[1, 0, 1, 0]),
    )
    node._handle_emergency_stop.assert_not_called()
    MeasurementControl.joy_callback(
        node, SimpleNamespace(buttons=[0, 0, 0, 1]),
    )
    node._handle_emergency_stop.assert_called_once_with('Y')
    assert node._stop_seen


def test_lost_joystick_stops_an_active_move():
    node = harness()
    node._stop_seen = True
    node._preparing = True
    node._joy_at = 0
    MeasurementControl.guard(node)
    node._handle_emergency_stop.assert_called_once()
    assert not node.ready_publisher.publish.call_args.args[0].data


def test_fetch_is_rejected_and_palm_cannot_release():
    node = harness()
    msg = SimpleNamespace(action='fetch', target_class='coke')
    MeasurementControl.command_callback(node, msg)
    MeasurementControl.palm_callback(node, SimpleNamespace(data=True))
    assert node._publish_command_status.call_args.args[0] is False
    node._start_operation.assert_not_called()


def test_stop_command_is_still_handled():
    node = harness()
    MeasurementControl.command_callback(
        node, SimpleNamespace(action='stop', target_class=''),
    )
    node._handle_emergency_stop.assert_called_once_with('voice')
