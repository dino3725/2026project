#!/usr/bin/env python3
r"""
측정 전용 도구 - control_node를 거치지 않음 - 평상시 사용 금지.

MEASUREMENT ONLY. Visit candidate camera poses automatically and record how
well one object is detected at each pose. A person starts it, keeps a hand
on the Y button and swaps objects between runs. The gripper is never
commanded.

For each object run:
  1. Put the object on the box at the given spot.
  2. The tool visits the current RECOGNITION pose first, then the other
     candidates (generated from URDF kinematics so the object lands inside
     the image), dwells a few seconds at each and runs YOLO on the live
     camera for every class.
  3. Each pose gets one CSV row: detection rate at >= 0.5 for the target
     class, mean/max confidence, image position, best other class.
  4. At the end it offers to return HOME.

Safety: same as pose_jog.py / arm_probe.py (control_node IDLE required;
Y, voice stop, Ctrl+C or control_node leaving IDLE cancel; every target
and the joint path are checked for limits, 50 mm surface clearance and
the robot body; <= 10 deg/s).

    export PYTHONPATH=~/2026project/.venv/lib/python3.10/site-packages:$PYTHONPATH
    python3 tools/manipulator_eval/pose_sweep.py --object vaseline \
        --object-height-mm 69
"""

import argparse
import datetime
import os
import threading
import time
from collections import Counter

import cv2
from cv_bridge import CvBridge
import rclpy
from rclpy.qos import qos_profile_sensor_data
from sensor_msgs.msg import Image

from arm_probe import ask, load_control_params
from baseline_math import (
    forward_kinematics,
    jog_target_problems,
    project_to_image,
    sweep_candidates,
)
from live_detect import (
    annotate,
    DEFAULT_MODEL,
    DEFAULT_SAVE_DIR,
    detect,
    NORMAL_THRESHOLD,
)
from pose_jog import MAX_STEP_SPEED_DEG_S, path_problems, PoseJog
from ros_common import (
    append_csv_row,
    ARM_JOINTS,
    default_csv_path,
    fmt,
    shutdown,
    spin_in_background,
)

OBJECTS = ('airpod', 'coke', 'tissue', 'vaseline')
BANNER = ('*** pose_sweep: 측정 전용 - control_node를 거치지 않음 - '
          '평상시 사용 금지 ***')
JOINT2 = (-45.0, -35.0, -25.0, -15.0)
JOINT3 = (-50.0, -41.0, -30.0, -20.0)
JOINT4 = (95.0, 105.0, 115.0)
FIELDS = (
    ['timestamp', 'object', 'object_x', 'object_y', 'index', 'result']
    + [f'{j}_deg' for j in ARM_JOINTS]
    + ['cam_pitch_deg', 'pred_row', 'pred_col', 'pred_dist_m', 'frames',
       'target_rate', 'target_conf_mean', 'target_conf_max',
       'target_row_mean', 'target_col_mean', 'other_class',
       'other_conf_mean', 'message']
)


class SweepNode(PoseJog):
    """PoseJog plus the newest camera frame."""

    def __init__(self, params: dict, topic: str) -> None:
        super().__init__(params)
        self.bridge = CvBridge()
        self._frame_lock = threading.Lock()
        self._frame = None
        self._frame_id = 0
        self.create_subscription(Image, topic, self._image_callback,
                                 qos_profile_sensor_data)

    def _image_callback(self, msg: Image) -> None:
        image = self.bridge.imgmsg_to_cv2(msg, desired_encoding='bgr8')
        with self._frame_lock:
            self._frame = image
            self._frame_id += 1

    def frame(self):
        """Return (frame_id, frame copy) of the newest image."""
        with self._frame_lock:
            if self._frame is None:
                return 0, None
            return self._frame_id, self._frame.copy()


def collect(node: SweepNode, model, device, target: str, dwell: float,
            min_conf: float, save_path: str = '') -> dict:
    """Run YOLO on fresh frames for ``dwell`` seconds and summarize."""
    frames = 0
    hits = 0
    confs, rows, cols = [], [], []
    others = Counter()
    other_conf = Counter()
    last_id = -1
    deadline = time.monotonic() + dwell
    while time.monotonic() < deadline and not node.abort_event.is_set():
        frame_id, frame = node.frame()
        if frame is None or frame_id == last_id:
            time.sleep(0.03)
            continue
        last_id = frame_id
        frames += 1
        detections = detect(model, frame, min_conf, device)
        best = next((d for d in detections if d[0] == target), None)
        if best is not None:
            confs.append(best[1])
            rows.append((best[3] + best[5]) / 2.0)
            cols.append((best[2] + best[4]) / 2.0)
            if best[1] >= NORMAL_THRESHOLD:
                hits += 1
        else:
            confs.append(0.0)
        for name, conf, *_ in detections:
            if name != target:
                others[name] += 1
                other_conf[name] += conf
    if save_path and last_id >= 0:
        _, frame = node.frame()
        if frame is not None:
            image = annotate(frame, detect(model, frame, min_conf, device),
                             node.joints(), 0.0)
            os.makedirs(os.path.dirname(save_path), exist_ok=True)
            cv2.imwrite(save_path, image)
    summary = {'frames': frames}
    if frames:
        summary['target_rate'] = hits / frames
        summary['target_conf_mean'] = sum(confs) / frames
        summary['target_conf_max'] = max(confs)
    if rows:
        summary['target_row_mean'] = sum(rows) / len(rows)
        summary['target_col_mean'] = sum(cols) / len(cols)
    if others:
        name, count = others.most_common(1)[0]
        summary['other_class'] = name
        summary['other_conf_mean'] = other_conf[name] / count
    return summary


def write_row(args, index, pose, result, summary=None, message='',
              pixel=None) -> None:
    """Append one pose row."""
    summary = summary or {}
    row = {
        'timestamp': datetime.datetime.now().isoformat(timespec='seconds'),
        'object': args.object, 'index': index, 'result': result,
        'object_x': fmt(args.object_x, 3), 'object_y': fmt(args.object_y, 3),
        'cam_pitch_deg': fmt(forward_kinematics(pose).camera_pitch_deg, 1),
        'message': message, 'frames': summary.get('frames', ''),
        'other_class': summary.get('other_class', ''),
    }
    for name, value in zip(ARM_JOINTS, pose):
        row[f'{name}_deg'] = fmt(value, 1)
    if pixel is not None:
        row.update({'pred_col': fmt(pixel[0], 0), 'pred_row': fmt(pixel[1], 0),
                    'pred_dist_m': fmt(pixel[2], 3)})
    for key in ('target_rate', 'target_conf_mean', 'target_conf_max',
                'other_conf_mean'):
        if key in summary:
            row[key] = fmt(summary[key], 3)
    for key in ('target_row_mean', 'target_col_mean'):
        if key in summary:
            row[key] = fmt(summary[key], 0)
    append_csv_row(args.out, FIELDS, row)


def return_home(node: SweepNode, args, params) -> None:
    """Offer a checked joint-space return to HOME (via RECOGNITION)."""
    if not node.moved:
        return
    if node.abort_event.is_set() and node.abort_reason != 'Ctrl+C':
        print(f'\n비상정지({node.abort_reason}). 이 도구는 다시 움직이지 '
              '않습니다. 주변 확인 후 X로 HOME 복귀하세요.')
        return
    if node.control_state != 'IDLE':
        print('\ncontrol_node가 IDLE이 아닙니다. X로 HOME 복귀하세요.')
        return
    if args.return_home == 'no' or (
            args.return_home == 'ask'
            and ask('\n복귀: Enter=인식 자세 거쳐 HOME (Y 준비), n=그대로: ')
            in ('n', 'q')):
        print('팔을 그대로 둡니다. 나중에 Y -> X로 HOME 복귀하세요.')
        return
    node.abort_event.clear()
    node.abort_reason = ''
    clearance = 0.05
    for label, pose in (('RECOGNITION', params['recognition_pose_deg']),
                        ('HOME', params['home_pose_deg'])):
        pose = [float(v) for v in pose]
        start = node.measured_deg() or pose
        problems = path_problems(start, pose, args.surface_z, clearance) \
            if label == 'RECOGNITION' else []
        if problems:
            print(f'  {label} 경로 거부: {problems[0]}. Y -> X로 복귀하세요.')
            return
        ok, message = node.send_joints(pose, 4.0)
        print(f'  {label}: {message}')
        if not ok:
            print('  복귀 중단. 주변 확인 후 Y -> X로 복귀하세요.')
            return


def main() -> None:
    parser = argparse.ArgumentParser(description='pose_sweep (measurement only)')
    parser.add_argument('--object', required=True, choices=OBJECTS)
    parser.add_argument('--object-height-mm', required=True, type=float)
    parser.add_argument('--object-x', type=float, default=0.24,
                        help='object centre x on the box (m, base_link)')
    parser.add_argument('--object-y', type=float, default=0.0)
    parser.add_argument('--surface-z', type=float, default=0.070,
                        help='box top in base_link (8 cm box = 0.070)')
    parser.add_argument('--dwell', type=float, default=3.0)
    parser.add_argument('--settle', type=float, default=1.0)
    parser.add_argument('--min-conf', type=float, default=0.10)
    parser.add_argument('--model', default=DEFAULT_MODEL)
    parser.add_argument('--topic', default='/realsense/d435i/color/image_raw')
    parser.add_argument('--control-config', default=None)
    parser.add_argument('--yes', action='store_true',
                        help='skip the start prompt (the person at the '
                             'robot has already confirmed: object placed, '
                             'hand on Y)')
    parser.add_argument('--return-home', choices=('ask', 'yes', 'no'),
                        default='ask',
                        help='after the sweep or Ctrl+C (never after Y or '
                             'voice stop)')
    parser.add_argument('--poses', default='',
                        help='explicit candidates "j2,j3,j4;j2,j3,j4;..." '
                             '(joint1 = 0); replaces the generated grid')
    parser.add_argument('--save-frames', action='store_true',
                        help='save one annotated image per pose under '
                             'results/live_snapshots/')
    parser.add_argument('--dry-run', action='store_true',
                        help='print the candidate poses and exit')
    parser.add_argument('--out', default=None,
                        help='default: results/raw/pose_verify_<date>.csv '
                             'with --poses, else pose_sweep_<date>.csv')
    args = parser.parse_args()

    if args.out is None:
        args.out = default_csv_path('pose_verify' if args.poses else 'pose_sweep')
    params = load_control_params(args.control_config)
    recognition = [float(v) for v in params['recognition_pose_deg']]
    target_point = (args.object_x, args.object_y,
                    args.surface_z + args.object_height_mm / 2000.0)
    if args.poses:
        try:
            poses = [tuple([0.0] + [float(v) for v in item.split(',')])
                     for item in args.poses.split(';') if item.strip()]
        except ValueError:
            parser.error('--poses must look like "-45,-30,115;-35,-41,115"')
        if any(len(pose) != 4 for pose in poses):
            parser.error('each --poses item needs joint2,joint3,joint4')
    else:
        poses = sweep_candidates(target_point, JOINT2, JOINT3, JOINT4,
                                 args.surface_z, first=recognition)
    print(BANNER)
    print(f'후보 자세 {len(poses)}개, 물체={args.object} at '
          f'({target_point[0]:.3f}, {target_point[1]:.3f}, '
          f'{target_point[2]:.3f}) m')
    if args.dry_run:
        for index, pose in enumerate(poses):
            column, row, dist = project_to_image(pose, target_point)
            problems = jog_target_problems(pose, args.surface_z)
            print(f'  {index:2d} {pose} row {row:4.0f} col {column:4.0f} '
                  f'dist {dist:.2f} pitch '
                  f'{forward_kinematics(pose).camera_pitch_deg:.0f}'
                  + (f'  REFUSED: {problems[0]}' if problems else ''))
        return

    import torch  # noqa: E402
    from ultralytics import YOLO  # noqa: E402
    device = 0 if torch.cuda.is_available() else 'cpu'
    model = YOLO(args.model)

    run_stamp = datetime.datetime.now().strftime('%H%M%S')
    rclpy.init()
    node = SweepNode(params, args.topic)
    executor, thread = spin_in_background(node)
    try:
        try:
            deadline = time.monotonic() + 15.0
            while time.monotonic() < deadline and (
                    node.frame()[1] is None
                    or node.control_state == ''
                    or node.measured_deg() is None):
                time.sleep(0.2)
            print(f'control_node 상태: {node.control_state or "(없음)"} | '
                  f'그리퍼 명령 없음 | 저장={args.out}')
            if node.frame()[1] is None:
                print('카메라 영상이 없습니다. REALSENSE 패널을 확인하세요.')
                return
            if not args.yes and ask(
                    f'{args.object}를 박스 위 정해진 자리에 놓고 Y를 준비한 '
                    '뒤 Enter (q=취소): ') in ('q', 'n'):
                return
            if node.control_state != 'IDLE':
                print('control_node가 IDLE이 아니라 시작하지 않습니다.')
                return
            for index, pose in enumerate(poses):
                if node.abort_event.is_set():
                    break
                start = node.measured_deg() or list(pose)
                problems = path_problems(start, pose, args.surface_z, 0.05)
                pixel = project_to_image(pose, target_point)
                if problems:
                    write_row(args, index, pose, 'skipped',
                              message=problems[0], pixel=pixel)
                    print(f'[{index + 1}/{len(poses)}] 건너뜀: {problems[0]}')
                    continue
                delta = max(abs(a - b) for a, b in zip(start, pose))
                duration = 4.0 if index == 0 else \
                    max(1.0, delta / MAX_STEP_SPEED_DEG_S)
                ok, message = node.send_joints(pose, duration)
                if not ok:
                    write_row(args, index, pose, 'stopped', message=message,
                              pixel=pixel)
                    print(f'[{index + 1}/{len(poses)}] 정지: {message}')
                    break
                time.sleep(args.settle)
                save_path = ''
                if args.save_frames:
                    save_path = os.path.join(
                        DEFAULT_SAVE_DIR,
                        f'{run_stamp}_{args.object}_y{args.object_y:+.2f}_'
                        f'{index:02d}.png')
                summary = collect(node, model, device, args.object,
                                  args.dwell, args.min_conf, save_path)
                write_row(args, index, pose, 'ok', summary, pixel=pixel)
                print(f'[{index + 1}/{len(poses)}] {pose} '
                      f'rate {summary.get("target_rate", 0):.2f} '
                      f'conf {summary.get("target_conf_mean", 0):.2f} '
                      f'other {summary.get("other_class", "-")}', flush=True)
        except KeyboardInterrupt:
            node.abort('Ctrl+C')
            time.sleep(0.3)
            print('\nCtrl+C: 현재 동작을 취소했습니다.', flush=True)
        try:
            return_home(node, args, params)
        except KeyboardInterrupt:
            node.abort('Ctrl+C during return')
            print('\n복귀 중 Ctrl+C: 정지. Y -> X로 복귀하세요.')
    finally:
        shutdown(node, executor, thread)


if __name__ == '__main__':
    main()
