"""Five explicit object trials; observe arrival errors without accepting them."""

import json
import math
from pathlib import Path
import statistics
import time

import rclpy
from rclpy.executors import MultiThreadedExecutor
from rclpy.parameter import Parameter
from rclpy.time import Time
from std_srvs.srv import Trigger

from control_pkg.arm_arrival import joint_arrival_error
from control_pkg.control_node import ArmArrivalUnverified, OperationCancelled
from control_pkg.task_state import TaskState
from measurement_control import MeasurementControl


def summarize_pose(expected, samples):
    """Keep signed bias and absolute error separate, in joint order."""
    if len(expected) != 4 or not samples or not all(
            len(row) == 4 and all(math.isfinite(x) for x in row)
            for row in [expected, *samples]):
        raise ValueError('Finite four-joint samples are required')
    errors = [[math.degrees(a - b) for a, b in zip(row, expected)]
              for row in samples]
    return dict(
        target_deg=list(map(math.degrees, expected)), samples_rad=samples,
        mean_error_deg=[statistics.mean(row[i] for row in errors)
                        for i in range(4)],
        mean_absolute_error_deg=[statistics.mean(abs(row[i]) for row in errors)
                                 for i in range(4)],
        max_absolute_error_deg=[max(abs(row[i]) for row in errors)
                                for i in range(4)])


class ObjectPoseTrials(MeasurementControl):
    """Use existing motion methods; stop, stale data and action failures block."""

    arrival_observation_only = True

    def __init__(self):
        self.trial = 0
        super().__init__()
        self.declare_parameter('trial_output', '')
        self.declare_parameter('trial_target', 'coke')
        self.trial_target = self.get_parameter('trial_target').value
        if self.trial_target not in self.supported_targets:
            raise ValueError('Unsupported trial target')
        path = self.get_parameter('trial_output').value
        if not path:
            raise ValueError('A new trial_output directory is required')
        self.output = Path(path)
        self.output.mkdir(parents=True, exist_ok=False)
        self.journal = (self.output / 'events.jsonl').open('x', buffering=1)
        for name, callback in (
                ('start', self.start_trial), ('release', self.release_trial),
                ('recover_home', self.recover_home)):
            self.create_service(Trigger, '/trial/' + name, callback,
                                callback_group=self.callback_group)
        self.get_logger().warn(
            'TRIAL ONLY: arrival deviations recorded, not blocking; '
            'stop/stale inputs/action failures still block. Base disabled.')

    def _read_parameters(self):
        super()._read_parameters()
        self.pick_enabled = True
        self.set_parameters([Parameter('pick_enabled', value=True)])

    def record(self, kind, **data):
        with self._lock:
            self.journal.write(json.dumps(dict(
                trial=self.trial, kind=kind, time=time.time(), **data)) + '\n')

    def prepare(self, request, response):
        response.success = False
        response.message = 'Use explicit trial services only'
        return response

    def request(self, response, state, sequence):
        with self._lock:
            reason = self.blocked_reason()
            if self._preparing or self.state != state:
                reason = 'Busy or wrong state'
            if self._stop_event.is_set():
                reason = 'Stopped; do not restart automatically'
            if reason:
                response.success = False
                response.message = reason
                return response
            self._preparing = True
            self._start_operation(sequence)
            response.success = True
            response.message = 'Explicit trial operation accepted'
            return response

    def start_trial(self, request, response):
        if self.trial >= 5:
            response.success = False
            response.message = 'Five attempts already recorded'
            return response
        return self.request(response, TaskState.IDLE, self.trial_sequence)

    def release_trial(self, request, response):
        return self.request(response, TaskState.DRIVE_TO_PLACE,
                            self.release_sequence)

    def recover_home(self, request, response):
        return self.request(response, TaskState.ERROR, self.recovery_sequence)

    def _verify_arm_arrival(self, expected, operation_id, label, point=None,
                            joint_tolerances=None):
        self._sleep_cancelable(0.5, operation_id)
        samples, stamps, tool_errors = [], set(), []
        started = time.monotonic()
        while time.monotonic() - started < 1.0:
            self._ensure_active(operation_id)
            with self._lock:
                actual = self._current_joint_positions
                stamp = self._joint_stamp_ns
                receipt_age = time.monotonic() - self._joint_received_at
            age = (self.get_clock().now().nanoseconds - stamp) / 1e9
            if (receipt_age > self.arrival_max_age or stamp <= 0
                    or not 0 <= age <= self.arrival_max_age
                    or actual is None or len(actual) != 4
                    or not all(math.isfinite(x) for x in actual)):
                raise ArmArrivalUnverified('Trial stopped: invalid/stale joints')
            if stamp not in stamps:
                samples.append(list(actual))
                stamps.add(stamp)
            if point is not None:
                transform = self.tf_buffer.lookup_transform(
                    self.base_frame, self.end_effector_link, Time())
                ts = (transform.header.stamp.sec * 1_000_000_000
                      + transform.header.stamp.nanosec)
                tf_age = (self.get_clock().now().nanoseconds - ts) / 1e9
                if not 0 <= tf_age <= self.arrival_max_age:
                    raise ArmArrivalUnverified('Trial stopped: stale tool TF')
                xyz = transform.transform.translation
                error = math.dist((xyz.x, xyz.y, xyz.z), point)
                if not math.isfinite(error):
                    raise ArmArrivalUnverified('Trial stopped: nonfinite tool TF')
                tool_errors.append(error * 1000)
            self._sleep_cancelable(0.05, operation_id)
        if len(samples) < 5:
            raise ArmArrivalUnverified('Trial stopped: insufficient fresh samples')
        limits = (self.arrival_joint_tolerance if joint_tolerances is None
                  else joint_tolerances)
        nominal_pass = all(not joint_arrival_error(row, expected, limits)
                           for row in samples)
        if tool_errors and max(tool_errors) > self.position_tolerance * 1000:
            nominal_pass = False
        self.record('pose', label=label, nominal_pass=nominal_pass,
                    tool_error_mm=tool_errors, **summarize_pose(expected, samples))
        self.get_logger().warn(
            f'Trial {self.trial} {label}: observed only; '
            f'mean error deg={summarize_pose(expected, samples)["mean_error_deg"]}')

    def trial_sequence(self, operation_id):
        self.trial += 1
        self.record('started')
        try:
            self.current_target_class = self.trial_target
            self._publish_target()
            self._set_state(TaskState.RETURN_HOME, operation_id)
            self._send_joint_pose(self.home_pose, operation_id, 'HOME')
            self._set_state(TaskState.MOVE_TO_RECOGNITION, operation_id)
            self._send_joint_pose(self.recognition_pose, operation_id, 'RECOGNITION')
            self._sleep_cancelable(self.recognition_settle, operation_id)
            self._set_state(TaskState.VISION_SEARCH, operation_id)
            result = self._find_object(operation_id)
            point = (result.x, result.y, result.z)
            if not all(math.isfinite(x) for x in point):
                raise ValueError('Nonfinite object position')
            self.record('object', target=self.trial_target, xyz_m=point,
                        source='YOLO')
            pick = self._add_xyz(point, self.pick_calibration)
            pregrasp = self._add_xyz(pick, self.pregrasp_offset)
            self._set_state(TaskState.PICKING, operation_id)
            self._send_gripper(self.gripper_open, operation_id, 'OPEN')
            self._send_cartesian_pose(pregrasp, operation_id, 'PREGRASP')
            self._send_cartesian_pose(pick, operation_id, 'PICK')
            self._send_gripper(self.gripper_closed, operation_id,
                               'CLOSE', accept_stall=True)
            self.record('grasp_confirmed')
            self._send_vertical_retreat(operation_id)
            self._set_state(TaskState.MOVE_TO_CARGO, operation_id)
            self._send_joint_pose(self.cargo_pose, operation_id, 'CARGO')
            self._set_state(TaskState.DRIVE_TO_PLACE, operation_id)
            self.record('awaiting_release')
        except OperationCancelled:
            self.record('cancelled')
        except Exception as exc:
            self.record('failed', error=str(exc))
            self._fail_operation(operation_id, str(exc), auto_home=False)
        finally:
            with self._lock:
                self._preparing = False

    def release_sequence(self, operation_id):
        try:
            self._set_state(TaskState.PLACING, operation_id)
            self._send_gripper(self.gripper_open, operation_id, 'OPEN')
            self._sleep_cancelable(self.release_settle, operation_id)
            self._send_joint_pose(self.home_pose, operation_id, 'HOME')
            self._set_state(TaskState.IDLE, operation_id)
            self.record('finished')
        except OperationCancelled:
            self.record('cancelled')
        except Exception as exc:
            self.record('failed', error=str(exc))
            self._fail_operation(operation_id, str(exc), auto_home=False)
        finally:
            with self._lock:
                self._preparing = False

    def recovery_sequence(self, operation_id):
        try:
            self._send_joint_pose(self.home_pose, operation_id, 'HOME_RECOVERY')
            self._set_state(TaskState.IDLE, operation_id)
            self.record('recovered')
        except OperationCancelled:
            self.record('cancelled')
        except Exception as exc:
            self.record('failed', error=str(exc))
            self._fail_operation(operation_id, str(exc), auto_home=False)
        finally:
            with self._lock:
                self._preparing = False


def main():
    rclpy.init()
    node = ObjectPoseTrials()
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
