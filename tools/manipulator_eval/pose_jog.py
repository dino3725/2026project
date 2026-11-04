#!/usr/bin/env python3
"""
측정 전용 도구 - control_node를 거치지 않음 - 평상시 사용 금지.

MEASUREMENT ONLY. Jog the arm joints with the keyboard to find a camera
pose where every object is detected well, and save candidate poses. Run
it next to live_detect.py. The gripper is never commanded.

Keys (terminal focus):
  q / a   joint1 + / -   (turn left / right)
  w / s   joint2 + / -   (upper arm forward-down / back-up)
  e / d   joint3 + / -   (forearm down / up)
  r / f   joint4 + / -   (wrist: camera looks more down / more up)
  1 2 3   step size 1 / 2 / 5 degrees
  g       go to RECOGNITION pose from control.yaml (4 s)
  h       go to HOME pose from control.yaml (4 s)
  m       save the current pose (asks for a note)
  space   cancel the current motion and stop this tool
  x       quit

Safety layers (shared with arm_probe.py):
  * control_node must be running and IDLE. Y, voice ``stop``, Ctrl+C or
    control_node leaving IDLE cancel the motion and lock this tool.
    Recover with X (control_node HOME).
  * Every target and the joint-space path to it are checked first: URDF
    limits, hand and camera at least 50 mm above the surface, and above
    the robot body. Motion is slow (<= 10 deg/s per step).

    python3 tools/manipulator_eval/pose_jog.py --surface-z 0.070
"""

import argparse
import datetime
import math
import select
import sys
import termios
import threading
import time
import tty
from typing import Optional, Sequence, Tuple

from action_msgs.msg import GoalStatus
from builtin_interfaces.msg import Duration
from control_msgs.action import FollowJointTrajectory
import rclpy
from rclpy.action import ActionClient
from trajectory_msgs.msg import JointTrajectoryPoint

from arm_probe import ArmProbe, load_control_params
from baseline_math import (
    checked_min_clearance_m,
    forward_kinematics,
    jog_target_problems,
    MIN_CLEARANCE_FLOOR_MM,
)
from ros_common import (
    append_csv_row,
    ARM_JOINTS,
    default_csv_path,
    fmt,
    shutdown,
    spin_in_background,
)

BANNER = ('*** pose_jog: 측정 전용 - control_node를 거치지 않음 - '
          '평상시 사용 금지 ***')
KEYS = {'q': (0, 1), 'a': (0, -1), 'w': (1, 1), 's': (1, -1),
        'e': (2, 1), 'd': (2, -1), 'r': (3, 1), 'f': (3, -1)}
STEPS = {'1': 1.0, '2': 2.0, '3': 5.0}
MAX_STEP_SPEED_DEG_S = 10.0
FIXED_POSE_SEC = 4.0
PATH_SAMPLES = 12
FIELDS = (['timestamp', 'label']
          + [f'{j}_deg' for j in ARM_JOINTS]
          + [f'cmd_{j}_deg' for j in ARM_JOINTS]
          + ['ee_x', 'ee_y', 'ee_z', 'cam_x', 'cam_y', 'cam_z',
             'cam_pitch_deg', 'note'])


def path_problems(start: Sequence[float], target: Sequence[float],
                  surface_z: float, clearance: float) -> list:
    """Check the target and the straight joint-space path to it."""
    for index in range(1, PATH_SAMPLES + 1):
        t = index / PATH_SAMPLES
        sample = [a + (b - a) * t for a, b in zip(start, target)]
        problems = jog_target_problems(sample, surface_z, clearance)
        if problems:
            where = 'target' if index == PATH_SAMPLES else \
                f'path at {t * 100:.0f}%'
            return [f'{where}: {p}' for p in problems]
    return []


class PoseJog(ArmProbe):
    """ArmProbe stop handling plus direct joint trajectory goals."""

    def __init__(self, params: dict) -> None:
        super().__init__(params)
        self.trajectory_client = ActionClient(
            self, FollowJointTrajectory,
            '/arm_controller/follow_joint_trajectory')

    def measured_deg(self) -> Optional[list]:
        """Return measured arm joints in degrees, or None."""
        joints = self.joints()
        if not all(name in joints for name in ARM_JOINTS):
            return None
        return [math.degrees(joints[name]) for name in ARM_JOINTS]

    def send_joints(self, target_deg: Sequence[float],
                    duration: float) -> Tuple[bool, str]:
        """Move joint1-4 to ``target_deg`` over ``duration`` seconds."""
        refusal = self._check_ready()
        if refusal:
            return False, refusal
        if not self.trajectory_client.wait_for_server(timeout_sec=3.0):
            return False, 'arm trajectory server not available'
        goal = FollowJointTrajectory.Goal()
        goal.trajectory.joint_names = list(ARM_JOINTS)
        point = JointTrajectoryPoint()
        point.positions = [math.radians(v) for v in target_deg]
        whole = int(duration)
        point.time_from_start = Duration(
            sec=whole, nanosec=int((duration - whole) * 1e9))
        goal.trajectory.points.append(point)

        self.moved = True
        self._moving = True
        try:
            send_future = self.trajectory_client.send_goal_async(goal)
            if not self._wait(send_future, 5.0):
                return False, f'not sent ({self.abort_reason or "timeout"})'
            handle = send_future.result()
            if not handle.accepted:
                return False, 'goal rejected'
            with self._goal_lock:
                self._goal_handle = handle
            if self.abort_event.is_set():
                handle.cancel_goal_async()
                return False, f'stopped: {self.abort_reason}'
            result_future = handle.get_result_async()
            if not self._wait(result_future, duration + 8.0):
                handle.cancel_goal_async()
                return False, f'stopped: {self.abort_reason or "timeout"}'
            response = result_future.result()
        finally:
            with self._goal_lock:
                self._goal_handle = None
            self._moving = False
        if response.status != GoalStatus.STATUS_SUCCEEDED or \
                response.result.error_code != 0:
            return False, (f'failed: status={response.status}, '
                           f'error={response.result.error_code}')
        return True, 'done'


def describe(joints_deg: Sequence[float], surface_z: float) -> str:
    """One status line: joints, camera pitch, heights above the surface."""
    points = forward_kinematics(joints_deg)
    joints = ', '.join(f'{v:6.1f}' for v in joints_deg)
    return (f'joints [{joints}]° | camera pitch {points.camera_pitch_deg:4.0f}°'
            f' z {points.camera[2]:.3f} | hand x {points.ee[0]:.3f} '
            f'z {points.ee[2]:.3f} (surface +'
            f'{(points.ee[2] - surface_z) * 1000:.0f} mm)')


def save_pose(node: PoseJog, args, command: Sequence[float],
              note: str) -> None:
    """Append the measured pose and its FK to the poses CSV."""
    measured = node.measured_deg() or list(command)
    points = forward_kinematics(measured)
    row = {
        'timestamp': datetime.datetime.now().isoformat(timespec='seconds'),
        'label': f'P{datetime.datetime.now():%H%M%S}',
        'note': note,
        'cam_pitch_deg': fmt(points.camera_pitch_deg, 1),
    }
    for name, value, cmd in zip(ARM_JOINTS, measured, command):
        row[f'{name}_deg'] = fmt(value, 2)
        row[f'cmd_{name}_deg'] = fmt(cmd, 2)
    for prefix, xyz in (('ee', points.ee), ('cam', points.camera)):
        for axis, value in zip('xyz', xyz):
            row[f'{prefix}_{axis}'] = fmt(value)
    append_csv_row(args.out, FIELDS, row)
    print(f'\r\n  saved {row["label"]} -> {args.out}\r\n', flush=True)


def read_key(timeout: float) -> str:
    """Return one key pressed within ``timeout`` seconds, or ''."""
    ready, _, _ = select.select([sys.stdin], [], [], timeout)
    return sys.stdin.read(1) if ready else ''


def run(node: PoseJog, args, params: dict, clearance: float) -> None:
    """Run the keyboard loop (stdin in cbreak mode)."""
    recognition = [float(v) for v in params['recognition_pose_deg']]
    home = [float(v) for v in params['home_pose_deg']]
    command = node.measured_deg()
    if command is None:
        print('No /joint_states. Is the hardware bringup running?')
        return
    step = 2.0
    print(__doc__.split('Keys (terminal focus):')[1].split('Safety')[0])
    print(describe(command, args.surface_z), flush=True)
    worker: Optional[threading.Thread] = None
    state = {'busy': False, 'command': command}

    def move(target, duration):
        state['busy'] = True
        ok, message = node.send_joints(target, duration)
        if ok:
            state['command'] = list(target)
        else:
            state['command'] = node.measured_deg() or state['command']
        now = node.measured_deg() or target
        print(f'\r  {message}\r\n  {describe(now, args.surface_z)}\r',
              flush=True)
        state['busy'] = False

    old = termios.tcgetattr(sys.stdin)
    tty.setcbreak(sys.stdin.fileno())
    try:
        while rclpy.ok():
            if node.abort_event.is_set():
                print(f'\r\n정지됨({node.abort_reason}). 이 도구는 더 움직이지 '
                      '않습니다. X 버튼으로 HOME 복귀하세요.\r', flush=True)
                break
            key = read_key(0.05)
            if not key:
                continue
            if key == 'x':
                break
            if key == ' ':
                node.abort('space (cancel)')
                continue
            if key in STEPS:
                step = STEPS[key]
                print(f'\r  step {step:.0f}°\r', flush=True)
                continue
            if state['busy']:
                print('\r  이동 중입니다. 끝난 뒤 누르세요.\r', flush=True)
                continue
            if key == 'm':
                termios.tcsetattr(sys.stdin, termios.TCSADRAIN, old)
                try:
                    note = input('\n  메모 (예: vaseline 0.8 coke 0.9): ')
                finally:
                    tty.setcbreak(sys.stdin.fileno())
                save_pose(node, args, state['command'], note.strip())
                continue
            start = node.measured_deg() or state['command']
            if key in KEYS:
                index, sign = KEYS[key]
                target = list(state['command'])
                target[index] += sign * step
                duration = max(0.6, step / MAX_STEP_SPEED_DEG_S)
            elif key in ('g', 'h'):
                target = recognition if key == 'g' else home
                duration = FIXED_POSE_SEC
            else:
                continue
            problems = path_problems(start, target, args.surface_z,
                                     clearance)
            if problems:
                print('\r  거부: ' + '; '.join(problems[:2]) + '\r',
                      flush=True)
                continue
            worker = threading.Thread(target=move, args=(target, duration),
                                      daemon=True)
            worker.start()
    finally:
        termios.tcsetattr(sys.stdin, termios.TCSADRAIN, old)
        if worker is not None and worker.is_alive():
            node.abort('quit')
            worker.join(timeout=3.0)


def main() -> None:
    parser = argparse.ArgumentParser(description='pose_jog (measurement only)')
    parser.add_argument('--surface-z', type=float, default=0.070,
                        help='surface top in base_link (8 cm box = 0.070, '
                             'floor = -0.010); stricter is safer')
    parser.add_argument('--min-clearance-mm', type=float,
                        default=MIN_CLEARANCE_FLOOR_MM)
    parser.add_argument('--control-config', default=None)
    parser.add_argument('--out', default=default_csv_path('poses'))
    args = parser.parse_args()
    try:
        clearance = checked_min_clearance_m(args.min_clearance_mm)
    except ValueError as exc:
        parser.error(str(exc))
    if not sys.stdin.isatty():
        parser.error('run this in an interactive terminal')

    params = load_control_params(args.control_config)
    print(BANNER)
    rclpy.init()
    node = PoseJog(params)
    executor, thread = spin_in_background(node)
    try:
        time.sleep(1.5)
        print(f'control_node 상태: {node.control_state or "(없음)"} | '
              f'표면 z={args.surface_z:.3f} m, 최소 간격 '
              f'{clearance * 1000:.0f} mm | 그리퍼 명령 없음 | 저장={args.out}')
        if node.control_state != 'IDLE':
            print('control_node가 IDLE이 아니면 움직이지 않습니다.')
        run(node, args, params, clearance)
    except KeyboardInterrupt:
        node.abort('Ctrl+C')
        time.sleep(0.3)
    finally:
        print('\n끝. 팔을 HOME으로: h 키(종료 전) 또는 Y -> X.')
        shutdown(node, executor, thread)


if __name__ == '__main__':
    main()
