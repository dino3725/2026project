"""Record one stationary calibration observation or audit RGB-D configuration."""

import argparse
from datetime import datetime
import hashlib
import json
from pathlib import Path
import time

import cv2
import numpy as np
import rclpy
from rclpy.qos import DurabilityPolicy, QoSProfile, qos_profile_sensor_data
from rclpy.time import Time
from scipy.spatial.transform import Rotation
from sensor_msgs.msg import CameraInfo
from std_msgs.msg import String

from capture_geometry import Capture, stamp_ns
from handeye import BOARD_SPEC, detect, transform


def matrix(msg):
    """Convert a ROS transform without changing its direction."""
    q, t = msg.transform.rotation, msg.transform.translation
    return transform(Rotation.from_quat([q.x, q.y, q.z, q.w]).as_matrix(), [t.x, t.y, t.z])


def camera_info(msg):
    """Keep the complete projection metadata needed to audit a capture."""
    return {'k': list(msg.k), 'd': list(msg.d), 'r': list(msg.r), 'p': list(msg.p),
            'width': msg.width, 'height': msg.height, 'model': msg.distortion_model,
            'frame': msg.header.frame_id}


class BoardCapture(Capture):
    """Subscribe only; use exact RGB CameraInfo stamps and image-time TF."""

    def __init__(self):
        """Add color metadata and model fingerprint subscriptions."""
        super().__init__()
        self.color_infos = {}
        self.robot_description_sha256 = None
        self.create_subscription(
            CameraInfo, '/realsense/d435i/color/camera_info',
            self.on_color_info, qos_profile_sensor_data)
        self.create_subscription(
            String, '/robot_description', self.on_description,
            QoSProfile(depth=1, durability=DurabilityPolicy.TRANSIENT_LOCAL))

    def on_description(self, msg):
        """Fingerprint the actual robot description published by RSP."""
        self.robot_description_sha256 = hashlib.sha256(msg.data.encode()).hexdigest()

    def on_color_info(self, msg):
        """Keep a bounded exact-stamp lookup for RGB intrinsics."""
        self.color_infos[stamp_ns(msg)] = msg
        while len(self.color_infos) > 30:
            del self.color_infos[next(iter(self.color_infos))]


def main():
    """Save an audit or one accepted sample; never publish robot commands."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--audit-only', action='store_true')
    parser.add_argument('--session-id', default='')
    parser.add_argument('--role', choices=['train', 'validation'], default='train')
    args = parser.parse_args()
    if not args.audit_only and not args.session_id:
        parser.error('--session-id is required; keep board/base fixed within this session')
    args.output.mkdir(parents=True, exist_ok=False)
    rclpy.init(args=[])
    node = BoardCapture()
    result = {'created': datetime.now().astimezone().isoformat(),
              'status': 'incomplete', 'session_id': args.session_id, 'role': args.role,
              'board': BOARD_SPEC, 'opencv_version': cv2.__version__}
    started = time.monotonic()
    last_reason = 'Waiting for fresh readiness, RGB-D, CameraInfo and TF'
    history = []
    try:
        while time.monotonic() - started < 20:
            rclpy.spin_once(node, timeout_sec=0.05)
            now = time.monotonic()
            if (node.pair is None or node.info is None or not node.ready
                    or node.robot_description_sha256 is None
                    or now - node.ready_at > 1 or now - node.joint_at > 0.5):
                continue
            rgb, depth = node.pair
            info = node.color_infos.get(stamp_ns(rgb))
            if info is None:
                continue
            stamp = Time.from_msg(rgb.header.stamp)
            age = (node.get_clock().now().nanoseconds - stamp.nanoseconds) / 1e9
            if not 0 <= age <= 0.5:
                continue
            if (rgb.header.frame_id != info.header.frame_id
                    or depth.header.frame_id != rgb.header.frame_id
                    or (rgb.width, rgb.height) != (info.width, info.height)
                    or (depth.width, depth.height) != (info.width, info.height)
                    or node.info.header.frame_id != info.header.frame_id
                    or (node.info.width, node.info.height) != (info.width, info.height)
                    or not np.allclose(info.k, node.info.k, atol=1e-6, rtol=0)
                    or depth.encoding != '16UC1'):
                raise ValueError('RGB / aligned depth metadata mismatch')
            if info.distortion_model != 'plumb_bob' or len(info.d) != 5:
                raise ValueError('Unsupported RGB distortion model; do not guess PnP convention')
            pairs = [('base_link', 'end_effector_link'),
                     ('d435i_link', rgb.header.frame_id), ('base_link', 'link1')]
            if not all(node.buffer.can_transform(a, b, stamp) for a, b in pairs):
                continue
            ee, optical, link1 = [matrix(node.buffer.lookup_transform(a, b, stamp))
                                  for a, b in pairs]
            if history and history[-1][0] == stamp.nanoseconds:
                continue
            history.append((stamp.nanoseconds, ee))
            history = [h for h in history if stamp.nanoseconds - h[0] <= 800000000]
            if not history or stamp.nanoseconds - history[0][0] < 500000000:
                continue
            if any(np.linalg.norm(ee[:3, 3] - h[:3, 3]) > 0.001
                   or Rotation.from_matrix(ee[:3, :3].T @ h[:3, :3]).magnitude()
                   > np.deg2rad(0.3) for _, h in history):
                last_reason = 'Arm not stationary for 0.5 seconds'
                continue
            result.update({
                'rgb_stamp_ns': stamp_ns(rgb), 'depth_stamp_ns': stamp_ns(depth),
                'pair_delta_ms': abs(stamp_ns(rgb) - stamp_ns(depth)) / 1e6,
                'color_info': camera_info(info), 'aligned_depth_info': camera_info(node.info),
                'T_base_ee': ee.tolist(), 'T_camera_optical': optical.tolist(),
                'T_base_link1': link1.tolist(), 'joints_latest': node.joints,
                'robot_description_sha256': node.robot_description_sha256,
                'depth_encoding': depth.encoding,
            })
            if args.audit_only:
                result['status'] = 'metadata_audit_only_not_calibration'
                break
            if not np.allclose(link1[:3, 3], [-0.092, 0, 0.116], atol=1e-6):
                raise ValueError(
                    'Measured arm model is not loaded; restart with matched launches first')
            image = node.bridge.imgmsg_to_cv2(rgb, desired_encoding='bgr8')
            try:
                found = detect(image, info.k, info.d)
            except ValueError as exc:
                last_reason = str(exc)
                continue
            if not cv2.imwrite(str(args.output / 'rgb.png'), image):
                raise RuntimeError('Failed to save RGB')
            depth_image = node.bridge.imgmsg_to_cv2(depth, desired_encoding='passthrough')
            if not cv2.imwrite(str(args.output / 'depth.png'), depth_image):
                raise RuntimeError('Failed to save depth')
            result.update(found)
            result['image_sha256'] = hashlib.sha256(
                (args.output / 'rgb.png').read_bytes()).hexdigest()
            result['status'] = 'accepted_observation_not_calibration'
            break
        else:
            raise RuntimeError(last_reason)
    except Exception as exc:
        result['error'] = str(exc)
        raise
    finally:
        (args.output / 'observation.json').write_text(json.dumps(result, indent=2) + '\n')
        print(json.dumps(
            {k: result.get(k) for k in ['status', 'error', 'pair_delta_ms']}, indent=2))
        node.destroy_node()
        rclpy.shutdown()


if __name__ == '__main__':
    main()
