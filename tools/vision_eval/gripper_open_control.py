"""One explicit gripper-open request with the existing stop/cancellation guards."""

import json
from pathlib import Path
import signal

import rclpy
from rclpy.executors import MultiThreadedExecutor
from rclpy.signals import SignalHandlerOptions
from std_srvs.srv import Trigger

from control_pkg.control_node import OperationCancelled
from measurement_control import MeasurementControl


class GripperOpenControl(MeasurementControl):
    """Open once at the current arm pose; do not move the arm or close the jaws."""

    def __init__(self):
        self.used = False
        super().__init__()
        self.declare_parameter('result_path', '')
        self.result_path = Path(self.get_parameter('result_path').value)
        if not str(self.get_parameter('result_path').value) or self.result_path.exists():
            raise ValueError('A new explicit result path is required')
        self.open_service = self.create_service(
            Trigger, '/measurement/open_gripper_once', self.open_once,
            callback_group=self.callback_group)

    def prepare(self, request, response):
        response.success = False
        response.message = 'Arm motion disabled in gripper-open-only controller'
        return response

    def open_once(self, request, response):
        with self._lock:
            reason = self.blocked_reason()
            if self.used or self._preparing or self._stop_event.is_set():
                reason = 'Already attempted, busy or stopped'
            if reason:
                response.success, response.message = False, reason
                return response
            self.result_path.write_text(json.dumps(dict(attempted=True, completed=False))+'\n')
            self.used, self._preparing = True, True
            self._start_operation(self.open_sequence)
            response.success, response.message = True, 'One gripper OPEN requested; arm holds'
            return response

    def open_sequence(self, operation_id):
        result = dict(attempted=True, completed=False, target=self.gripper_open)
        try:
            self._send_gripper(self.gripper_open, operation_id, 'MODEL TRIAL OPEN')
            self._sleep_cancelable(.5, operation_id)
            result['completed'] = True
        except OperationCancelled:
            result['error'] = 'Cancelled'
        except Exception as exc:
            result['error'] = str(exc)
            self._fail_operation(operation_id, str(exc), auto_home=False)
        finally:
            self.result_path.write_text(json.dumps(result, indent=2)+'\n')
            with self._lock:
                self._preparing = False


def main():
    # Keep ROS alive while cancelling goals on a process interruption.
    rclpy.init(signal_handler_options=SignalHandlerOptions.NO)

    def interrupt(signum, frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGINT, interrupt)
    signal.signal(signal.SIGTERM, interrupt)
    node = GripperOpenControl()
    executor = MultiThreadedExecutor(num_threads=4)
    executor.add_node(node)
    try:
        executor.spin()
    except KeyboardInterrupt:
        node._handle_emergency_stop('process interrupt')
    finally:
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        executor.shutdown()
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()


if __name__ == '__main__':
    main()
