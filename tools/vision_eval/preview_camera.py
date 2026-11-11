"""Display live RGB frames locally without recording or sending robot commands."""

import time

from cv_bridge import CvBridge
import cv2
import rclpy
from rclpy.node import Node
from rclpy.qos import qos_profile_sensor_data
from sensor_msgs.msg import Image


def main():
    """Show the unmirrored camera image until the window closes or Esc is pressed."""
    rclpy.init()
    node = Node('calibration_camera_preview')
    bridge = CvBridge()
    latest = {'image': None, 'received': 0.0}
    title = 'EATED - Live camera'

    def receive(msg):
        if latest['image'] is None:
            print(f'Live RGB received: {msg.width}x{msg.height}', flush=True)
        latest['image'] = bridge.imgmsg_to_cv2(msg, desired_encoding='bgr8')
        latest['received'] = time.monotonic()

    subscription = node.create_subscription(
        Image, '/realsense/d435i/color/image_raw', receive, qos_profile_sensor_data)
    cv2.namedWindow(title, cv2.WINDOW_NORMAL)
    cv2.resizeWindow(title, 960, 540)
    cv2.moveWindow(title, 40, 60)
    displayed = False
    try:
        while rclpy.ok():
            rclpy.spin_once(node, timeout_sec=0.03)
            if latest['image'] is not None:
                frame = latest['image'].copy()
                stale = time.monotonic() - latest['received'] > 1.0
                label = 'NO FRESH IMAGE' if stale else 'LIVE | Esc / Q: close preview'
                color = (0, 0, 255) if stale else (0, 220, 0)
                cv2.putText(frame, label, (16, 30), cv2.FONT_HERSHEY_SIMPLEX,
                            0.7, color, 2, cv2.LINE_AA)
                cv2.imshow(title, frame)
                displayed = True
            if cv2.waitKey(1) & 0xff in [27, ord('q'), ord('Q')]:
                break
            if displayed:
                # GTK may return -1 for the unsupported visibility property.
                visible = cv2.getWindowProperty(title, cv2.WND_PROP_VISIBLE)
                if 0 <= visible < 1:
                    break
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_subscription(subscription)
        node.destroy_node()
        cv2.destroyAllWindows()
        if rclpy.ok():
            rclpy.shutdown()


if __name__ == '__main__':
    main()
