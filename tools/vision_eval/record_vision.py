"""Record FindObject trials; never command an arm, gripper or base actuator."""

import argparse
import csv
from datetime import datetime
import hashlib
import json
import math
from pathlib import Path
import statistics
import subprocess
import time

import cv2
from cv_bridge import CvBridge
import rclpy
from rclpy.action import ActionClient
from rclpy.node import Node
from rclpy.qos import qos_profile_sensor_data
from rclpy.time import Time
from sensor_msgs.msg import CameraInfo, Image, JointState
from std_msgs.msg import Bool
from tf2_ros import Buffer, TransformListener
import yaml

from eated_interfaces.action import FindObject


def summarize(rows):
    """Keep failures in the denominator and summarize successful coordinates."""
    good = [r for r in rows if r['success']]
    result = {'trials': len(rows), 'successes': len(good)}
    for axis in ('x_mm', 'y_mm', 'z_raw_mm', 'z_adjusted_mm'):
        values = [r[axis] for r in good]
        result[axis] = {
            'mean': statistics.mean(values) if values else None,
            'sample_std': statistics.stdev(values) if len(values) > 1 else None,
            'min': min(values) if values else None,
            'max': max(values) if values else None,
        }
    return result


class Recorder(Node):
    """Observe images, joints, TF and measurement readiness around each trial."""

    def __init__(self):
        super().__init__('vision_baseline_recorder')
        self.ready = False
        self.ready_at = 0.0
        self.joints = None
        self.images = {}
        self.camera_info = None
        self.bridge = CvBridge()
        self.buffer = Buffer()
        self.listener = TransformListener(self.buffer, self)
        self.subs = [
            self.create_subscription(
                Bool, '/measurement/ready', self.on_ready, 10,
            ),
            self.create_subscription(
                JointState, '/joint_states', self.on_joints,
                qos_profile_sensor_data,
            ),
            self.create_subscription(
                CameraInfo, '/realsense/d435i/color/camera_info',
                self.on_camera_info, qos_profile_sensor_data,
            ),
        ]
        topics = {
            'rgb': '/realsense/d435i/color/image_raw',
            'depth': '/realsense/d435i/aligned_depth_to_color/image_raw',
            'debug': '/vision/debug_image',
        }
        for name, topic in topics.items():
            self.subs.append(self.create_subscription(
                Image, topic,
                lambda msg, key=name: self.images.update({key: msg}),
                qos_profile_sensor_data,
            ))
        self.client = ActionClient(self, FindObject, '/find_object')

    def on_ready(self, msg):
        self.ready = bool(msg.data)
        self.ready_at = time.monotonic()

    def on_joints(self, msg):
        self.joints = dict(zip(msg.name, msg.position))

    def on_camera_info(self, msg):
        self.camera_info = {
            'frame_id': msg.header.frame_id, 'width': msg.width,
            'height': msg.height, 'k': list(msg.k), 'd': list(msg.d),
            'distortion_model': msg.distortion_model,
        }

    def is_ready(self):
        return self.ready and time.monotonic() - self.ready_at < 1.0

    def wait(self, future, seconds, require_ready=True):
        deadline = time.monotonic() + seconds
        while rclpy.ok() and not future.done():
            rclpy.spin_once(self, timeout_sec=0.05)
            if require_ready and not self.is_ready():
                raise RuntimeError('Measurement readiness lost (stop or stale input)')
            if time.monotonic() >= deadline:
                raise TimeoutError('Vision action timed out')
        if not future.done():
            raise RuntimeError('ROS shutdown')
        return future.result()

    def snapshot(self, directory, trial):
        saved = {}
        for name, msg in self.images.items():
            encoding = 'passthrough' if name == 'depth' else 'bgr8'
            image = self.bridge.imgmsg_to_cv2(msg, desired_encoding=encoding)
            path = directory / f'{trial:03d}_{name}.png'
            if not cv2.imwrite(str(path), image):
                raise OSError(f'Could not write {path}')
            saved[name] = {
                'file': path.name,
                'stamp': [msg.header.stamp.sec, msg.header.stamp.nanosec],
                'encoding': msg.encoding,
            }
        return saved


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--target', choices=['coke', 'vaseline', 'tissue', 'airpod'], required=True)
    parser.add_argument('--trials', type=int, default=3)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--reference-x-mm', type=float)
    parser.add_argument('--reference-y-mm', type=float)
    args = parser.parse_args()
    if args.trials < 1:
        parser.error('--trials must be positive')
    root = Path(__file__).resolve().parents[2]
    geometry_path = root / 'docs/measurements/measured_geometry_2026-11-11.yaml'
    geometry = yaml.safe_load(geometry_path.read_text())
    args.output.mkdir(parents=True, exist_ok=False)
    rclpy.init(args=[])
    node = Recorder()
    rows = []
    active = None
    pending = None
    metadata = {
        'created': datetime.now().astimezone().isoformat(),
        'target': args.target,
        'requested_trials': args.trials,
        'reference_footprint_center_mm': {
            'x': args.reference_x_mm, 'y': args.reference_y_mm,
        },
        'reference_note': 'Vision surface points are not object footprint centers.',
        'geometry': geometry,
        'git_head': subprocess.check_output(
            ['git', 'rev-parse', 'HEAD'], cwd=root, text=True,
        ).strip(),
        'git_status': subprocess.check_output(
            ['git', 'status', '--short'], cwd=root, text=True,
        ),
        'files_sha256': {},
        'trials': [],
    }
    for rel in (
        'src/control_pkg/config/control.yaml',
        'src/vision_pkg/config/vision2.yaml',
        'src/vision_pkg/launch/vision2.launch.py',
        'src/vision_pkg/vision_pkg/vision_node.py',
        'src/vision_pkg/vision_pkg/vision_node2.py',
        'tools/vision_eval/measurement_control.py',
        'tools/vision_eval/record_vision.py',
        'best.pt',
    ):
        metadata['files_sha256'][rel] = hashlib.sha256((root / rel).read_bytes()).hexdigest()
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            rclpy.spin_once(node, timeout_sec=0.1)
            if (node.is_ready() and 'rgb' in node.images
                    and 'depth' in node.images and node.camera_info
                    and node.buffer.can_transform('base_link', 'link1', Time())):
                break
        if not node.is_ready():
            raise RuntimeError('Recognition pose is not ready; no trial was sent')
        if not all(k in node.images for k in ('rgb', 'depth')) or not node.camera_info:
            raise RuntimeError('RGB/depth/intrinsics missing; no trial was sent')
        if not node.client.wait_for_server(timeout_sec=5.0):
            raise RuntimeError('FindObject server unavailable')
        mount = node.buffer.lookup_transform('base_link', 'link1', Time())
        live_z_mm = mount.transform.translation.z * 1000
        inferred_z_mm = geometry['mounting']['arm']['derived_geometry']['base_fixed_xyz_relative_to_base_link'][2]
        correction = inferred_z_mm - live_z_mm
        metadata['live_base_to_link1_z_mm'] = live_z_mm
        metadata['inferred_mount_height_adjustment_mm'] = correction
        metadata['camera_info'] = node.camera_info
        camera_tf = node.buffer.lookup_transform(
            'base_link', node.camera_info['frame_id'], Time(),
        ).transform
        metadata['camera_tf_latest'] = {
            'translation': [camera_tf.translation.x, camera_tf.translation.y,
                            camera_tf.translation.z],
            'quaternion': [camera_tf.rotation.x, camera_tf.rotation.y,
                           camera_tf.rotation.z, camera_tf.rotation.w],
        }
        for i in range(1, args.trials + 1):
            if not node.is_ready():
                raise RuntimeError('Measurement readiness lost before trial')
            row = {
                'trial': i, 'target': args.target, 'success': False,
                'elapsed_seconds': 0, 'confidence': None,
                'x_mm': None, 'y_mm': None, 'z_raw_mm': None,
                'z_adjusted_mm': None, 'message': '',
            }
            started = time.monotonic()
            feedback = []
            pending = node.client.send_goal_async(
                FindObject.Goal(target_class=args.target),
                feedback_callback=lambda msg: feedback.append({
                    'detected': bool(msg.feedback.detected),
                    'stable_time': float(msg.feedback.stable_time),
                    'confidence': float(msg.feedback.confidence),
                }),
            )
            try:
                active = node.wait(pending, 10)
                if not active.accepted:
                    raise RuntimeError('Vision request rejected')
                response = node.wait(active.get_result_async(), 65)
                active = None
                result = response.result
                row['message'] = result.message
                row['success'] = bool(
                    response.status == 4 and result.success
                    and result.frame_id == 'base_link'
                    and all(math.isfinite(v) for v in (result.x, result.y, result.z))
                )
                if row['success']:
                    row.update(
                        confidence=float(result.confidence),
                        x_mm=result.x * 1000, y_mm=result.y * 1000,
                        z_raw_mm=result.z * 1000,
                        z_adjusted_mm=result.z * 1000 + correction,
                    )
            except Exception as exc:
                row['message'] = str(exc)
                raise
            finally:
                row['elapsed_seconds'] = time.monotonic() - started
                rows.append(row)
                details = {'trial': i, 'joints': node.joints, 'feedback': feedback}
                try:
                    details['images'] = node.snapshot(args.output, i)
                except Exception as exc:
                    details['image_save_error'] = str(exc)
                metadata['trials'].append(details)
                print(json.dumps(row), flush=True)
    except (Exception, KeyboardInterrupt) as exc:
        metadata['interrupted'] = str(exc) or 'KeyboardInterrupt'
        print(f'Recording stopped: {metadata["interrupted"]}', flush=True)
    finally:
        if active is not None and active.accepted:
            try:
                node.wait(active.cancel_goal_async(), 3, require_ready=False)
            except Exception as exc:
                metadata['cancel_error'] = str(exc)
        elif pending is not None and 'interrupted' in metadata:
            # Bound the final wait for an acknowledgement before exiting.
            try:
                late = node.wait(pending, 3, require_ready=False)
                if late.accepted:
                    node.wait(late.cancel_goal_async(), 3, require_ready=False)
            except Exception as exc:
                metadata['cancel_error'] = str(exc)
        if rows:
            with (args.output / 'trials.csv').open('w', newline='') as stream:
                writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
                writer.writeheader()
                writer.writerows(rows)
        metadata['summary'] = summarize(rows)
        (args.output / 'session.json').write_text(
            json.dumps(metadata, ensure_ascii=False, indent=2) + '\n',
        )
        print(json.dumps(metadata['summary'], indent=2), flush=True)
        node.destroy_node()
        rclpy.shutdown()
    return 1 if 'interrupted' in metadata else 0


if __name__ == '__main__':
    raise SystemExit(main())
