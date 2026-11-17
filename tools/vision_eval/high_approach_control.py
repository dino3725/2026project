"""Execute one reviewed high trajectory with measurement guards."""

import hashlib
import json
from pathlib import Path

import numpy as np
import rclpy
from action_msgs.msg import GoalStatus
from moveit_msgs.action import ExecuteTrajectory
from rclpy.executors import MultiThreadedExecutor
from rosidl_runtime_py.set_message import set_message_fields
from std_srvs.srv import Trigger

from control_pkg.control_node import OperationCancelled
from control_pkg.task_state import TaskState
from measurement_control import MeasurementControl


def validate_trajectory(plan, current, model_sha):
    """Reject stale starts, unreviewed plans and unexpected actuators."""
    if (not plan.get('approved') or plan.get('executed')
            or plan['model_sha256'] != model_sha
            or plan['velocity_scale'] != 0.10
            or plan['acceleration_scale'] != 0.10):
        raise ValueError('Unapproved plan/model/scaling')
    target = plan['target_m']
    diagnostic = plan.get('kind') == 'diagnostic_lift'
    height_ok = 0.30 <= target[2] <= 0.36 if diagnostic else target[2] == 0.290
    if not (0.20 <= target[0] <= 0.31
            and abs(target[1]) <= 0.08 and height_ok):
        raise ValueError('Not a reviewed high approach target')
    trajectory = plan['response']['trajectory']
    joint = trajectory['joint_trajectory']
    if (joint['joint_names'] != ['joint1', 'joint2', 'joint3', 'joint4']
            or trajectory['multi_dof_joint_trajectory']['points']):
        raise ValueError('Unexpected actuators')
    points = joint['points']
    if len(points) < 2:
        raise ValueError('Empty trajectory')
    q = np.array([point['positions'] for point in points], dtype=float)
    v = np.array([point['velocities'] for point in points], dtype=float)
    a = np.array([point['accelerations'] for point in points], dtype=float)
    t = np.array([p['time_from_start']['sec']
                  + p['time_from_start']['nanosec'] * 1e-9 for p in points])
    if (q.shape != (len(points), 4) or v.shape != q.shape or a.shape != q.shape
            or not all(np.isfinite(x).all() for x in (q, v, a, t))):
        raise ValueError('Malformed/nonfinite trajectory')
    if diagnostic:
        prior = np.array(plan['previous_reference_rad'], dtype=float)
        goal = np.array(plan['diagnostic_target_rad'], dtype=float)
        if (prior.shape != (4,) or goal.shape != (4,)
                or not np.isfinite(prior).all() or not np.isfinite(goal).all()
                or not np.allclose(goal - prior, [0, -np.deg2rad(6), 0, 0],
                                   atol=1e-8, rtol=0)
                or np.max(np.abs(q[-1] - goal)) > 0.00011):
            raise ValueError('Not the reviewed shoulder-only diagnostic lift')
    minimum_duration = 4 if diagnostic else 1
    if (abs(t[0]) > 1e-9 or np.any(np.diff(t) <= 0)
            or not minimum_duration <= t[-1] <= 20):
        raise ValueError('Invalid trajectory timing')
    # Installed MoveIt reports a 1 rad/s^2 fallback because the vendor model
    # has no acceleration limits. Keep the existing 0.10 request multipliers;
    # do not mistake that multiplier for a 0.10 rad/s^2 physical limit.
    if np.max(np.abs(v)) > 0.480001 or np.max(np.abs(a)) > 1.000001:
        raise ValueError('Trajectory exceeds reviewed speed/acceleration')
    if (np.any(q < np.deg2rad([-162, -102.6, -53, -102.6]) - 1e-6)
            or np.any(q > np.deg2rad([162, 90, 78, 117]) + 1e-6)):
        raise ValueError('Joint limit or elbow margin violated')
    current = np.asarray(current, dtype=float)
    if (current.shape != (4,) or not np.isfinite(current).all()
            or np.max(np.abs(q[0] - current)) > np.deg2rad(2)):
        raise ValueError('Robot has moved since planning')
    return trajectory, float(t[-1])


class HighApproachControl(MeasurementControl):
    """Hold after one approach; never descend, grasp or return HOME."""

    def __init__(self):
        self.used = False
        super().__init__()
        self.declare_parameter('reviewed_plan_path', '')
        self.declare_parameter('reviewed_plan_sha256', '')
        self.path = Path(self.get_parameter('reviewed_plan_path').value)
        data = self.path.read_bytes()
        if hashlib.sha256(data).hexdigest() != self.get_parameter(
                'reviewed_plan_sha256').value:
            raise ValueError('Reviewed plan hash mismatch')
        self.plan = json.loads(data)
        model = (Path(__file__).resolve().parents[2]
                 / 'log/measured_model_20261111/measured.urdf')
        self.model_sha = hashlib.sha256(model.read_bytes()).hexdigest()
        self.result_path = self.path.parent / 'execution.json'
        if self.result_path.exists():
            raise ValueError('Execution already attempted; do not replay')
        self.execute_service = self.create_service(
            Trigger, '/measurement/execute_high_approach', self.execute,
            callback_group=self.callback_group)
        self.get_logger().info(
            'HIGH APPROACH: no startup motion; awaiting explicit service.')

    def prepare(self, request, response):
        response.success = False
        response.message = (
            'Recognition movement disabled in one-shot approach controller')
        return response

    def execute(self, request, response):
        with self._lock:
            reason = self.blocked_reason()
            if self.used or self._preparing or self._stop_event.is_set():
                reason = 'Already attempted, busy or stopped'
            if reason:
                response.success = False
                response.message = reason
                return response
            try:
                self.trajectory, self.duration = validate_trajectory(
                    self.plan, self._current_joint_positions, self.model_sha)
            except Exception as exc:
                response.success = False
                response.message = str(exc)
                return response
            # Mark before sending so a crash cannot make this replayable.
            self.result_path.write_text(json.dumps({'attempted': True,
                                                   'completed': False}) + '\n')
            self.used = True
            self._preparing = True
            self._start_operation(self.approach_sequence)
            response.success = True
            response.message = (
                'One reviewed high approach requested; no gripper/descend')
            return response

    def approach_sequence(self, operation_id):
        result = {'attempted': True, 'completed': False}
        try:
            self._wait_for_server(self.execute_trajectory_client,
                                  'MoveIt execution', operation_id)
            goal = ExecuteTrajectory.Goal()
            set_message_fields(goal.trajectory, self.trajectory)
            handle = self._send_goal(self.execute_trajectory_client, goal,
                                     operation_id, 'HIGH APPROACH')
            response = self._wait_result(handle, operation_id,
                                         'HIGH APPROACH', self.duration + 10)
            if (response.status != GoalStatus.STATUS_SUCCEEDED
                    or response.result.error_code.val != 1):
                raise RuntimeError(
                    f'Execution failed: {response.result.error_code.val}')
            result['action_succeeded'] = True
            self._verify_trajectory_arrival(
                goal.trajectory.joint_trajectory, operation_id,
                'HIGH APPROACH', self.plan['target_m'])
            result['arrival_verified'] = True
            with self._lock:
                self._ensure_active(operation_id)
                self._set_state(TaskState.VISION_SEARCH, operation_id)
                result.update(
                    completed=True,
                    final_joints_rad=list(self._current_joint_positions))
            self.get_logger().info(
                'HIGH APPROACH complete. Holding; no descent or grasp.')
        except OperationCancelled:
            result['error'] = 'Cancelled'
        except Exception as exc:
            result['error'] = str(exc)
            self._fail_operation(operation_id, str(exc), auto_home=False)
        finally:
            self.result_path.write_text(json.dumps(result, indent=2) + '\n')
            with self._lock:
                self._preparing = False


def main():
    rclpy.init()
    node = HighApproachControl()
    executor = MultiThreadedExecutor(num_threads=4)
    executor.add_node(node)
    try:
        executor.spin()
    except KeyboardInterrupt:
        node._handle_emergency_stop('process interrupt')
    finally:
        executor.shutdown()
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()


if __name__ == '__main__':
    main()
