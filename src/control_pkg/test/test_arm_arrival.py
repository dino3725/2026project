"""Verify arrival gates without moving hardware or starting a ROS node."""

import math
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from sensor_msgs.msg import JointState

from control_pkg.arm_arrival import joint_arrival_error, trajectory_endpoint
from control_pkg.control_node import (
    ActionOutcomeUnknown, ArmArrivalUnverified, ControlNode, OperationCancelled,
)
from action_msgs.msg import GoalStatus
from control_msgs.action import FollowJointTrajectory


@pytest.mark.parametrize('actual', [None, [0] * 3, [float('nan')] * 4,
                                    [0, 0, math.radians(2.4), 0]])
def test_missing_bad_or_observed_error_is_rejected(actual):
    """The observed elbow error must not pass the new one-degree criterion."""
    assert joint_arrival_error(actual, [0] * 4, math.radians(1))


def test_joint_order_and_duplicate_names():
    """Reorder named endpoints and reject duplicate or non-arm joints."""
    names = ['joint1', 'joint2', 'joint3', 'joint4']
    trajectory = SimpleNamespace(joint_names=names[::-1],
                                 points=[SimpleNamespace(positions=[4, 3, 2, 1])])
    assert trajectory_endpoint(trajectory, names) == [1, 2, 3, 4]
    trajectory.joint_names.append('joint1')
    with pytest.raises(ValueError):
        trajectory_endpoint(trajectory, names)


def test_home_limits_apply_only_to_the_measured_elbow_residual():
    """A HOME elbow residual is accepted; errors elsewhere still block."""
    limits = [math.radians(x) for x in (1, 1, 2, 1)]
    assert not joint_arrival_error(
        [0, 0, math.radians(1.76), 0], [0] * 4, limits)
    for actual in ([0, math.radians(1.1), 0, 0],
                   [0, 0, math.radians(2.1), 0]):
        assert joint_arrival_error(actual, [0] * 4, limits)
    assert joint_arrival_error(
        [0, 0, math.radians(1.76), 0], [0] * 4, math.radians(1))


@pytest.mark.parametrize('label,pose,uses_home', [
    ('HOME', [0, -1, 0.5, 0.7], True),
    ('HOME', [0, -1, 0.6, 0.7], False),
    ('RECOGNITION', [0, -1, 0.5, 0.7], False),
    ('CARGO', [0, -1, 0.5, 0.7], False),
])
def test_pose_name_alone_cannot_select_home_exception(label, pose, uses_home):
    """Only the unchanged configured HOME goal uses its own limits."""
    pose = list(map(float, pose))
    home_limits = [0.01, 0.01, 0.02, 0.01]
    node = SimpleNamespace(
        _wait_for_server=Mock(), arm_joint_client=Mock(),
        JOINT_NAMES=ControlNode.JOINT_NAMES, joint_motion_duration=4.0,
        _set_duration=ControlNode._set_duration, _send_goal=Mock(),
        _wait_result=Mock(return_value=SimpleNamespace(
            status=GoalStatus.STATUS_SUCCEEDED,
            result=SimpleNamespace(error_code=FollowJointTrajectory.Result.SUCCESSFUL))),
        home_pose=[0, -1, 0.5, 0.7], home_arrival_joint_tolerances=home_limits,
        _verify_arm_arrival=Mock(), _lock=threading.RLock(),
        _current_joint_positions=pose, get_logger=Mock(return_value=Mock()))
    ControlNode._send_joint_pose(node, pose, 1, label)
    assert node._verify_arm_arrival.call_args.kwargs['joint_tolerances'] == (
        home_limits if uses_home else None)


def harness(monkeypatch, actual=None, fresh=True, tool_error=0.0):
    """Advance fake time and sensor callbacks during cancelable waits."""
    clock = [0.0]
    monkeypatch.setattr('control_pkg.control_node.time.monotonic',
                        lambda: clock[0])
    node = SimpleNamespace(
        _lock=threading.RLock(), _current_joint_positions=actual or [0] * 4,
        _joint_received_at=-1.0, _joint_stamp_ns=0,
        arrival_timeout=0.5, arrival_stable=0.15, arrival_max_age=0.2,
        arrival_joint_tolerance=math.radians(1), position_tolerance=0.01,
        base_frame='base_link', end_effector_link='end_effector_link',
        _ensure_active=Mock(), get_logger=Mock(return_value=Mock()),
        get_clock=lambda: SimpleNamespace(now=lambda: SimpleNamespace(
            nanoseconds=int((10 + clock[0]) * 1e9))),
    )

    def sleep(seconds, operation_id):
        clock[0] += seconds
        if fresh:
            node._joint_received_at = clock[0]
            node._joint_stamp_ns = int((10 + clock[0]) * 1e9)

    def transform(*args):
        stamp = int((10 + clock[0]) * 1e9)
        return SimpleNamespace(
            header=SimpleNamespace(stamp=SimpleNamespace(
                sec=stamp // 1_000_000_000, nanosec=stamp % 1_000_000_000)),
            transform=SimpleNamespace(translation=SimpleNamespace(
                x=0.0, y=0.0, z=tool_error)))

    node._sleep_cancelable = sleep
    node.tf_buffer = SimpleNamespace(lookup_transform=transform)
    return node, clock


def test_fresh_stable_feedback_passes(monkeypatch):
    """Wait for multiple fresh messages, not a single old in-range value."""
    node, clock = harness(monkeypatch)
    ControlNode._verify_arm_arrival(node, [0] * 4, 1, 'test', (0, 0, 0))
    assert clock[0] >= node.arrival_stable


@pytest.mark.parametrize('kind', [
    'stale', 'nonfinite_joint', 'nonfinite_tool', 'missing_joint', 'stale_tool',
])
def test_success_flag_does_not_override_feedback_failure(monkeypatch, kind):
    """Invalid or stale feedback must still prevent subsequent motion."""
    node, _ = harness(
        monkeypatch, fresh=kind != 'stale',
        actual=[0, 0, float('nan'), 0] if kind == 'nonfinite_joint' else None,
        tool_error=float('nan') if kind == 'nonfinite_tool' else 0)
    if kind == 'missing_joint':
        node._current_joint_positions = None
    if kind == 'stale_tool':
        transform = node.tf_buffer.lookup_transform()
        transform.header.stamp.sec = 1
        node.tf_buffer.lookup_transform = lambda *args: transform
    with pytest.raises(ArmArrivalUnverified):
        ControlNode._verify_arm_arrival(node, [0] * 4, 1, 'test', (0, 0, 0))


@pytest.mark.parametrize('label', [
    'HOME', 'RECOGNITION', 'PREGRASP', 'PICK', 'RETREAT', 'CARGO',
])
def test_deviation_continues_without_warning(monkeypatch, label):
    """Joint and Cartesian deviations do not block or emit warnings."""
    node, clock = harness(monkeypatch, actual=[0, 0.08, 0.04, 0],
                          tool_error=0.021)
    ControlNode._verify_arm_arrival(node, [0] * 4, 1, label, (0, 0, 0))
    assert clock[0] < node.arrival_timeout
    node.get_logger.assert_not_called()


def test_stop_cancels_arrival_wait(monkeypatch):
    """An emergency stop must escape the settling loop immediately."""
    node, _ = harness(monkeypatch)
    node._ensure_active.side_effect = OperationCancelled()
    with pytest.raises(OperationCancelled):
        ControlNode._verify_arm_arrival(node, [0] * 4, 1, 'test')


def test_bad_joint_message_does_not_refresh_feedback():
    """Nonfinite sensor values must not extend freshness of an older sample."""
    node = SimpleNamespace(
        JOINT_NAMES=ControlNode.JOINT_NAMES, _lock=threading.RLock(),
        _current_joint_positions=[0] * 4, _joint_received_at=123.0)
    msg = JointState(name=ControlNode.JOINT_NAMES,
                     position=[0.0, float('nan'), 0.0, 0.0])
    ControlNode.joint_state_callback(node, msg)
    assert node._joint_received_at == 123.0


def test_failed_arrival_prevents_descent_close_and_automatic_home():
    """Invalid post-action feedback cannot progress pick or trigger HOME."""
    node = SimpleNamespace(
        recognition_pose=[0] * 4, recognition_settle=0.0,
        _send_joint_pose=Mock(), _sleep_cancelable=Mock(), _set_state=Mock(),
        _find_object=Mock(return_value=SimpleNamespace(x=0.25, y=0.0, z=0.15)),
        _lock=threading.RLock(), get_logger=Mock(return_value=Mock()),
        pick_enabled=True, pick_calibration=(0.02, 0, 0),
        pregrasp_offset=(0, 0, 0.05), _add_xyz=ControlNode._add_xyz,
        gripper_open=0.019, _send_gripper=Mock(),
        _send_cartesian_pose=Mock(side_effect=ArmArrivalUnverified('bad arrival')),
        _fail_operation=Mock(),
    )
    ControlNode._recognition_pick_sequence(node, 1)
    assert node._send_cartesian_pose.call_count == 1
    assert node._send_gripper.call_count == 1  # Existing initial OPEN only.
    assert node._send_gripper.call_args.args[-1] == 'OPEN'
    assert node._fail_operation.call_args.kwargs['auto_home'] is False
    assert issubclass(ArmArrivalUnverified, ActionOutcomeUnknown)


def test_cartesian_arrival_uses_planned_reference_not_optional_execution_history():
    """Humble may return success with no recorded executed trajectory."""
    planned = SimpleNamespace(joint_trajectory=object())
    node = SimpleNamespace(
        _wait_for_server=Mock(), move_group_client=Mock(),
        move_group_name='arm', moveit_planning_time=3.0,
        moveit_velocity_scale=0.1, moveit_acceleration_scale=0.1,
        pick_orientation=[0.0, 0.7071068, 0.0, 0.7071068],
        base_frame='base_link', end_effector_link='end_effector_link',
        position_tolerance=0.01, use_orientation_constraint=False,
        _send_goal=Mock(), _wait_result=Mock(return_value=SimpleNamespace(
            status=GoalStatus.STATUS_SUCCEEDED, result=SimpleNamespace(
                error_code=SimpleNamespace(val=1), planned_trajectory=planned,
                executed_trajectory=SimpleNamespace(joint_trajectory=None)))),
        _verify_trajectory_arrival=Mock(), get_logger=Mock(return_value=Mock()))
    ControlNode._send_cartesian_pose(node, (0.25, 0.0, 0.2), 1, 'PREGRASP')
    assert node._verify_trajectory_arrival.call_args.args[0] is planned.joint_trajectory
