#!/usr/bin/env python3
"""
Record vision-coordinate vs true-position trials (read-only).

Run control_node with ``pick_enabled: false`` so each trial is
RECOGNITION -> /find_object -> HOME, without any Pick motion. This script
only listens to /rosout, /control/state, TF and /joint_states; the person
at the robot starts each recognition with the joystick A button (or a
fetch command).

    python3 tools/manipulator_eval/record_vision_trials.py \
        --object coke --object-height-mm 122

For each trial:
  1. Put the object centered on a grid mark and type the mark id.
  2. Press A (or send a fetch command).
  3. The script stores the camera pose at VISION_SEARCH and the vision
     result, then prints the error against the mark.
"""

import argparse
import csv
import datetime
import os
import queue
import threading
import time

import rclpy
from rcl_interfaces.msg import Log
from rclpy.qos import QoSProfile, ReliabilityPolicy

from baseline_math import (
    DEFAULT_SURFACE_Z_M,
    grid_mm_to_base,
    parse_vision_log,
    point_error_mm,
)
from ros_common import (
    append_csv_row,
    BASE_FRAME,
    CAMERA_OPTICAL_FRAME,
    default_csv_path,
    fmt,
    JOINT_FIELDS,
    joint_columns,
    MeasurementNode,
    next_trial_id,
    quaternion_to_rpy_deg,
    shutdown,
    spin_in_background,
)

OBJECTS = ('airpod', 'coke', 'tissue', 'vaseline')
HERE = os.path.dirname(os.path.abspath(__file__))

FIELDS = (
    ['trial', 'timestamp', 'object', 'object_height_mm', 'mark',
     'result', 'true_x', 'true_y', 'true_z', 'vis_x', 'vis_y', 'vis_z',
     'err_x_mm', 'err_y_mm', 'err_z_mm', 'err_xy_mm', 'err_xyz_mm',
     'cam_x', 'cam_y', 'cam_z', 'cam_roll_deg', 'cam_pitch_deg',
     'cam_yaw_deg']
    + JOINT_FIELDS + ['message', 'note']
)


def load_marks(path: str) -> dict:
    """Read ``id,x_mm,y_mm`` grid marks."""
    marks = {}
    with open(path, newline='', encoding='utf-8') as handle:
        for row in csv.DictReader(handle):
            marks[row['id'].strip()] = (float(row['x_mm']),
                                        float(row['y_mm']))
    return marks


class VisionRecorder(MeasurementNode):
    """Collect control_node vision results and camera pose snapshots."""

    def __init__(self) -> None:
        super().__init__('baseline_vision_recorder')
        self.results: 'queue.Queue' = queue.Queue()
        self.search_started = threading.Event()
        self.pick_seen = threading.Event()
        qos = QoSProfile(depth=100)
        qos.reliability = ReliabilityPolicy.RELIABLE
        self.create_subscription(Log, '/rosout', self._log_callback, qos)
        self.add_state_listener(self._on_state)

    def _on_state(self, state: str) -> None:
        if state == 'VISION_SEARCH':
            self.search_started.set()
        elif state == 'PICKING':
            self.pick_seen.set()

    def _log_callback(self, msg: Log) -> None:
        if msg.name != 'control_node':
            return
        point = parse_vision_log(msg.msg)
        if point is not None:
            self.results.put(('ok', point, msg.msg))
        elif msg.level >= Log.ERROR and 'recognition/pick failed' in msg.msg:
            self.results.put(('fail', None, msg.msg))

    def reset_trial(self) -> None:
        """Forget events from a previous trial."""
        self.search_started.clear()
        while not self.results.empty():
            self.results.get_nowait()


def ask_mark(marks: dict, previous: str):
    """Return (mark_label, x_mm, y_mm) or None to quit."""
    hint = f' [Enter={previous}]' if previous else ''
    while True:
        text = input(
            f'\n표식 번호 또는 "x_mm,y_mm" (q=종료){hint}: ').strip()
        if text.lower() == 'q':
            return None
        if not text and previous:
            text = previous
        if text in marks:
            return (text,) + marks[text]
        try:
            x_mm, y_mm = (float(v) for v in text.split(','))
            return (f'{x_mm:g},{y_mm:g}', x_mm, y_mm)
        except ValueError:
            print(f'  알 수 없는 입력입니다. 표식: {", ".join(marks)}')


def run_trial(node: VisionRecorder, args, mark, trial_id: int) -> None:
    """Wait for one recognition and append the result row."""
    label, x_mm, y_mm = mark
    truth = grid_mm_to_base(
        x_mm, y_mm, args.object_height_mm / 2.0, args.surface_z)
    node.reset_trial()
    if node.control_state != 'IDLE':
        print(f'  주의: control_node 상태가 {node.control_state or "없음"}'
              '입니다. IDLE에서 시작하세요.')
    print('  조이스틱 A 버튼(또는 fetch 명령)으로 인식을 시작하세요...')

    deadline = time.monotonic() + args.timeout
    camera = None
    joints = {}
    while time.monotonic() < deadline:
        if node.search_started.wait(timeout=0.2):
            time.sleep(0.3)  # arm is stationary at RECOGNITION here
            camera = node.lookup(BASE_FRAME, CAMERA_OPTICAL_FRAME)
            joints = node.joints()
            print('  VISION_SEARCH: 카메라 자세 저장, 인식 결과 대기 중')
            break
    try:
        status, point, message = node.results.get(
            timeout=max(1.0, deadline - time.monotonic()))
    except queue.Empty:
        print('  시간 초과: 인식 결과를 받지 못했습니다. 기록하지 않습니다.')
        return

    row = {
        'trial': trial_id,
        'timestamp': datetime.datetime.now().isoformat(timespec='seconds'),
        'object': args.object,
        'object_height_mm': fmt(args.object_height_mm, 1),
        'mark': label,
        'result': status,
        'true_x': fmt(truth[0]), 'true_y': fmt(truth[1]),
        'true_z': fmt(truth[2]),
        'message': message,
    }
    row.update(joint_columns(joints))
    if camera is not None:
        for axis, value in zip('xyz', camera[0]):
            row[f'cam_{axis}'] = fmt(value)
        for name, value in zip(('roll', 'pitch', 'yaw'),
                               quaternion_to_rpy_deg(camera[1])):
            row[f'cam_{name}_deg'] = fmt(value, 2)

    if status == 'ok':
        error = point_error_mm(point, truth)
        row.update({
            'vis_x': fmt(point[0]), 'vis_y': fmt(point[1]),
            'vis_z': fmt(point[2]),
            'err_x_mm': fmt(error.ex, 1), 'err_y_mm': fmt(error.ey, 1),
            'err_z_mm': fmt(error.ez, 1),
            'err_xy_mm': fmt(error.horizontal, 1),
            'err_xyz_mm': fmt(error.total, 1),
        })
        print(f'  실제   : ({truth[0]:.3f}, {truth[1]:.3f}, {truth[2]:.3f}) m')
        print(f'  비전   : ({point[0]:.3f}, {point[1]:.3f}, {point[2]:.3f}) m')
        print(f'  오차   : x {error.ex:+.1f}, y {error.ey:+.1f}, '
              f'z {error.ez:+.1f} mm | 수평 {error.horizontal:.1f} mm')
    else:
        print(f'  인식 실패: {message}')

    time.sleep(0.5)
    if node.pick_seen.is_set():
        print('  !!! PICKING 상태를 봤습니다. pick_enabled=false 설정으로 '
              'control_node를 다시 실행하세요. Y 버튼을 준비하세요.')
    row['note'] = input('  메모 (없으면 Enter): ').strip()
    append_csv_row(args.out, FIELDS, row)
    print(f'  저장: trial {trial_id} -> {args.out}')


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    parser.add_argument('--object', required=True, choices=OBJECTS)
    parser.add_argument('--object-height-mm', required=True, type=float,
                        help='object height; truth z = surface + height/2')
    parser.add_argument('--marks', default=os.path.join(
        HERE, 'marks_vision.csv'))
    parser.add_argument('--surface-z', type=float,
                        default=DEFAULT_SURFACE_Z_M,
                        help='surface height in base_link (floor = -0.010)')
    parser.add_argument('--timeout', type=float, default=120.0)
    parser.add_argument('--out', default=default_csv_path('vision'))
    args = parser.parse_args()

    marks = load_marks(args.marks)
    rclpy.init()
    node = VisionRecorder()
    executor, thread = spin_in_background(node)
    try:
        time.sleep(1.0)
        print(f'물체={args.object}, 높이={args.object_height_mm:.0f} mm, '
              f'표면 z={args.surface_z:.3f} m, 저장={args.out}')
        print(f'control_node 상태: {node.control_state or "(아직 없음)"}')
        previous = ''
        while rclpy.ok():
            mark = ask_mark(marks, previous)
            if mark is None:
                break
            previous = mark[0]
            run_trial(node, args, mark, next_trial_id(args.out))
    except (KeyboardInterrupt, EOFError):
        pass
    finally:
        shutdown(node, executor, thread)


if __name__ == '__main__':
    main()
