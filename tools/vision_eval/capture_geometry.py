"""Capture synchronized RGB/depth and depth-time TF, without motion commands."""

import argparse
from collections import deque
from datetime import datetime
import json
from pathlib import Path
import time

from cv_bridge import CvBridge
import cv2
import message_filters
import rclpy
from rclpy.node import Node
from rclpy.qos import qos_profile_sensor_data
from rclpy.time import Time
from sensor_msgs.msg import CameraInfo, Image, JointState
from std_msgs.msg import Bool
from tf2_ros import Buffer, TransformListener

from capture_stationarity import stationary


def stamp_ns(msg):
    return Time.from_msg(msg.header.stamp).nanoseconds


def transform_dict(msg):
    t, q = msg.transform.translation, msg.transform.rotation
    return {'translation': [t.x, t.y, t.z],
            'quaternion': [q.x, q.y, q.z, q.w]}


class Capture(Node):
    def __init__(self):
        super().__init__('geometry_remeasurement_capture')
        self.pair = None
        self.info = None
        self.joints = None
        self.joint_at = 0
        self.joint_stamp = 0
        self.joint_history = deque(maxlen=100)
        self.ready = False
        self.ready_at = 0
        self.buffer = Buffer()
        self.listener = TransformListener(self.buffer, self)
        self.bridge = CvBridge()
        prefix = '/realsense/d435i/'
        self.rgb = message_filters.Subscriber(
            self, Image, prefix + 'color/image_raw', qos_profile=qos_profile_sensor_data)
        self.depth = message_filters.Subscriber(
            self, Image, prefix + 'aligned_depth_to_color/image_raw',
            qos_profile=qos_profile_sensor_data)
        self.sync = message_filters.ApproximateTimeSynchronizer(
            [self.rgb, self.depth], queue_size=10, slop=0.02)
        self.sync.registerCallback(self.on_pair)
        self.create_subscription(CameraInfo, prefix + 'aligned_depth_to_color/camera_info',
                                 self.on_info, qos_profile_sensor_data)
        self.create_subscription(JointState, '/joint_states', self.on_joints,
                                 qos_profile_sensor_data)
        self.create_subscription(Bool, '/measurement/ready', self.on_ready, 10)

    def on_pair(self, rgb, depth):
        self.pair = (rgb, depth)

    def on_info(self, msg):
        self.info = msg

    def on_joints(self, msg):
        self.joints = dict(zip(msg.name, msg.position))
        self.joint_at = time.monotonic()
        self.joint_stamp = stamp_ns(msg)
        names = ('joint1', 'joint2', 'joint3', 'joint4')
        if all(name in self.joints for name in names):
            self.joint_history.append((self.joint_at, [self.joints[n] for n in names]))

    def on_ready(self, msg):
        self.ready, self.ready_at = msg.data, time.monotonic()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--stationary-only', action='store_true',
                        help='Read-only capture using fresh stationary joint feedback, '
                             'independent of the measurement controller ready flag')
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    rclpy.init(args=[])
    node = Capture()
    records = []
    started = time.monotonic()
    last_saved = 0
    last_depth_stamp = -1
    metadata = {'created': datetime.now().astimezone().isoformat(), 'frames': records,
                'stationary_only': args.stationary_only,
                'method': 'RGB/depth paired within 20ms; TF queried at depth stamp; no actuator commands.'}
    try:
        while len(records) < 10 and time.monotonic() - started < 40:
            rclpy.spin_once(node, timeout_sec=0.05)
            now = time.monotonic()
            if args.stationary_only:
                ready = stationary(node.joint_history, now)
            else:
                ready = node.ready and now - node.ready_at <= 1
            joint_age = (node.get_clock().now().nanoseconds - node.joint_stamp) / 1e9
            if (not ready or now - node.joint_at > 1 or not 0 <= joint_age <= .5
                    or node.pair is None or node.info is None or now - last_saved < 0.5):
                continue
            rgb, depth = node.pair
            stamp = Time.from_msg(depth.header.stamp)
            if stamp.nanoseconds <= last_depth_stamp:
                continue
            age = (node.get_clock().now().nanoseconds - stamp.nanoseconds) / 1e9
            if not 0 <= age <= .25:
                continue
            frame = depth.header.frame_id
            if node.info.header.frame_id != frame:
                raise RuntimeError('Aligned depth intrinsics frame mismatch')
            if (depth.width, depth.height) != (node.info.width, node.info.height):
                raise RuntimeError('Aligned depth intrinsics size mismatch')
            if depth.encoding != '16UC1':
                raise RuntimeError(f'Unexpected depth encoding: {depth.encoding}')
            frames = [frame, 'link1', 'link3', 'base_footprint']
            if not all(node.buffer.can_transform('base_link', f, stamp) for f in frames):
                continue
            transforms = {f: transform_dict(node.buffer.lookup_transform('base_link', f, stamp))
                          for f in frames}
            i = len(records) + 1
            for kind, msg, encoding in [('rgb', rgb, 'bgr8'), ('depth', depth, 'passthrough')]:
                data = node.bridge.imgmsg_to_cv2(msg, desired_encoding=encoding)
                if not cv2.imwrite(str(args.output / f'{i:03d}_{kind}.png'), data):
                    raise RuntimeError('Image save failed')
            records.append({
                'index': i, 'rgb_stamp_ns': stamp_ns(rgb), 'depth_stamp_ns': stamp_ns(depth),
                'pair_delta_ms': abs(stamp_ns(rgb) - stamp_ns(depth)) / 1e6,
                'depth_age_seconds_at_capture': age, 'optical_frame': frame,
                'intrinsics': {'k': list(node.info.k), 'd': list(node.info.d),
                               'model': node.info.distortion_model,
                               'width': node.info.width, 'height': node.info.height},
                'transforms_to_base_link_at_depth_stamp': transforms,
                'joints_latest_radians': node.joints,
                'joint_stamp_ns': node.joint_stamp,
                'joint_to_depth_delta_ms': (node.joint_stamp - stamp.nanoseconds) / 1e6,
            })
            last_saved = now
            last_depth_stamp = stamp.nanoseconds
            print(f'Captured {i}/10; pair delta {records[-1]["pair_delta_ms"]:.3f}ms', flush=True)
        if len(records) != 10:
            raise RuntimeError(f'Captured {len(records)}/10; check fresh readiness, images and TF')
    except Exception as exc:
        metadata['error'] = str(exc)
        raise
    finally:
        (args.output / 'capture.json').write_text(json.dumps(metadata, indent=2) + '\n')
        node.destroy_node()
        rclpy.shutdown()


if __name__ == '__main__':
    main()
