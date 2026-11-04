#!/usr/bin/env python3
"""
Move the hand to listed points and record where it really arrives.

!!! THIS SCRIPT MOVES THE ARM. Only a person at the robot runs it. !!!

It sends the same kind of MoveIt goal as control_node's PREGRASP/PICK
(end_effector_link position in base_link, 1 cm tolerance box, speed and
acceleration scale from control.yaml) and then compares:

  target   - the point that was requested
  FK       - where the robot thinks the hand is (TF from joint encoders)
  physical - where the hand really is (measured with a ruler on the grid)

Safety layers:
  * control_node must be running and IDLE. Its Y emergency stop sends an
    arm hold trajectory that overrides this motion.
  * This script cancels its own goal on Y, a voice ``stop``, Ctrl+C, or
    when control_node leaves IDLE (for example, A was pressed).
  * Every target is checked against a conservative box, planned with
    ``plan_only`` first, and executed only after you press Enter.
  * Speed/acceleration scales above 0.10 are refused.

    python3 tools/manipulator_eval/arm_probe.py
    python3 tools/manipulator_eval/arm_probe.py --targets my_targets.csv
"""

import argparse
import csv
import datetime
import os
import threading
import time
from typing import Optional, Tuple

from action_msgs.msg import GoalStatus
from ament_index_python.packages import get_package_share_directory
from eated_interfaces.msg import RobotCommand
from geometry_msgs.msg import Pose
from moveit_msgs.action import MoveGroup
from moveit_msgs.msg import Constraints, MoveItErrorCodes, PositionConstraint
import rclpy
from rclpy.action import ActionClient
from sensor_msgs.msg import Joy
from shape_msgs.msg import SolidPrimitive
import yaml

from baseline_math import (
    DEFAULT_SURFACE_Z_M,
    grid_mm_to_base,
    mean_point,
    point_error_mm,
    point_spread_mm,
    probe_target_problems,
    ProbeLimits,
)
from ros_common import (
    append_csv_row,
    BASE_FRAME,
    default_csv_path,
    EE_FRAME,
    fmt,
    JOINT_FIELDS,
    joint_columns,
    MeasurementNode,
    next_trial_id,
    quaternion_to_rpy_deg,
    shutdown,
    spin_in_background,
)

HERE = os.path.dirname(os.path.abspath(__file__))
MAX_SCALE = 0.10

FIELDS = (
    ['trial', 'timestamp', 'target_id', 'target_x', 'target_y', 'target_z',
     'result', 'fk_x', 'fk_y', 'fk_z', 'fk_spread_mm',
     'ee_roll_deg', 'ee_pitch_deg', 'ee_yaw_deg',
     'err_fk_x_mm', 'err_fk_y_mm', 'err_fk_z_mm', 'err_fk_xy_mm',
     'err_fk_xyz_mm',
     'phys_x', 'phys_y', 'phys_z',
     'err_phys_x_mm', 'err_phys_y_mm', 'err_phys_z_mm', 'err_phys_xy_mm',
     'err_phys_xyz_mm',
     'mech_x_mm', 'mech_y_mm', 'mech_z_mm', 'mech_xy_mm']
    + JOINT_FIELDS + ['message', 'note']
)


def load_control_params(path: Optional[str]) -> dict:
    """Read control_node parameters so the probe matches the real Pick."""
    if path is None:
        try:
            path = os.path.join(
                get_package_share_directory('control_pkg'),
                'config', 'control.yaml')
        except Exception:
            path = os.path.join(HERE, '..', '..', 'src', 'control_pkg',
                                'config', 'control.yaml')
    with open(path, encoding='utf-8') as handle:
        params = yaml.safe_load(handle)['control_node']['ros__parameters']
    params['_path'] = os.path.abspath(path)
    return params


def load_targets(path: str):
    """Read ``id,x_mm,y_mm,height_mm`` targets."""
    with open(path, newline='', encoding='utf-8') as handle:
        return [
            (row['id'].strip(), float(row['x_mm']), float(row['y_mm']),
             float(row['height_mm']))
            for row in csv.DictReader(handle)
        ]


class ArmProbe(MeasurementNode):
    """Send position-only MoveIt goals that any stop signal cancels."""

    def __init__(self, params: dict) -> None:
        super().__init__('baseline_arm_probe')
        self.params = params
        self.velocity_scale = float(params['moveit_velocity_scale'])
        self.acceleration_scale = float(params['moveit_acceleration_scale'])
        if self.velocity_scale > MAX_SCALE or \
                self.acceleration_scale > MAX_SCALE:
            raise ValueError(
                f'speed/acceleration scale above {MAX_SCALE} is refused')
        self.emergency_index = int(params.get('emergency_button_index', 3))
        self.abort_event = threading.Event()
        self.abort_reason = ''
        self._goal_lock = threading.Lock()
        self._goal_handle = None
        self._moving = False
        self._previous_emergency = False
        self.client = ActionClient(self, MoveGroup, '/move_action')
        self.create_subscription(Joy, '/joy', self._joy_callback, 10)
        self.create_subscription(
            RobotCommand, '/voice/robot_command', self._voice_callback, 10)
        self.add_state_listener(self._on_state)

    # Stop signals -----------------------------------------------------

    def abort(self, reason: str) -> None:
        """Cancel the active MoveIt goal immediately."""
        self.abort_reason = reason
        self.abort_event.set()
        with self._goal_lock:
            handle = self._goal_handle
        if handle is not None:
            handle.cancel_goal_async()
        self.get_logger().warn(f'Probe stopped: {reason}')

    def _joy_callback(self, msg: Joy) -> None:
        index = self.emergency_index
        pressed = 0 <= index < len(msg.buttons) and msg.buttons[index] == 1
        if pressed and not self._previous_emergency:
            self.abort('joystick Y')
        self._previous_emergency = pressed

    def _voice_callback(self, msg: RobotCommand) -> None:
        if msg.action.strip().lower() == 'stop':
            self.abort('voice stop')

    def _on_state(self, state: str) -> None:
        if self._moving and state != 'IDLE':
            self.abort(f'control_node state changed to {state}')

    # Motion -----------------------------------------------------------

    def build_goal(self, point, plan_only: bool) -> MoveGroup.Goal:
        """Mirror control_node._send_cartesian_pose for one point."""
        p = self.params
        goal = MoveGroup.Goal()
        goal.request.group_name = p['move_group_name']
        goal.request.allowed_planning_time = float(
            p['moveit_planning_time_sec'])
        goal.request.num_planning_attempts = 3
        goal.request.max_velocity_scaling_factor = self.velocity_scale
        goal.request.max_acceleration_scaling_factor = \
            self.acceleration_scale
        goal.planning_options.plan_only = plan_only
        goal.planning_options.replan = not plan_only
        goal.planning_options.replan_attempts = 0 if plan_only else 2

        pose = Pose()
        pose.position.x, pose.position.y, pose.position.z = point
        (pose.orientation.x, pose.orientation.y, pose.orientation.z,
         pose.orientation.w) = [float(v) for v in p['pick_orientation_xyzw']]
        constraint = PositionConstraint()
        constraint.header.frame_id = p['base_frame']
        constraint.link_name = p['end_effector_link']
        box = SolidPrimitive()
        box.type = SolidPrimitive.BOX
        box.dimensions = [float(p['position_tolerance_m'])] * 3
        constraint.constraint_region.primitives.append(box)
        constraint.constraint_region.primitive_poses.append(pose)
        constraint.weight = 1.0
        constraints = Constraints()
        constraints.position_constraints.append(constraint)
        goal.request.goal_constraints.append(constraints)
        return goal

    def _wait(self, future, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while not future.done():
            if self.abort_event.is_set() or time.monotonic() > deadline:
                return False
            time.sleep(0.02)
        return True

    def move(self, point, plan_only: bool) -> Tuple[bool, str]:
        """Plan (and optionally execute); return (success, message)."""
        if self.abort_event.is_set():
            return False, f'stopped: {self.abort_reason}'
        if self.control_state != 'IDLE':
            return False, (f'control_node state is '
                           f'{self.control_state or "unknown"}, need IDLE')
        if not self.client.wait_for_server(timeout_sec=5.0):
            return False, 'MoveIt /move_action server not available'

        self._moving = not plan_only
        try:
            send_future = self.client.send_goal_async(
                self.build_goal(point, plan_only))
            if not self._wait(send_future, 10.0):
                return False, f'goal not sent ({self.abort_reason or "timeout"})'
            handle = send_future.result()
            if not handle.accepted:
                return False, 'MoveIt rejected the goal'
            with self._goal_lock:
                self._goal_handle = handle
            if self.abort_event.is_set():
                handle.cancel_goal_async()
                return False, f'stopped: {self.abort_reason}'
            result_future = handle.get_result_async()
            timeout = float(self.params['moveit_planning_time_sec']) + 60.0
            if not self._wait(result_future, timeout):
                handle.cancel_goal_async()
                return False, f'stopped: {self.abort_reason or "timeout"}'
            response = result_future.result()
        finally:
            with self._goal_lock:
                self._goal_handle = None
            self._moving = False

        code = response.result.error_code.val
        if response.status != GoalStatus.STATUS_SUCCEEDED or \
                code != MoveItErrorCodes.SUCCESS:
            return False, f'MoveIt failed: status={response.status}, error={code}'
        return True, 'planned' if plan_only else 'executed'


def ask(prompt: str) -> str:
    """Read one line, treating Ctrl+D as quit."""
    try:
        return input(prompt).strip().lower()
    except EOFError:
        return 'q'


def ask_physical(surface_z: float):
    """Return the ruler-measured hand point in base_link or None."""
    while True:
        try:
            text = input('  실측 x_mm y_mm 높이_mm (Enter=건너뜀): ').strip()
        except EOFError:
            return None
        if not text:
            return None
        try:
            x_mm, y_mm, h_mm = (float(v) for v in text.replace(',', ' ').split())
            return grid_mm_to_base(x_mm, y_mm, h_mm, surface_z)
        except ValueError:
            print('  숫자 세 개를 입력하세요. 예: 203 -41 58')


def error_columns(prefix: str, measured, truth) -> dict:
    """Return x/y/z/xy/xyz error columns for ``measured - truth``."""
    error = point_error_mm(measured, truth)
    columns = {
        f'{prefix}_x_mm': fmt(error.ex, 1),
        f'{prefix}_y_mm': fmt(error.ey, 1),
        f'{prefix}_z_mm': fmt(error.ez, 1),
        f'{prefix}_xy_mm': fmt(error.horizontal, 1),
    }
    if prefix != 'mech':
        columns[f'{prefix}_xyz_mm'] = fmt(error.total, 1)
    return columns, error


def probe_target(node: ArmProbe, args, target, limits) -> Optional[dict]:
    """Plan, execute and measure one target. Return a CSV row or None."""
    target_id, x_mm, y_mm, h_mm = target
    point = grid_mm_to_base(x_mm, y_mm, h_mm, args.surface_z)
    print(f'\n[{target_id}] 목표: 격자 ({x_mm:.0f}, {y_mm:.0f}) mm, '
          f'표면 위 {h_mm:.0f} mm -> base_link '
          f'({point[0]:.3f}, {point[1]:.3f}, {point[2]:.3f}) m')
    problems = probe_target_problems(point, limits)
    if problems:
        print('  거부: ' + '; '.join(problems))
        return None

    choice = ask('  Enter=계획 확인(움직이지 않음), s=건너뜀, q=종료: ')
    if choice in ('s', 'q'):
        return {'_quit': True} if choice == 'q' else None
    ok, message = node.move(point, plan_only=True)
    print(f'  계획: {message}')
    row = {
        'timestamp': datetime.datetime.now().isoformat(timespec='seconds'),
        'target_id': target_id,
        'target_x': fmt(point[0]), 'target_y': fmt(point[1]),
        'target_z': fmt(point[2]),
    }
    if not ok:
        row.update({'result': 'plan_failed', 'message': message})
        return row

    choice = ask('  Y 버튼 준비. Enter=실행(팔이 움직입니다), s=건너뜀, q=종료: ')
    if choice in ('s', 'q'):
        return {'_quit': True} if choice == 'q' else None
    ok, message = node.move(point, plan_only=False)
    print(f'  실행: {message}')
    if not ok:
        row.update({'result': 'stopped' if node.abort_event.is_set()
                    else 'execute_failed', 'message': message})
        return row

    time.sleep(args.settle)
    samples = node.sample_position(BASE_FRAME, EE_FRAME)
    pose = node.lookup(BASE_FRAME, EE_FRAME)
    row.update(joint_columns(node.joints()))
    row.update({'result': 'reached', 'message': message})
    if not samples or pose is None:
        row['message'] = 'reached, but TF end_effector_link unavailable'
        return row

    fk = mean_point(samples)
    row.update({'fk_x': fmt(fk[0]), 'fk_y': fmt(fk[1]), 'fk_z': fmt(fk[2]),
                'fk_spread_mm': fmt(point_spread_mm(samples), 2)})
    for name, value in zip(('roll', 'pitch', 'yaw'),
                           quaternion_to_rpy_deg(pose[1])):
        row[f'ee_{name}_deg'] = fmt(value, 1)
    columns, fk_error = error_columns('err_fk', fk, point)
    row.update(columns)
    print(f'  FK    : ({fk[0]:.3f}, {fk[1]:.3f}, {fk[2]:.3f}) m | '
          f'목표 대비 x {fk_error.ex:+.1f}, y {fk_error.ey:+.1f}, '
          f'z {fk_error.ez:+.1f} mm')
    print(f'  손끝 pitch {row["ee_pitch_deg"]}° '
          '(90°에 가까우면 아래를 향함)')

    physical = ask_physical(args.surface_z)
    if physical is not None:
        row.update({'phys_x': fmt(physical[0]), 'phys_y': fmt(physical[1]),
                    'phys_z': fmt(physical[2])})
        columns, phys_error = error_columns('err_phys', physical, point)
        row.update(columns)
        row.update(error_columns('mech', physical, fk)[0])
        print(f'  실측  : 목표 대비 x {phys_error.ex:+.1f}, '
              f'y {phys_error.ey:+.1f}, z {phys_error.ez:+.1f} mm | '
              f'수평 {phys_error.horizontal:.1f} mm')
    return row


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    parser.add_argument('--targets', default=os.path.join(
        HERE, 'targets_arm.csv'))
    parser.add_argument('--control-config', default=None,
                        help='control.yaml to copy MoveIt settings from')
    parser.add_argument('--surface-z', type=float,
                        default=DEFAULT_SURFACE_Z_M)
    parser.add_argument('--min-clearance-mm', type=float, default=30.0)
    parser.add_argument('--settle', type=float, default=1.5,
                        help='seconds to wait before reading TF')
    parser.add_argument('--out', default=default_csv_path('arm'))
    args = parser.parse_args()

    params = load_control_params(args.control_config)
    targets = load_targets(args.targets)
    limits = ProbeLimits(surface_z_m=args.surface_z,
                         min_clearance_m=args.min_clearance_mm / 1000.0)

    rclpy.init()
    node = ArmProbe(params)
    executor, thread = spin_in_background(node)
    try:
        time.sleep(1.5)
        print(f'설정: {params["_path"]}')
        print(f'속도/가속 배율: {node.velocity_scale:.2f} / '
              f'{node.acceleration_scale:.2f}, 허용 상자 '
              f'{float(params["position_tolerance_m"]) * 1000:.0f} mm')
        print(f'control_node 상태: {node.control_state or "(없음)"}')
        print(f'목표 {len(targets)}개, 저장={args.out}')
        if node.control_state != 'IDLE':
            print('control_node가 IDLE이 아니면 움직이지 않습니다.')
        for target in targets:
            if node.abort_event.is_set():
                print(f'\n정지됨({node.abort_reason}). 남은 목표는 건너뜁니다.')
                break
            row = probe_target(node, args, target, limits)
            if row is None:
                continue
            if row.get('_quit'):
                break
            row['trial'] = next_trial_id(args.out)
            try:
                row['note'] = input('  메모 (없으면 Enter): ').strip()
            except EOFError:
                pass
            append_csv_row(args.out, FIELDS, row)
            print(f'  저장: trial {row["trial"]}')
    except KeyboardInterrupt:
        node.abort('Ctrl+C')
        time.sleep(0.3)
    finally:
        print('\n끝. 팔을 HOME으로: 조이스틱 Y(정지) -> X(HOME 복귀).')
        shutdown(node, executor, thread)


if __name__ == '__main__':
    main()
