"""Exercise delayed action responses and failures without ROS nodes or motion."""

from concurrent.futures import Future
import threading
from types import MethodType, SimpleNamespace
from unittest.mock import Mock

import pytest

from control_pkg.control_node import ControlNode, OperationCancelled
from control_pkg.task_state import TaskState


def make_node(monkeypatch):
    """Bind controller methods to fake ROS edges and completed/pending futures."""
    monkeypatch.setattr('control_pkg.control_node.rclpy.ok', lambda: True)
    node = SimpleNamespace(
        _lock=threading.RLock(),
        _goal_lock=threading.Lock(),
        _stop_event=threading.Event(),
        _operation_id=1,
        _active_goals=[],
        server_timeout=0.0,
        result_timeout=0.0,
        state=TaskState.PICKING,
        recognition_pose=[0.0] * 4,
        home_pose=[0.0] * 4,
        gripper_open=0.019,
        get_logger=Mock(return_value=Mock()),
        _publish_zero_velocity=Mock(),
        _set_manual_drive=Mock(),
        _finish_task=Mock(),
        _send_joint_pose=Mock(),
        _send_gripper=Mock(),
    )
    methods = (
        '_send_goal', '_wait_result', '_wait_future', '_ensure_active',
        '_cancel_active_goals', '_cancel_abandoned_goal', '_fail_operation',
        '_recognition_pick_sequence', '_release_sequence',
    )
    for name in methods:
        setattr(node, name, MethodType(getattr(ControlNode, name), node))

    def set_state(state, operation_id):
        node._ensure_active(operation_id)
        node.state = state

    node._set_state = set_state
    return node


def make_goal(accepted=True):
    """Return a fake goal whose result and cancel request are independent."""
    return SimpleNamespace(
        accepted=accepted,
        get_result_async=Mock(return_value=Future()),
        cancel_goal_async=Mock(return_value=Future()),
    )


def make_client(goal=None):
    """Provide either an immediate or a delayed goal acknowledgement."""
    response = Future()
    if goal is not None:
        response.set_result(goal)
    return SimpleNamespace(send_goal_async=Mock(return_value=response)), response


def test_completed_action_is_removed_without_cancellation(monkeypatch):
    node = make_node(monkeypatch)
    goal = make_goal()
    client, _ = make_client(goal)
    result = object()
    goal.get_result_async.return_value.set_result(result)

    assert node._send_goal(client, object(), 1, 'arm') is goal
    assert node._wait_result(goal, 1, 'arm') is result
    assert node._active_goals == []
    goal.cancel_goal_async.assert_not_called()


@pytest.mark.parametrize('sequence', ['pick', 'release'])
def test_result_timeout_cancels_without_automatic_home(monkeypatch, sequence):
    node = make_node(monkeypatch)
    goal = make_goal()
    node._active_goals.append(goal)

    def await_result(*args, **kwargs):
        return node._wait_result(goal, 1, 'arm')

    if sequence == 'pick':
        node._send_joint_pose.side_effect = await_result
        node._recognition_pick_sequence(1)
        assert node._send_joint_pose.call_count == 1
    else:
        node._send_gripper.side_effect = await_result
        node._release_sequence(1)
        node._send_joint_pose.assert_not_called()

    goal.cancel_goal_async.assert_called_once()
    assert node.state == TaskState.ERROR
    node._finish_task.assert_not_called()


def test_failed_result_future_is_still_available_for_cancellation(monkeypatch):
    node = make_node(monkeypatch)
    goal = make_goal()
    node._active_goals.append(goal)
    goal.get_result_async.return_value.set_exception(RuntimeError('transport'))

    with pytest.raises(RuntimeError, match='transport'):
        node._wait_result(goal, 1, 'arm')
    node._cancel_active_goals()

    goal.cancel_goal_async.assert_called_once()


def test_late_acceptance_after_timeout_is_cancelled_in_same_operation(
    monkeypatch,
):
    node = make_node(monkeypatch)
    client, response = make_client()

    def send_pose(*args):
        return node._send_goal(client, object(), 1, 'arm')

    node._send_joint_pose.side_effect = send_pose
    node._recognition_pick_sequence(1)
    goal = make_goal()
    response.set_result(goal)

    assert node._operation_id == 1
    assert node.state == TaskState.ERROR
    assert node._send_joint_pose.call_count == 1
    goal.cancel_goal_async.assert_called_once()


def test_acceptance_at_timeout_boundary_is_cancelled(monkeypatch):
    node = make_node(monkeypatch)
    goal = make_goal()
    client, response = make_client()

    def timeout_after_response(*args):
        response.set_result(goal)
        raise RuntimeError('goal response timed out')

    node._wait_future = timeout_after_response
    with pytest.raises(RuntimeError, match='timed out'):
        node._send_goal(client, object(), 1, 'arm')

    goal.cancel_goal_async.assert_called_once()


def test_late_acceptance_after_stop_is_cancelled(monkeypatch):
    node = make_node(monkeypatch)
    client, response = make_client()

    def stop_while_waiting(*args):
        node._operation_id += 1
        node._stop_event.set()
        raise OperationCancelled()

    node._wait_future = stop_while_waiting
    with pytest.raises(OperationCancelled):
        node._send_goal(client, object(), 1, 'arm')
    goal = make_goal()
    response.set_result(goal)

    goal.cancel_goal_async.assert_called_once()


def test_stop_between_acceptance_and_registration_cancels_goal(monkeypatch):
    node = make_node(monkeypatch)
    goal = make_goal()
    client, _ = make_client(goal)

    def stop_before_registering(*args):
        node._operation_id += 1
        node._stop_event.set()
        node._cancel_active_goals()
        return goal

    node._wait_future = stop_before_registering
    with pytest.raises(OperationCancelled):
        node._send_goal(client, object(), 1, 'arm')

    goal.cancel_goal_async.assert_called_once()
    assert node._active_goals == []


def test_rejected_goal_is_not_cancelled(monkeypatch):
    node = make_node(monkeypatch)
    goal = make_goal(accepted=False)
    client, _ = make_client(goal)

    with pytest.raises(RuntimeError, match='rejected'):
        node._send_goal(client, object(), 1, 'arm')

    goal.cancel_goal_async.assert_not_called()
    assert node._active_goals == []


def test_timeout_during_automatic_home_also_cancels_goal(monkeypatch):
    node = make_node(monkeypatch)
    goal = make_goal()

    def home(*args):
        node._active_goals.append(goal)
        return node._wait_result(goal, 1, 'HOME')

    node._send_joint_pose.side_effect = home
    node._fail_operation(1, 'vision rejected')

    goal.cancel_goal_async.assert_called_once()
    assert node.state == TaskState.ERROR
    node._finish_task.assert_not_called()
