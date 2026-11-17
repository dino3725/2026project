"""Check B release, duplicate suppression and emergency priority offline."""

import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from sensor_msgs.msg import Joy
from std_msgs.msg import Bool

from control_pkg.control_node import ControlNode
from control_pkg.task_state import TaskState


def release_node(state=TaskState.DRIVE_TO_PLACE):
    """Build a node stub with the real button dispatch and release handler."""
    node = SimpleNamespace(
        state=state, _lock=threading.RLock(), _previous_buttons=[],
        recognition_button_index=0, release_button_index=1,
        home_button_index=2, emergency_button_index=3,
        _set_manual_drive=Mock(), _publish_zero_velocity=Mock(),
        _restore_state_authority=Mock(), _start_operation=Mock(),
        _release_sequence=object(), _handle_emergency_stop=Mock(),
        _handle_home_recovery=Mock(), _handle_recognition_request=Mock(),
        _palm_gate=Mock(), release_countdown_publisher=Mock(),
        open_palm_hold_sec=2.0, get_logger=Mock(return_value=Mock()),
    )
    node._set_state = lambda value: setattr(node, 'state', value)
    node._rising_edge = lambda buttons, index: ControlNode._rising_edge(
        node, buttons, index)
    node._handle_release_request = lambda: ControlNode._handle_release_request(node)
    return node


def test_b_starts_shared_release_once_and_blocks_palm_duplicate():
    """B starts the palm sequence once and immediately claims base control."""
    node = release_node()
    press = Joy(buttons=[0, 1, 0, 0])
    ControlNode.joy_callback(node, press)
    ControlNode.joy_callback(node, press)
    ControlNode.joy_callback(node, Joy(buttons=[0, 0, 0, 0]))
    ControlNode.joy_callback(node, press)
    ControlNode.palm_callback(node, Bool(data=True))
    assert node.state == TaskState.PLACING
    node._start_operation.assert_called_once_with(node._release_sequence)
    node._set_manual_drive.assert_called_once_with(False, force=True)
    node._publish_zero_velocity.assert_called_once()
    node._palm_gate.observe.assert_not_called()


@pytest.mark.parametrize('state', [
    state for state in TaskState if state != TaskState.DRIVE_TO_PLACE
])
def test_b_is_ignored_outside_cargo_wait(state):
    """B must not release during picking, recovery, idle or emergency stop."""
    node = release_node(state)
    ControlNode.joy_callback(node, Joy(buttons=[0, 1, 0, 0]))
    node._start_operation.assert_not_called()
    node._restore_state_authority.assert_called_once()


def test_y_has_priority_over_b():
    """A simultaneous Y and B press stops without starting release."""
    node = release_node()
    ControlNode.joy_callback(node, Joy(buttons=[0, 1, 0, 1]))
    node._handle_emergency_stop.assert_called_once()
    node._start_operation.assert_not_called()


def test_shared_release_opens_then_returns_home():
    """The shared palm/B sequence opens before issuing the HOME command."""
    events = []
    node = SimpleNamespace(
        gripper_open=0.019, release_settle=0.5, home_pose=[0.0] * 4,
        _send_gripper=lambda *args: events.append('open'),
        _sleep_cancelable=lambda *args: events.append('settle'),
        _set_state=lambda state, op: events.append(state),
        _send_joint_pose=lambda *args: events.append(('arm', args[-1])),
        _finish_task=lambda op: events.append('finished'),
        _fail_operation=Mock(),
    )
    ControlNode._release_sequence(node, 1)
    assert events == [
        'open', 'settle', TaskState.RETURN_HOME, ('arm', 'HOME'), 'finished',
    ]
    node._fail_operation.assert_not_called()
