"""Supervise sequential Jetson bringup in one foreground SSH terminal."""

import argparse
from datetime import datetime
import fcntl
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time


WORKSPACE = Path(__file__).resolve().parents[2]
MANAGED_NODES = {
    'controller_manager', 'robot_state_publisher', 'move_group',
    'd435i', 'vision_node2', 'vision_node', 'hand_gesture_node',
    'joy_node', 'total_control_node', 'control_node',
    'measurement_control', 'calibration_control',
    'd435i_mount_tf_publisher', 'eated_auto_monitor',
}


def stages(audit_directory=None, reviewed_approach_plan=None):
    """Keep manual-drive startup after control has finished HOME."""
    commands = [
        ('joy', ['ros2', 'run', 'joy', 'joy_node', '--ros-args',
                 '-p', 'device_id:=0', '-p', 'deadzone:=0.05',
                 '-p', 'autorepeat_rate:=20.0']),
        ('hardware', ['sg', 'dialout', '-c',
                      str(WORKSPACE / 'eated_hardware.sh')]),
        ('moveit', [str(WORKSPACE / 'eated_moveit.sh')]),
        ('camera', ['ros2', 'launch', 'realsense2_camera', 'rs_launch.py',
                    'camera_namespace:=realsense', 'camera_name:=d435i',
                    'enable_color:=true', 'enable_depth:=true',
                    'align_depth.enable:=true', 'pointcloud.enable:=false']),
        ('vision', ['ros2', 'launch', 'vision_pkg', 'vision2.launch.py',
                    f'model_path:={WORKSPACE / "best.pt"}']),
        ('control', ['ros2', 'launch', 'control_pkg', 'control.launch.py']),
        ('joystick', ['ros2', 'run', 'joystick_pkg', 'total_control']),
    ]

    if audit_directory is not None:
        for name, command in commands:
            if name == 'vision':
                command.extend([
                    f'observation_output_directory:={audit_directory / "observations"}',
                    'localization_mode:=legacy'])
            if name == 'control':
                command.append(f'config_file:={audit_directory / "control_audit.yaml"}')
    if reviewed_approach_plan is not None:
        import hashlib
        path = Path(reviewed_approach_plan).resolve()
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        commands = [(name, [sys.executable,
                     str(WORKSPACE / 'tools/vision_eval/high_approach_control.py'),
                     '--ros-args', '--params-file',
                     str(WORKSPACE / 'src/control_pkg/config/control.yaml'),
                     '-p', f'reviewed_plan_path:={path}',
                     '-p', f'reviewed_plan_sha256:={digest}'] if name == 'control' else command)
                    for name, command in commands]
    return commands


def write_audit_config(directory):
    """Disable Pick for operator-triggered recognition and leave all poses intact."""
    import yaml
    source = WORKSPACE / 'src/control_pkg/config/control.yaml'
    config = yaml.safe_load(source.read_text())
    config['control_node']['ros__parameters']['pick_enabled'] = False
    (directory / 'control_audit.yaml').write_text(yaml.safe_dump(config))


def child_environment(name):
    """Use project vision libraries only for the vision process."""
    env = os.environ.copy()
    env.pop('DISPLAY', None)
    env.pop('WAYLAND_DISPLAY', None)
    if name == 'vision':
        extra = str(WORKSPACE / '.venv/lib/python3.10/site-packages')
        env['PYTHONPATH'] = extra + os.pathsep + env.get('PYTHONPATH', '')
    return env


class Processes:
    """Own child process groups and never signal unrelated robot processes."""

    def __init__(self, directory):
        self.directory = directory
        self.children = {}
        self.logs = []

    def start(self, name, command):
        log = (self.directory / f'{name}.log').open('w')
        self.logs.append(log)
        self.children[name] = subprocess.Popen(
            command, cwd=WORKSPACE, env=child_environment(name),
            stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
            start_new_session=True,
        )

    def check(self):
        for name, process in self.children.items():
            code = process.poll()
            if code is not None:
                raise RuntimeError(
                    f'{name} 종료(code={code}): {self.directory / name}.log'
                )

    def interrupt(self, name):
        """Request exit without delaying cancellation of active arm goals."""
        process = self.children.get(name)
        if process is not None:
            try:
                os.killpg(process.pid, signal.SIGINT)
            except ProcessLookupError:
                pass

    def stop(self, name):
        process = self.children.pop(name, None)
        if process is None:
            return
        # Signal the entire group even if the ros2/sg parent exited first.
        for sig, timeout in ((signal.SIGINT, 10), (signal.SIGTERM, 3),
                             (signal.SIGKILL, 1)):
            try:
                os.killpg(process.pid, sig)
            except ProcessLookupError:
                break
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                process.poll()  # Reap the parent so it cannot hold the group.
                try:
                    os.killpg(process.pid, 0)
                except ProcessLookupError:
                    break
                time.sleep(0.1)
            else:
                continue
            break
        process.wait(timeout=2)

    def close(self):
        # Remove the drive publisher first; keep hardware available for cancel.
        errors = []
        for name in ('joystick', 'control', 'vision', 'camera', 'moveit',
                     'hardware', 'joy'):
            try:
                self.stop(name)
            except Exception as exc:
                errors.append(f'{name}: {exc}')
        for log in self.logs:
            log.close()
        return errors


class Readiness:
    """Observe real ROS data/services without sending any motion goals."""

    def __init__(self, report, telemetry=None):
        import rclpy
        from rclpy.action import ActionClient
        from rclpy.qos import DurabilityPolicy, QoSProfile
        from rclpy.qos import qos_profile_sensor_data
        from control_msgs.action import FollowJointTrajectory, GripperCommand
        from control_msgs.msg import DynamicJointState, JointTrajectoryControllerState
        from controller_manager_msgs.srv import ListControllers
        from eated_interfaces.action import FindObject
        from eated_interfaces.msg import RobotCommand
        from moveit_msgs.action import ExecuteTrajectory, MoveGroup
        from moveit_msgs.srv import GetCartesianPath
        from sensor_msgs.msg import CameraInfo, Image, JointState, Joy
        from std_msgs.msg import String

        self.rclpy = rclpy
        self.command_type = RobotCommand
        self.node = rclpy.create_node('eated_auto_monitor')
        self.report = report
        self.telemetry = telemetry
        self.received = {}
        self.state = None
        self.controller_future = None
        self.controllers = set()
        self.last_controller_query = 0.0
        self.controller_client = self.node.create_client(
            ListControllers, '/controller_manager/list_controllers')
        self.controller_request = ListControllers.Request
        self.actions = {
            'arm': ActionClient(self.node, FollowJointTrajectory,
                                '/arm_controller/follow_joint_trajectory'),
            'gripper': ActionClient(self.node, GripperCommand,
                                    '/gripper_controller/gripper_cmd'),
            'move': ActionClient(self.node, MoveGroup, '/move_action'),
            'execute': ActionClient(self.node, ExecuteTrajectory,
                                    '/execute_trajectory'),
            'find': ActionClient(self.node, FindObject, '/find_object'),
        }
        self.cartesian = self.node.create_client(
            GetCartesianPath, '/compute_cartesian_path')
        self.stop_publisher = self.node.create_publisher(
            RobotCommand, '/voice/robot_command', 10)
        for key, topic, msg_type in (
            ('joy', '/joy', Joy), ('joints', '/joint_states', JointState),
            ('rgb', '/realsense/d435i/color/image_raw', Image),
            ('depth', '/realsense/d435i/aligned_depth_to_color/image_raw',
             Image),
            ('info', '/realsense/d435i/color/camera_info', CameraInfo),
        ):
            self.node.create_subscription(
                msg_type, topic, lambda msg, key=key: self.receive(key, msg),
                qos_profile_sensor_data)
        state_qos = QoSProfile(depth=1)
        state_qos.durability = DurabilityPolicy.TRANSIENT_LOCAL
        self.node.create_subscription(String, '/control/state',
                                      self.on_state, state_qos)
        self.node.create_subscription(RobotCommand, '/voice/robot_command',
                                      self.on_command, 10)
        if telemetry is not None:
            self.node.create_subscription(
                JointTrajectoryControllerState, '/arm_controller/controller_state',
                self.on_controller, qos_profile_sensor_data)
            self.node.create_subscription(
                DynamicJointState, '/dynamic_joint_states',
                self.on_dynamic, qos_profile_sensor_data)

    def on_controller(self, message):
        self.telemetry(dict(
            kind='controller', state=self.state, names=list(message.joint_names),
            desired=list(message.desired.positions),
            actual=list(message.actual.positions),
            velocity=list(message.actual.velocities),
            error=list(message.error.positions)))

    def on_dynamic(self, message):
        if 'battery' in message.joint_names:
            value = message.interface_values[message.joint_names.index('battery')]
            self.telemetry(dict(kind='battery', state=self.state,
                                **dict(zip(value.interface_names, value.values))))

    def receive(self, key, message):
        self.received[key] = (time.monotonic(), message)

    def fresh(self, key):
        return (key in self.received
                and time.monotonic() - self.received[key][0] < 1.0)

    def on_state(self, message):
        self.state = message.data
        self.report(f'STATE: {message.data}')

    def on_command(self, message):
        self.report(f'COMMAND: {message.action} {message.target_class}')

    def spin(self):
        self.rclpy.spin_once(self.node, timeout_sec=0.1)

    def nodes(self):
        return {name for name, _ in self.node.get_node_names_and_namespaces()}

    def ready(self, name):
        if name == 'joy':
            if not self.fresh('joy'):
                return False
            msg = self.received['joy'][1]
            return (len(msg.buttons) >= 4 and len(msg.axes) >= 2
                    and not any(msg.buttons)
                    and abs(msg.axes[0]) <= 0.08
                    and abs(msg.axes[1]) <= 0.08)
        if name == 'hardware':
            if self.controller_future is not None:
                if self.controller_future.done():
                    response = self.controller_future.result()
                    self.controllers = {
                        c.name for c in response.controller
                        if c.state == 'active'
                    }
                    self.controller_future = None
            if (self.controller_future is None
                    and self.controller_client.service_is_ready()
                    and time.monotonic() - self.last_controller_query > 1):
                self.last_controller_query = time.monotonic()
                self.controller_future = self.controller_client.call_async(
                    self.controller_request())
            return (
                {'arm_controller', 'gripper_controller',
                 'joint_state_broadcaster', 'diff_drive_controller'}
                <= self.controllers
                and self.fresh('joints')
                and {'joint1', 'joint2', 'joint3', 'joint4'}
                <= set(self.received['joints'][1].name)
                and self.actions['arm'].server_is_ready()
                and self.actions['gripper'].server_is_ready()
            )
        if name == 'moveit':
            return (self.actions['move'].server_is_ready()
                    and self.actions['execute'].server_is_ready()
                    and self.cartesian.service_is_ready())
        if name == 'camera':
            return all(self.fresh(key) for key in ('rgb', 'depth', 'info'))
        if name == 'vision':
            return (self.actions['find'].server_is_ready()
                    and {'vision_node2', 'hand_gesture_node',
                         'd435i_mount_tf_publisher'} <= self.nodes())
        if name == 'control':
            if self.state == 'ERROR':
                raise RuntimeError('HOME 중 ERROR/정지 발생: 자동 진행을 중단합니다.')
            return self.state == 'IDLE'
        if name == 'joystick':
            return 'total_control_node' in self.nodes()
        raise ValueError(name)

    def stop_control(self):
        message = self.command_type()
        message.action = 'stop'
        message.transcript = 'eateded shutdown'
        message.header.stamp = self.node.get_clock().now().to_msg()
        # Reliable publisher already discovered during startup. A flood of
        # stops repeatedly replaces the arm hold target with measured error.
        # Send once and allow delivery/cancellation callbacks to finish.
        self.stop_publisher.publish(message)
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            self.spin()


def wait_ready(name, observer, processes, timeout):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        processes.check()
        observer.spin()
        if name != 'joy' and not observer.fresh('joy'):
            raise RuntimeError('조이스틱 입력이 끊겼습니다. 자동 진행 중단.')
        if observer.ready(name):
            return
    raise RuntimeError(f'{name}: {timeout:g}초 안에 준비되지 않았습니다.')


def preflight(observer):
    """Reject concurrent manual launchers and verify the calibrated model."""
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        observer.spin()
    conflicts = (observer.nodes() - {'eated_auto_monitor'}) & MANAGED_NODES
    if conflicts:
        raise RuntimeError(f'기존 ROS 실행을 먼저 종료하세요: {sorted(conflicts)}')
    for path in ('best.pt', 'install/setup.bash',
                 '.venv/lib/python3.10/site-packages'):
        if not (WORKSPACE / path).exists():
            raise RuntimeError(f'필수 파일 없음: {WORKSPACE / path}')
    for path in ('/dev/input/js0', '/dev/ttyACM0'):
        if not Path(path).exists():
            raise RuntimeError(f'장치 없음: {path}')
    subprocess.run(
        ['/usr/bin/python3',
         str(WORKSPACE / 'tools/vision_eval/ensure_calibrated_model.py'),
         '--directory', str(WORKSPACE / 'log/measured_model_20261111')],
        check=True, timeout=30,
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dry-run', action='store_true',
                        help='실행 순서만 출력; ROS/하드웨어를 시작하지 않음')
    parser.add_argument('--timeout', type=float, default=90,
                        help='단계별 준비 제한 시간(초, 기본 90)')
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--recognition-audit', action='store_true',
                        help='인식 기록만 수행: Pick 비활성, HOME/인식 자세 이동은 있음')
    mode.add_argument('--reviewed-approach-plan', type=Path,
                      help='검토한 접근 전용 계획: 시작 시 대기, 명시적 서비스로 1회 실행')
    args = parser.parse_args()
    if not 0 < args.timeout <= 600:
        parser.error('--timeout must be between 0 and 600 seconds')
    if args.dry_run:
        import shlex
        audit = WORKSPACE / 'log/AUDIT_RUN' if args.recognition_audit else None
        for name, command in stages(audit, args.reviewed_approach_plan):
            print(f'{name}: {shlex.join(command)}')
        print('command/state: 위 터미널과 monitor.log에 통합 표시')
        print('control: 접근 서비스 대기' if args.reviewed_approach_plan else
              '주의: 실제 실행 시 control 단계에서 기존 HOME 동작 실행')
        print('주의: 공급 하드웨어 드라이버는 종료 시에도 자체 복귀/집게 초기화 수행')
        return 0

    (WORKSPACE / 'log').mkdir(exist_ok=True)
    lock = (WORKSPACE / 'log/eateded.lock').open('w')
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print('eateded가 이미 실행 중입니다.', file=sys.stderr)
        return 1
    directory = (WORKSPACE / 'log'
                 / datetime.now().strftime('eateded_%Y%m%d_%H%M%S_%f'))
    directory.mkdir()
    if args.recognition_audit:
        write_audit_config(directory)
    processes = Processes(directory)
    observer = None
    result = 0
    interrupted = False
    received_signal = None

    def interrupt(signum, frame):
        nonlocal interrupted, received_signal
        if not interrupted:
            received_signal = signal.Signals(signum).name
            interrupted = True
            raise KeyboardInterrupt

    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, interrupt)

    with (directory / 'monitor.log').open('w', buffering=1) as monitor, \
            (directory / 'telemetry.jsonl').open('w', buffering=1) as telemetry:
        def report(message):
            line = f'[{datetime.now():%H:%M:%S}] {message}'
            monitor.write(line + '\n')
            try:
                print(line, flush=True)
            except (BrokenPipeError, OSError):
                pass  # SSH may have disconnected; cleanup must still run.

        try:
            import rclpy
            from rclpy.signals import SignalHandlerOptions
            rclpy.init(signal_handler_options=SignalHandlerOptions.NO)
            started = time.monotonic()

            def record(data):
                telemetry.write(json.dumps(dict(
                    elapsed=time.monotonic() - started, **data)) + '\n')

            observer = Readiness(report, record)
            report(f'감독 프로세스 pid={os.getpid()}, ppid={os.getppid()}, '
                   f'sid={os.getsid(0)}')
            report(f'로그: {directory}')
            report('접근 전용 실행: 제어기는 이동 없이 서비스 요청을 기다립니다.'
                   if args.reviewed_approach_plan else
                   '자동 실행: 팔이 HOME으로 움직입니다. 주변을 비우고 F710을 준비하세요.')
            report('공급 드라이버는 종료 시에도 자체 복귀/집게 초기화를 수행합니다.')
            preflight(observer)
            if args.recognition_audit:
                report('인식 기록 모드: 물체 인식 후 집지 않고 HOME으로 복귀합니다.')
            for name, command in stages(directory if args.recognition_audit else None,
                                        args.reviewed_approach_plan):
                # Do not start HOME with stale or pressed joystick inputs.
                if name == 'control':
                    wait_ready('joy', observer, processes, args.timeout)
                    for dependency in (
                            'hardware', 'moveit', 'camera', 'vision'):
                        wait_ready(dependency, observer, processes,
                                   args.timeout)
                report(f'{name}: 시작 / 준비 확인 중')
                processes.start(name, command)
                wait_ready(name, observer, processes, args.timeout)
                report(f'{name}: 준비 완료')
            report('전체 실행 완료. Ctrl+C로 작업 취소 후 종료합니다.')
            while True:
                processes.check()
                observer.spin()
                if not observer.fresh('joy'):
                    raise RuntimeError('조이스틱 입력이 끊겼습니다.')
        except KeyboardInterrupt:
            report(f'종료 요청을 받았습니다. 신호={received_signal or "KeyboardInterrupt"}')
        except Exception as exc:
            result = 1
            report(f'실행 실패: {exc}')
        finally:
            # Ignore repeated termination signals during bounded cleanup.
            interrupted = True
            report('실행한 프로세스를 정리합니다. 하드웨어는 마지막에 종료합니다.')
            try:
                processes.interrupt('joystick')
                if observer is not None and 'control' in processes.children:
                    observer.stop_control()
            except Exception as exc:
                result = 1
                report(f'종료 중 정지 요청 오류: {exc}')
            finally:
                errors = processes.close()
                if errors:
                    result = 1
                    report(f'프로세스 종료 확인 필요: {errors}')
                if observer is not None:
                    observer.node.destroy_node()
                    observer.rclpy.shutdown()
            report('종료 완료.')
    lock.close()
    return result


if __name__ == '__main__':
    sys.exit(main())
