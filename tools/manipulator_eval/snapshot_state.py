#!/usr/bin/env python3
"""
Print the arm joints, gripper value and key frames (read-only).

Use it while measuring the gripper width (record ``gripper_q_m`` together
with the caliper reading) or to see where the robot thinks its hand and
camera are.

    python3 tools/manipulator_eval/snapshot_state.py
    python3 tools/manipulator_eval/snapshot_state.py --watch
    python3 tools/manipulator_eval/snapshot_state.py --label gripper_open

This script only listens. It never moves the robot.
"""

import argparse
import datetime
import math
import time

import rclpy

from ros_common import (
    append_csv_row,
    ARM_JOINTS,
    BASE_FRAME,
    CAMERA_OPTICAL_FRAME,
    default_csv_path,
    EE_FRAME,
    fmt,
    GRIPPER_JOINT,
    JOINT_FIELDS,
    joint_columns,
    MeasurementNode,
    quaternion_to_rpy_deg,
    shutdown,
    spin_in_background,
)

FRAMES = (
    ('end_effector_link', EE_FRAME),
    ('link2 (shoulder)', 'link2'),
    ('d435i_link', 'd435i_link'),
    ('camera optical', CAMERA_OPTICAL_FRAME),
    ('base_footprint', 'base_footprint'),
)

FIELDS = (
    ['timestamp', 'label', 'control_state'] + JOINT_FIELDS
    + ['ee_x', 'ee_y', 'ee_z', 'cam_x', 'cam_y', 'cam_z', 'note']
)


def describe(node: MeasurementNode) -> str:
    """Return a human-readable snapshot."""
    joints = node.joints()
    lines = [f'control_state: {node.control_state or "(none)"}']
    if joints:
        arm = ', '.join(
            f'{name}={math.degrees(joints[name]):7.2f}°'
            for name in ARM_JOINTS if name in joints
        )
        lines.append(f'arm joints : {arm}')
        if GRIPPER_JOINT in joints:
            lines.append(
                f'gripper    : {GRIPPER_JOINT}={joints[GRIPPER_JOINT]:.4f} m '
                '(open 0.019 / closed -0.010)'
            )
        lines.append(f'joint_states age: {node.joint_age():.2f} s')
    else:
        lines.append('arm joints : no /joint_states yet')
    for label, frame in FRAMES:
        pose = node.lookup(BASE_FRAME, frame)
        if pose is None:
            lines.append(f'{label:17s}: TF {BASE_FRAME}->{frame} unavailable')
            continue
        (x, y, z), quat = pose
        roll, pitch, yaw = quaternion_to_rpy_deg(quat)
        lines.append(
            f'{label:17s}: xyz=({x:+.4f}, {y:+.4f}, {z:+.4f}) m  '
            f'rpy=({roll:+.1f}, {pitch:+.1f}, {yaw:+.1f})°'
        )
    return '\n'.join(lines)


def log_snapshot(node: MeasurementNode, label: str, path: str,
                 note: str) -> None:
    """Append the current state to a CSV file."""
    row = {
        'timestamp': datetime.datetime.now().isoformat(timespec='seconds'),
        'label': label,
        'control_state': node.control_state,
        'note': note,
    }
    row.update(joint_columns(node.joints()))
    for prefix, frame in (('ee', EE_FRAME), ('cam', CAMERA_OPTICAL_FRAME)):
        pose = node.lookup(BASE_FRAME, frame)
        if pose is not None:
            for axis, value in zip('xyz', pose[0]):
                row[f'{prefix}_{axis}'] = fmt(value)
    append_csv_row(path, FIELDS, row)
    print(f'Saved to {path}')


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    parser.add_argument('--watch', action='store_true',
                        help='refresh once per second until Ctrl+C')
    parser.add_argument('--label', default='',
                        help='also append the snapshot to a CSV with a label')
    parser.add_argument('--note', default='')
    parser.add_argument('--out', default=default_csv_path('snapshots'))
    args = parser.parse_args()

    rclpy.init()
    node = MeasurementNode('baseline_snapshot')
    executor, thread = spin_in_background(node)
    try:
        time.sleep(1.5)  # let TF and joint_states arrive
        if args.watch:
            while rclpy.ok():
                print('\033[2J\033[H' + describe(node), flush=True)
                time.sleep(1.0)
        else:
            print(describe(node))
            if args.label:
                log_snapshot(node, args.label, args.out, args.note)
    except KeyboardInterrupt:
        pass
    finally:
        shutdown(node, executor, thread)


if __name__ == '__main__':
    main()
