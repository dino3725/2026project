"""Hold the existing recognition pose with Y-stop and no pick/base driving."""

import math
import time

import rclpy
from rclpy.executors import MultiThreadedExecutor
from rclpy.parameter import Parameter
from std_msgs.msg import Bool
from std_srvs.srv import Trigger

from control_pkg.control_node import ControlNode, OperationCancelled
from control_pkg.task_state import TaskState


class MeasurementControl(ControlNode):
    """Use the normal action cancellation path for one explicit arm move."""

    def __init__(self):
        self._joy_at = 0.0
        self._joint_at = 0.0
        self._stop_seen = False
        self._y_down = False
        self._preparing = False
        super().__init__()
        self.ready_publisher = self.create_publisher(
            Bool, '/measurement/ready', 10,
        )
        self.prepare_service = self.create_service(
            Trigger, '/measurement/prepare_recognition', self.prepare,
            callback_group=self.callback_group,
        )
        self.guard_timer = self.create_timer(
            0.1, self.guard, callback_group=self.callback_group,
        )
        self.get_logger().info(
            'MEASUREMENT MODE: no startup movement, no fetch, no gripper, '
            'no base driving. Press and release Y before prepare service.'
        )

    def _read_parameters(self):
        super()._read_parameters()
        self.home_on_startup = False
        self.pick_enabled = False
        self.set_parameters([
            Parameter('home_on_startup', value=False),
            Parameter('pick_enabled', value=False),
        ])

    def _set_manual_drive(self, enabled, force=False):
        super()._set_manual_drive(False, force=force)

    def command_callback(self, msg):
        if msg.action == 'stop':
            self._handle_emergency_stop('voice')
        else:
            self._publish_command_status(
                False, msg.action, msg.target_class,
                'Measurement mode: fetch is disabled',
            )

    def palm_callback(self, msg):
        pass

    def joy_callback(self, msg):
        with self._lock:
            self._joy_at = time.monotonic()
            down = (
                len(msg.buttons) > self.emergency_button_index
                and bool(msg.buttons[self.emergency_button_index])
            )
            rising = down and not self._y_down
            self._y_down = down
            if rising:
                self._stop_seen = True
                self._handle_emergency_stop('Y')
                self.get_logger().info('Physical Y input verified.')

    def joint_state_callback(self, msg):
        values = dict(zip(msg.name, msg.position))
        if all(
            name in values and math.isfinite(values[name])
            for name in self.JOINT_NAMES
        ):
            with self._lock:
                super().joint_state_callback(msg)
                self._joint_at = time.monotonic()

    def _publish_arm_hold(self):
        if time.monotonic() - self._joint_at <= 0.5:
            super()._publish_arm_hold()

    def _handle_emergency_stop(self, source='Y'):
        with self._lock:
            self._operation_id += 1
            self._stop_event.set()
            self._emergency_active = True
            self._set_manual_drive(False, force=True)
            self._set_state(TaskState.ERROR)
        self._cancel_active_goals()
        self._publish_zero_velocity()
        self._publish_arm_hold()
        self.get_logger().warn(
            f'Measurement stop ({source}): cancellation requested; '
            'no automatic HOME and base remains disabled.'
        )

    def blocked_reason(self):
        now = time.monotonic()
        if not self._stop_seen:
            return 'Press and release physical Y first'
        if self._y_down:
            return 'Release Y first'
        if now - self._joy_at > 0.5:
            return 'No fresh joystick heartbeat'
        if now - self._joint_at > 0.5:
            return 'No fresh complete joint state'
        return ''

    def prepare(self, request, response):
        with self._lock:
            reason = self.blocked_reason()
            if self._preparing:
                reason = 'An arm movement is already pending'
            if reason:
                response.success = False
                response.message = reason
                return response
            self._preparing = True
            self._set_state(TaskState.MOVE_TO_RECOGNITION)
            self._start_operation(self.prepare_sequence)
            response.success = True
            response.message = 'Recognition pose requested; await ready=true'
            return response

    def prepare_sequence(self, operation_id):
        try:
            self._send_joint_pose(
                self.recognition_pose, operation_id, 'RECOGNITION',
            )
            self._sleep_cancelable(self.recognition_settle, operation_id)
            with self._lock:
                self._ensure_active(operation_id)
                self._set_state(TaskState.VISION_SEARCH, operation_id)
            self.get_logger().info(
                'Recognition pose ready. Holding; no pick or gripper command.'
            )
        except OperationCancelled:
            pass
        except Exception as exc:
            self._fail_operation(
                operation_id, f'Measurement pose failed: {exc}',
                auto_home=False,
            )
        finally:
            with self._lock:
                self._preparing = False

    def guard(self):
        with self._lock:
            reason = self.blocked_reason()
            if self._preparing and reason and not self._stop_event.is_set():
                self._handle_emergency_stop(reason)
            ready = (
                not reason and not self._preparing
                and not self._stop_event.is_set()
                and self.state == TaskState.VISION_SEARCH
            )
            self.ready_publisher.publish(Bool(data=ready))


def main():
    rclpy.init()
    node = MeasurementControl()
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
