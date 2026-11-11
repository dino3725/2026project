"""Prepare nearby calibration poses with FK, joint-limit and image checks."""

import argparse
import hashlib
import json
from pathlib import Path
import xml.etree.ElementTree as ET

import cv2
import numpy as np
from scipy.spatial.transform import Rotation

from handeye import check_diversity, transform

JOINTS = ['joint1', 'joint2', 'joint3', 'joint4']
EXPANDED_OFFSETS_DEG = [
    (0, 12, 0, 0), (-9, 12, 0, 0), (-18, 12, 0, 0), (-18, 20, 0, 0),
    (0, 20, 0, 0), (18, 20, 0, 0), (18, 12, 0, 0), (9, 12, 0, 0),
    (0, 12, 8, -8), (-12, 18, 8, -8), (12, 18, 8, -8), (0, 20, 8, -8),
    (-6, 18, 4, -4), (6, 20, 4, -4), (15, 18, 6, -6),
]


def fk(xml, angles, tip='end_effector_link'):
    """Evaluate a URDF chain relative to base_link without ROS."""
    root = ET.fromstring(xml)
    by_child = {j.find('child').get('link'): j for j in root.findall('joint')}
    chain = []
    child = tip
    while child != 'base_link':
        joint = by_child[child]
        chain.append(joint)
        child = joint.find('parent').get('link')
    result = np.eye(4)
    for joint in reversed(chain):
        origin = joint.find('origin')
        xyz = [float(v) for v in origin.get('xyz', '0 0 0').split()]
        rpy = [float(v) for v in origin.get('rpy', '0 0 0').split()]
        result = result @ transform(Rotation.from_euler('xyz', rpy).as_matrix(), xyz)
        if joint.get('type') == 'revolute':
            value = angles[joint.get('name')]
            limits = joint.find('limit')
            if not float(limits.get('lower')) <= value <= float(limits.get('upper')):
                raise ValueError(f'Joint limit violated: {joint.get("name")}')
            axis = np.array([float(v) for v in joint.find('axis').get('xyz').split()])
            result = result @ transform(Rotation.from_rotvec(axis * value).as_matrix(), [0, 0, 0])
        elif joint.get('type') != 'fixed':
            raise ValueError('Unsupported joint type in arm chain')
    return result


def make_plan(xml, observation, expanded_start=None):
    """Screen an explicit pose set, optionally with wider angles and arm articulation."""
    center = np.array([observation['joints_latest'][name] for name in JOINTS])
    if not np.allclose(fk(xml, dict(zip(JOINTS, center))), observation['T_base_ee'], atol=0.002):
        raise ValueError('Recorded TF and URDF FK disagree')
    # This uncalibrated mount is used only for conservative visibility screening.
    mount = transform(np.eye(3), [-0.065, 0.0195, 0.052])
    camera_optical = np.asarray(observation['T_camera_optical'])
    fixed_board = (np.asarray(observation['T_base_ee']) @ mount @ camera_optical
                   @ np.asarray(observation['T_optical_board']))
    corners = np.array([[0, 0, 0], [.175, 0, 0], [.175, .125, 0], [0, .125, 0]])
    info = observation['color_info']
    k = np.asarray(info['k']).reshape(3, 3)
    d = np.asarray(info['d'])
    # Offsets in degrees relative to the actual stationary pose, not new task defaults.
    offsets = [(-6, 0), (-12, 0), (-12, 6), (-6, 6), (0, 6), (6, 6),
               (12, 6), (12, 0), (6, 0), (6, 12), (0, 12), (-6, 12),
               (-9, 3), (3, 9), (9, 3)]
    offsets = [(yaw, shoulder, 0, 0) for yaw, shoulder in offsets]
    start = center.copy()
    if expanded_start is not None:
        if expanded_start['robot_description_sha256'] != observation['robot_description_sha256']:
            raise ValueError('Expanded start uses a different robot model')
        offsets = EXPANDED_OFFSETS_DEG
        start = np.array([expanded_start['joints_latest'][name] for name in JOINTS])
    previous = start.copy()
    poses = []
    for index, offset in enumerate(offsets):
        target = center + np.deg2rad(offset)
        if expanded_start is not None and np.max(np.abs(target - previous)) > np.deg2rad(24.01):
            raise ValueError('Expanded plan segment exceeds reviewed joint change')
        minimum_z = float('inf')
        minimum_margin = float('inf')
        for fraction in np.linspace(0, 1, 25):
            q = previous * (1 - fraction) + target * fraction
            joints = dict(zip(JOINTS, q))
            ee = fk(xml, joints)
            # End-effector frame clearance is a screening metric, not mesh collision checking.
            minimum_z = min(minimum_z, float(ee[2, 3]))
            optical_board = np.linalg.inv(ee @ mount @ camera_optical) @ fixed_board
            if np.min((optical_board[:3, :3] @ corners.T + optical_board[:3, 3, None])[2]) <= 0:
                raise ValueError('Board behind predicted camera')
            pixels = cv2.projectPoints(corners, cv2.Rodrigues(optical_board[:3, :3])[0],
                                       optical_board[:3, 3], k, d)[0].reshape(-1, 2)
            margin = min(pixels[:, 0].min(), pixels[:, 1].min(),
                         info['width'] - pixels[:, 0].max(), info['height'] - pixels[:, 1].max())
            minimum_margin = min(minimum_margin, float(margin))
        z_limit, margin_limit = (0.25, 80) if expanded_start is not None else (0.20, 50)
        if minimum_z < z_limit or minimum_margin < margin_limit:
            raise ValueError(
                f'Pose {index + 1}: clearance {minimum_z:.3f}m, margin {minimum_margin:.1f}px')
        poses.append({
            'index': index + 1, 'role': 'train' if index < 12 else 'validation',
            'positions_rad': target.tolist(), 'positions_deg': np.rad2deg(target).tolist(),
            'predicted_min_ee_z_base_m': minimum_z,
            'predicted_min_pattern_image_margin_px': minimum_margin,
        })
        previous = target
    singular = check_diversity([fk(xml, dict(zip(JOINTS, start)))] + [
        fk(xml, dict(zip(JOINTS, p['positions_rad']))) for p in poses if p['role'] == 'train'])
    return {
        'status': 'prepared_not_executed',
        'phase': 'expanded_v2' if expanded_start is not None else 'nearby_v1',
        'session_id': (expanded_start or observation)['session_id'],
        'model_sha256': hashlib.sha256(xml.encode()).hexdigest(),
        'center_positions_rad': center.tolist(), 'joint_names': JOINTS, 'poses': poses,
        'start_positions_rad': start.tolist(),
        'rotation_singular_values': singular,
        'limits': 'Visibility uses nominal mount; no mesh collision proof. Clear space required.',
        'motion_duration_sec': 4.0,
        'execution': 'One explicit Trigger per pose; no automatic sequence.',
    }


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--model', type=Path, required=True)
    parser.add_argument('--observation', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--expanded-start', type=Path)
    args = parser.parse_args()
    start = json.loads(args.expanded_start.read_text()) if args.expanded_start else None
    result = make_plan(args.model.read_text(), json.loads(args.observation.read_text()), start)
    with args.output.open('x') as stream:
        stream.write(json.dumps(result, indent=2) + '\n')
    for pose in result['poses']:
        print(pose['index'], pose['role'], pose['positions_deg'],
              'margin px', round(pose['predicted_min_pattern_image_margin_px'], 1),
              'EE z mm', round(pose['predicted_min_ee_z_base_m'] * 1000, 1))
