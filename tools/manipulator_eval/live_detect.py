#!/usr/bin/env python3
"""
Show live YOLO detections for every class (read-only, no robot motion).

The normal vision node runs YOLO only during a /find_object request and
only for the requested class. This viewer runs the same model on the live
RGB stream for all classes, so you can see how well each object is
detected from the current camera pose, and which class a missed object is
confused with.

  * Boxes: green >= 0.5 (normal threshold), yellow 0.3-0.5, grey below.
  * The arm joint angles are drawn on the image (camera pose context).
  * Every second the terminal prints the best confidence per class.
  * Keys: s = save raw + annotated image and a detection list, q = quit.

It subscribes to the camera and /joint_states only. It never publishes.

    source /opt/ros/humble/setup.bash
    source ~/2026project/install/setup.bash
    export PYTHONPATH=~/2026project/.venv/lib/python3.10/site-packages:$PYTHONPATH
    python3 tools/manipulator_eval/live_detect.py
"""

import argparse
import datetime
import math
import os
import threading
import time

import cv2
from cv_bridge import CvBridge
import numpy as np
import rclpy
from rclpy.executors import SingleThreadedExecutor
from rclpy.node import Node
from rclpy.qos import qos_profile_sensor_data
from sensor_msgs.msg import Image, JointState

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_MODEL = os.path.join(HERE, '..', '..', 'best.pt')
DEFAULT_SAVE_DIR = os.path.join(HERE, 'results', 'live_snapshots')
NORMAL_THRESHOLD = 0.5
ARM_JOINTS = ('joint1', 'joint2', 'joint3', 'joint4')


def box_color(confidence: float):
    """Return a BGR colour for a confidence value."""
    if confidence >= NORMAL_THRESHOLD:
        return (0, 200, 0)
    if confidence >= 0.3:
        return (0, 200, 255)
    return (160, 160, 160)


class FrameListener(Node):
    """Keep the newest RGB frame and arm joint angles."""

    def __init__(self, topic: str) -> None:
        super().__init__('live_detect_viewer')
        self.bridge = CvBridge()
        self.lock = threading.Lock()
        self.frame = None
        self.frame_stamp = 0.0
        self.joints = {}
        self.create_subscription(
            Image, topic, self._image_callback, qos_profile_sensor_data)
        self.create_subscription(
            JointState, '/joint_states', self._joint_callback, 10)

    def _image_callback(self, msg: Image) -> None:
        image = self.bridge.imgmsg_to_cv2(msg, desired_encoding='bgr8')
        with self.lock:
            self.frame = image
            self.frame_stamp = time.monotonic()

    def _joint_callback(self, msg: JointState) -> None:
        with self.lock:
            for name, position in zip(msg.name, msg.position):
                self.joints[name] = position

    def latest(self):
        """Return (frame copy or None, age seconds, joint dict)."""
        with self.lock:
            if self.frame is None:
                return None, math.inf, dict(self.joints)
            return (self.frame.copy(), time.monotonic() - self.frame_stamp,
                    dict(self.joints))


def detect(model, frame, min_conf: float, device):
    """Run YOLO and return [(class, conf, x1, y1, x2, y2)] sorted by conf."""
    result = model.predict(source=frame, conf=min_conf, device=device,
                           verbose=False)[0]
    detections = []
    for box in result.boxes:
        cls = int(box.cls[0])
        x1, y1, x2, y2 = (float(v) for v in box.xyxy[0])
        detections.append((model.names[cls], float(box.conf[0]),
                           x1, y1, x2, y2))
    return sorted(detections, key=lambda d: -d[1])


def annotate(frame, detections, joints, fps: float):
    """Draw boxes, labels, joint angles and the image centre line."""
    image = frame.copy()
    height, width = image.shape[:2]
    cv2.line(image, (0, height // 2), (width, height // 2), (90, 90, 90), 1)
    for name, conf, x1, y1, x2, y2 in detections:
        color = box_color(conf)
        p1, p2 = (int(x1), int(y1)), (int(x2), int(y2))
        cv2.rectangle(image, p1, p2, color, 2)
        center = (int((x1 + x2) / 2), int((y1 + y2) / 2))
        cv2.circle(image, center, 4, color, -1)
        cv2.putText(image, f'{name} {conf:.2f}  row {center[1]}',
                    (p1[0], max(18, p1[1] - 6)), cv2.FONT_HERSHEY_SIMPLEX,
                    0.6, color, 2, cv2.LINE_AA)
    if all(j in joints for j in ARM_JOINTS):
        pose = ', '.join(f'{math.degrees(joints[j]):.0f}' for j in ARM_JOINTS)
        text = f'joints [{pose}] deg'
    else:
        text = 'joints: no /joint_states'
    for row, line in enumerate((text, f'YOLO {fps:.1f} Hz | s=save q=quit',
                                'green>=0.5  yellow>=0.3  grey<0.3')):
        cv2.putText(image, line, (10, 26 + 24 * row),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.65, (255, 255, 255), 2,
                    cv2.LINE_AA)
    return image


def save_snapshot(save_dir, frame, image, detections, joints) -> str:
    """Save raw and annotated frames plus a text list of detections."""
    os.makedirs(save_dir, exist_ok=True)
    stamp = datetime.datetime.now().strftime('%Y%m%d_%H%M%S')
    base = os.path.join(save_dir, stamp)
    cv2.imwrite(base + '_raw.png', frame)
    cv2.imwrite(base + '_annotated.png', image)
    with open(base + '.txt', 'w', encoding='utf-8') as handle:
        pose = [round(math.degrees(joints[j]), 1) for j in ARM_JOINTS
                if j in joints]
        handle.write(f'joints_deg {pose}\n')
        for name, conf, x1, y1, x2, y2 in detections:
            handle.write(f'{name} {conf:.3f} {x1:.0f} {y1:.0f} '
                         f'{x2:.0f} {y2:.0f}\n')
    return base


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    parser.add_argument('--model', default=DEFAULT_MODEL)
    parser.add_argument('--topic', default='/realsense/d435i/color/image_raw')
    parser.add_argument('--min-conf', type=float, default=0.10)
    parser.add_argument('--rate', type=float, default=5.0,
                        help='maximum YOLO runs per second')
    parser.add_argument('--save-dir', default=DEFAULT_SAVE_DIR)
    parser.add_argument('--no-window', action='store_true',
                        help='terminal summary only')
    args = parser.parse_args()

    import torch  # noqa: E402  (heavy imports after argument parsing)
    from ultralytics import YOLO  # noqa: E402

    device = 0 if torch.cuda.is_available() else 'cpu'
    model = YOLO(os.path.abspath(args.model))
    model.predict(source=np.zeros((640, 640, 3), dtype=np.uint8),
                  device=device, verbose=False)  # warm-up
    classes = [model.names[i] for i in sorted(model.names)]
    print(f'model={os.path.abspath(args.model)} classes={classes} '
          f'device={"cuda" if device == 0 else "cpu"}', flush=True)

    rclpy.init()
    node = FrameListener(args.topic)
    executor = SingleThreadedExecutor()
    executor.add_node(node)
    spin = threading.Thread(target=executor.spin, daemon=True)
    spin.start()

    window = 'live_detect (all classes)'
    period = 1.0 / max(0.5, args.rate)
    best = {name: 0.0 for name in classes}
    last_print = time.monotonic()
    fps = 0.0
    try:
        while rclpy.ok():
            started = time.monotonic()
            frame, age, joints = node.latest()
            if frame is None or age > 1.0:
                if time.monotonic() - last_print > 2.0:
                    print('waiting for camera frames...', flush=True)
                    last_print = time.monotonic()
                time.sleep(0.1)
                continue
            detections = detect(model, frame, args.min_conf, device)
            for name, conf, *_ in detections:
                best[name] = max(best[name], conf)
            image = annotate(frame, detections, joints, fps)
            if time.monotonic() - last_print >= 1.0:
                line = ' | '.join(
                    f'{name} {best[name]:.2f}' if best[name] else f'{name} -'
                    for name in classes)
                print(line, flush=True)
                best = {name: 0.0 for name in classes}
                last_print = time.monotonic()
            if not args.no_window:
                cv2.imshow(window, image)
                key = cv2.waitKey(1) & 0xFF
                if key == ord('q'):
                    break
                if key == ord('s'):
                    base = save_snapshot(args.save_dir, frame, image,
                                         detections, joints)
                    print(f'saved {base}_*.png', flush=True)
            elapsed = time.monotonic() - started
            fps = 1.0 / max(elapsed, period)
            time.sleep(max(0.0, period - elapsed))
    except KeyboardInterrupt:
        pass
    finally:
        cv2.destroyAllWindows()
        executor.shutdown()
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()


if __name__ == '__main__':
    main()
