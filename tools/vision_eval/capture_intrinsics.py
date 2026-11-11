"""Save one RGB/CameraInfo pair for intrinsic validation, with no robot commands."""

import argparse
from datetime import datetime
import hashlib
import json
from pathlib import Path
import time

from cv_bridge import CvBridge
import cv2
import message_filters
import rclpy
from rclpy.node import Node
from rclpy.qos import qos_profile_sensor_data
from sensor_msgs.msg import CameraInfo, Image

from handeye import BOARD_SPEC
from intrinsics_observation import extract, metric_corners


class ColorCapture(Node):
    """Subscribe to exact-stamp raw color images and their camera metadata."""

    def __init__(self):
        """Create only subscriptions; neither TF nor arm drivers are needed."""
        super().__init__('intrinsics_observation_capture')
        self.pair = None
        prefix = '/realsense/d435i/color/'
        self.rgb = message_filters.Subscriber(
            self, Image, prefix + 'image_raw', qos_profile=qos_profile_sensor_data)
        self.info = message_filters.Subscriber(
            self, CameraInfo, prefix + 'camera_info', qos_profile=qos_profile_sensor_data)
        self.sync = message_filters.TimeSynchronizer([self.rgb, self.info], 20)
        self.sync.registerCallback(self.on_pair)

    def on_pair(self, rgb, info):
        """Retain the latest pair with matching ROS header timestamps."""
        self.pair = (rgb, info)


def main():
    """Record a deliberately requested observation, without auto-collecting poses."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--session-id', required=True)
    parser.add_argument('--role', choices=['audit', 'train', 'validation'], required=True)
    parser.add_argument('--width-mm', type=float, required=True)
    parser.add_argument('--height-mm', type=float, required=True)
    args = parser.parse_args()
    metric_corners([0], args.width_mm, args.height_mm)
    args.output.mkdir(parents=True, exist_ok=False)
    result = {
        'created': datetime.now().astimezone().isoformat(), 'status': 'incomplete',
        'session_id': args.session_id, 'role': args.role,
        'nominal_board': BOARD_SPEC,
        'measured_pattern_size_mm': [args.width_mm, args.height_mm],
        'geometry_assumption': 'uniform_print_scale_per_axis; measurement_uncertainty_unknown',
        'opencv_version': cv2.__version__, 'robot_commands_sent': False,
        'camera_stationarity': 'not_verified_by_this_image_only_collector',
        'applied': False,
    }
    rclpy.init(args=[])
    node = ColorCapture()
    bridge = CvBridge()
    last_reason = 'Waiting for fresh RGB and exact-stamp CameraInfo'
    last_stamp = None
    started = time.monotonic()
    try:
        while time.monotonic() - started < 20:
            rclpy.spin_once(node, timeout_sec=0.05)
            if node.pair is None:
                continue
            rgb, info = node.pair
            stamp = rgb.header.stamp.sec * 1000000000 + rgb.header.stamp.nanosec
            if stamp == last_stamp:
                continue
            last_stamp = stamp
            age = (node.get_clock().now().nanoseconds - stamp) / 1e9
            if not 0 <= age <= 0.5:
                continue
            if ((rgb.width, rgb.height) != (info.width, info.height)
                    or rgb.header.frame_id != info.header.frame_id):
                raise ValueError('Image and CameraInfo geometry mismatch')
            image = bridge.imgmsg_to_cv2(rgb, desired_encoding='bgr8')
            result.update({
                'rgb_stamp_ns': stamp, 'frame_age_seconds': age,
                'color_info': {
                    'k': list(info.k), 'd': list(info.d), 'r': list(info.r),
                    'p': list(info.p), 'width': info.width, 'height': info.height,
                    'model': info.distortion_model, 'frame': info.header.frame_id,
                },
            })
            try:
                result.update(extract(image, args.width_mm, args.height_mm))
            except ValueError as exc:
                last_reason = str(exc)
                if args.role != 'audit':
                    continue
                result['detection_error'] = last_reason
            if not cv2.imwrite(str(args.output / 'rgb.png'), image):
                raise RuntimeError('Failed to save image')
            result['image_sha256'] = hashlib.sha256(
                (args.output / 'rgb.png').read_bytes()).hexdigest()
            result['status'] = ('audit_only' if args.role == 'audit'
                                else 'observation_requires_quality_review')
            break
        else:
            raise RuntimeError(last_reason)
    except Exception as exc:
        result['error'] = str(exc)
        raise
    finally:
        (args.output / 'observation.json').write_text(json.dumps(result, indent=2) + '\n')
        print(json.dumps({k: result.get(k) for k in
                         ['status', 'corner_count', 'detection_error', 'error']}, indent=2))
        node.destroy_node()
        rclpy.shutdown()


if __name__ == '__main__':
    main()
