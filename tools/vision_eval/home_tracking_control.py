"""Record one explicit existing HOME move, then observe without retrying."""

import json
from pathlib import Path
import time

from control_msgs.msg import DynamicJointState, JointTrajectoryControllerState
import rclpy
from rclpy.executors import MultiThreadedExecutor
from std_srvs.srv import Trigger

from control_pkg.control_node import ArmArrivalUnverified, OperationCancelled
from control_pkg.task_state import TaskState
from measurement_control import MeasurementControl


class HomeTrackingControl(MeasurementControl):
    """Keep fetch, base drive, gripper and automatic recovery disabled."""

    def __init__(self):
        self.used = False
        self.phase = 'idle'
        self.started = time.monotonic()
        super().__init__()
        self.declare_parameter('diagnostic_output', '')
        self.directory = Path(self.get_parameter('diagnostic_output').value)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.journal = (self.directory / 'samples.jsonl').open('x', buffering=1)
        self.create_subscription(
            JointTrajectoryControllerState, '/arm_controller/controller_state',
            self.on_controller, 10, callback_group=self.callback_group)
        self.create_subscription(
            DynamicJointState, '/dynamic_joint_states', self.on_dynamic, 10,
            callback_group=self.callback_group)
        self.create_service(
            Trigger, '/measurement/diagnose_home', self.execute,
            callback_group=self.callback_group)
        self.get_logger().info('HOME DIAGNOSTIC: awaiting explicit one-shot request')

    def record(self, kind, data):
        with self._lock:
            self.journal.write(json.dumps(dict(
                elapsed=time.monotonic() - self.started, phase=self.phase,
                kind=kind, **data)) + '\n')

    def on_controller(self, msg):
        self.record('controller', dict(
            names=list(msg.joint_names),
            desired=list(msg.desired.positions), actual=list(msg.actual.positions),
            velocity=list(msg.actual.velocities), error=list(msg.error.positions)))

    def on_dynamic(self, msg):
        if 'battery' in msg.joint_names:
            value = msg.interface_values[msg.joint_names.index('battery')]
            self.record('battery', dict(zip(value.interface_names, value.values)))

    def prepare(self, request, response):
        response.success = False
        response.message = 'Only explicit HOME diagnosis is enabled'
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
            self.used = True
            self._preparing = True
            self._set_state(TaskState.RETURN_HOME)
            self._start_operation(self.sequence)
            response.success = True
            response.message = 'One existing HOME move; 15s passive observation'
            return response

    def sequence(self, operation_id):
        result = dict(target_rad=list(self.home_pose), arrival_verified=False)
        try:
            self.phase = 'home'
            self.record('event', dict(event='requested'))
            try:
                self._send_joint_pose(self.home_pose, operation_id, 'HOME')
                result['arrival_verified'] = True
            except ArmArrivalUnverified as exc:
                result['arrival_error'] = str(exc)
                self._set_state(TaskState.ERROR, operation_id)
                self.get_logger().error(str(exc))
            self.phase = 'observe'
            self.record('event', dict(event='observation_started', **result))
            self._sleep_cancelable(15.0, operation_id)
            self._ensure_active(operation_id)
            result['final_rad'] = list(self._current_joint_positions)
            self._set_state(TaskState.IDLE if result['arrival_verified']
                            else TaskState.ERROR, operation_id)
        except OperationCancelled:
            result['error'] = 'Cancelled'
        except Exception as exc:
            result['error'] = str(exc)
            self._fail_operation(operation_id, str(exc), auto_home=False)
        finally:
            self.phase = 'done'
            self.record('event', dict(event='finished', **result))
            (self.directory / 'result.json').write_text(
                json.dumps(result, indent=2) + '\n')
            with self._lock:
                self._preparing = False


def main():
    rclpy.init()
    node = HomeTrackingControl()
    executor = MultiThreadedExecutor(num_threads=4)
    executor.add_node(node)
    try:
        executor.spin()
    except KeyboardInterrupt:
        node._handle_emergency_stop('process interrupt')
    finally:
        executor.shutdown()
        node.journal.close()
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()


if __name__ == '__main__':
    main()
