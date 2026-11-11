"""Execute one audited nearby calibration pose at a time with measurement guards."""

from functools import partial
import hashlib
import json
from pathlib import Path

import numpy as np
import rclpy
from rclpy.executors import MultiThreadedExecutor
from rclpy.qos import DurabilityPolicy, QoSProfile
from std_msgs.msg import String
from std_srvs.srv import Trigger

from control_pkg.control_node import OperationCancelled
from control_pkg.task_state import TaskState
from measurement_control import MeasurementControl
from calibration_plan import EXPANDED_OFFSETS_DEG


def validate_plan(plan):
    """Retain the original gate or require the exact reviewed expanded pose list."""
    center = np.asarray(plan['center_positions_rad'], dtype=float)
    if center.shape != (4,) or not np.isfinite(center).all():
        raise ValueError('Invalid plan center')
    # Restrict this session tool to the already checked recognition neighborhood.
    if np.max(np.abs(np.rad2deg(center) - [0, -30, -41, 115])) > 2:
        raise ValueError('Plan center outside the reviewed recognition neighborhood')
    if plan['motion_duration_sec'] != 4.0 or len(plan['poses']) != 15:
        raise ValueError('Unexpected motion timing or plan length')
    phase = plan.get('phase', 'nearby_v1')
    if phase not in ['nearby_v1', 'expanded_v2']:
        raise ValueError('Unknown calibration phase')
    start = np.asarray(plan.get('start_positions_rad', center), dtype=float)
    if start.shape != (4,) or not np.isfinite(start).all():
        raise ValueError('Invalid planned start')
    if phase == 'expanded_v2' and np.max(np.abs(np.rad2deg(start - center))) > 14:
        raise ValueError('Expanded start differs from the reviewed first-session endpoint')
    for i, pose in enumerate(plan['poses']):
        q = np.asarray(pose['positions_rad'], dtype=float)
        if q.shape != (4,) or not np.isfinite(q).all() or pose['index'] != i + 1:
            raise ValueError('Invalid indexed pose')
        delta = np.rad2deg(q - center)
        if phase == 'expanded_v2':
            if not np.allclose(delta, EXPANDED_OFFSETS_DEG[i], atol=1e-6, rtol=0):
                raise ValueError('Expanded pose differs from the reviewed exact list')
        elif (abs(delta[0]) > 12.000001 or not -0.000001 <= delta[1] <= 12.000001
                or np.max(np.abs(delta[2:])) > 0.000001):
            raise ValueError('Pose outside the reviewed nearby region')
    return plan


class CalibrationControl(MeasurementControl):
    """Keep the hardware running while replacing only the measurement controller."""

    def __init__(self):
        """Load an explicit checked plan; startup never initiates motion."""
        self._model_sha = ''
        self._next_index = 0
        self._completed_index = 0
        self.plan = None
        super().__init__()
        self.declare_parameter('calibration_plan_path', '')
        path = Path(self.get_parameter('calibration_plan_path').value)
        self.plan = validate_plan(json.loads(path.read_text()))
        if self.joint_motion_duration != self.plan['motion_duration_sec']:
            raise ValueError('Controller duration differs from audited plan')
        self.create_subscription(
            String, '/robot_description', self.on_model,
            QoSProfile(depth=1, durability=DurabilityPolicy.TRANSIENT_LOCAL))
        self.pose_services = [self.create_service(
            Trigger, f'/measurement/calibration_pose_{i + 1:02d}', partial(self.move_pose, i),
            callback_group=self.callback_group) for i in range(len(self.plan['poses']))]
        self.calibration_status = self.create_publisher(
            String, '/measurement/calibration_status', 10)
        self.create_timer(0.2, self.publish_calibration_status, callback_group=self.callback_group)
        self.get_logger().info(
            'Calibration controller ready for Y verification; no startup motion.')

    def on_model(self, msg):
        """Reject a different robot model throughout the calibration run."""
        with self._lock:
            self._model_sha = hashlib.sha256(msg.data.encode()).hexdigest()

    def blocked_reason(self):
        """Retain all stop/heartbeat checks and require the reviewed model."""
        reason = super().blocked_reason()
        if reason:
            return reason
        if self.plan is None or self._model_sha != self.plan['model_sha256']:
            return 'Robot model does not match calibration plan'
        return ''

    def prepare(self, request, response):
        """Disable the old recognition move while an indexed calibration plan is loaded."""
        response.success = False
        response.message = 'Use the explicit indexed calibration pose service'
        return response

    def move_pose(self, index, request, response):
        """Accept only the next pose, from the measured neighborhood of its predecessor."""
        with self._lock:
            reason = self.blocked_reason()
            if self._preparing:
                reason = 'A pose movement is already pending'
            elif index != self._next_index:
                reason = 'Out-of-order or repeated pose request'
            else:
                expected = (self.plan.get('start_positions_rad', self.plan['center_positions_rad'])
                            if index == 0
                            else self.plan['poses'][index - 1]['positions_rad'])
                current = self._current_joint_positions
                if (current is None or not np.isfinite(current).all()
                        or np.max(np.abs(np.asarray(current) - expected)) > np.deg2rad(2)):
                    reason = 'Current joints differ from reviewed start; inspect before recovery'
            if reason:
                response.success = False
                response.message = reason
                return response
            self._preparing = True
            self._set_state(TaskState.MOVE_TO_RECOGNITION)
            self._start_operation(partial(self.move_sequence, index))
            response.success = True
            response.message = f'Pose {index + 1} accepted; wait for completed status and ready'
            return response

    def move_sequence(self, index, operation_id):
        """Use registered/cancelable actions, with no automatic retry or HOME."""
        try:
            pose = self.plan['poses'][index]
            self._send_joint_pose(
                pose['positions_rad'], operation_id, f'CALIBRATION_{index + 1:02d}')
            self._sleep_cancelable(self.recognition_settle, operation_id)
            with self._lock:
                self._ensure_active(operation_id)
                if np.max(np.abs(np.asarray(self._current_joint_positions)
                                 - pose['positions_rad'])) > np.deg2rad(2):
                    raise RuntimeError('Final joint positions outside calibration tolerance')
                self._next_index = index + 1
                self._completed_index = index + 1
                self._set_state(TaskState.VISION_SEARCH, operation_id)
            self.get_logger().info(f'Calibration pose {index + 1} complete; holding.')
        except OperationCancelled:
            pass
        except Exception as exc:
            self._fail_operation(operation_id, str(exc), auto_home=False)
        finally:
            with self._lock:
                self._preparing = False

    def publish_calibration_status(self):
        """Expose progress so capture is never inferred from service acceptance."""
        with self._lock:
            self.calibration_status.publish(String(data=json.dumps({
                'completed_pose': self._completed_index, 'preparing': self._preparing,
                'blocked_reason': self.blocked_reason(), 'stopped': self._stop_event.is_set(),
            })))


def main():
    """Run the guarded controller without initiating any trajectory."""
    rclpy.init()
    node = CalibrationControl()
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
