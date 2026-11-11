"""Request one indexed pose and wait for completion; never chain pose requests."""

import argparse
import json
from pathlib import Path
import time

import rclpy
from rclpy.node import Node
from std_msgs.msg import Bool, String
from std_srvs.srv import Trigger

from eated_interfaces.msg import RobotCommand


def main():
    """Require live progress, send one Trigger, and stop on uncertain completion."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--pose', type=int, choices=range(1, 16), required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise ValueError('Output already exists; do not accidentally repeat a pose')
    rclpy.init(args=[])
    node = Node('one_calibration_step')
    state = {'status': None, 'status_at': 0.0, 'ready': False, 'ready_at': 0.0}

    def status(msg):
        state.update(status=json.loads(msg.data), status_at=time.monotonic())

    def ready(msg):
        state.update(ready=msg.data, ready_at=time.monotonic())

    node.create_subscription(String, '/measurement/calibration_status', status, 10)
    node.create_subscription(Bool, '/measurement/ready', ready, 10)
    stop_pub = node.create_publisher(RobotCommand, '/voice/robot_command', 10)
    client = node.create_client(Trigger, f'/measurement/calibration_pose_{args.pose:02d}')
    requested = False
    result = {'pose': args.pose, 'completed': False}
    try:
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            rclpy.spin_once(node, timeout_sec=0.05)
            if (state['status'] is not None and time.monotonic() - state['status_at'] < 0.5
                    and client.service_is_ready()):
                break
        else:
            raise RuntimeError('No fresh calibration status/service; no request sent')
        before = state['status']
        result['before'] = before
        if before['blocked_reason'] or before['preparing']:
            raise RuntimeError(f'Controller gate: {before}')
        if before['completed_pose'] != args.pose - 1:
            raise RuntimeError('Unexpected previous pose; no request sent')
        if args.pose > 1 and before['stopped']:
            raise RuntimeError('Stopped session requires inspection; no automatic resume')
        requested = True
        requested_at = time.monotonic()
        future = client.call_async(Trigger.Request())
        rclpy.spin_until_future_complete(node, future, timeout_sec=5)
        if not future.done() or future.exception():
            raise RuntimeError('Trigger acceptance outcome unknown')
        response = future.result()
        result['acceptance'] = {'success': response.success, 'message': response.message}
        if not response.success:
            requested = False
            raise RuntimeError(response.message)
        deadline = time.monotonic() + 18
        while time.monotonic() < deadline:
            rclpy.spin_once(node, timeout_sec=0.05)
            now = time.monotonic()
            current = state['status']
            if (now - state['status_at'] < 0.5 and current['completed_pose'] == args.pose
                    and not current['preparing'] and not current['stopped']
                    and not current['blocked_reason'] and state['ready']
                    and now - state['ready_at'] < 0.5):
                result.update(completed=True, after=current,
                              elapsed_seconds=now - requested_at)
                requested = False
                break
            if now - requested_at > 2 and current['stopped']:
                raise RuntimeError('Calibration controller stopped; no next pose requested')
        else:
            raise RuntimeError('Pose completion timeout')
    except Exception as exc:
        result['error'] = str(exc)
        if requested:
            message = RobotCommand()
            message.action = 'stop'
            message.header.stamp = node.get_clock().now().to_msg()
            for _ in range(3):
                stop_pub.publish(message)
                rclpy.spin_once(node, timeout_sec=0.1)
            result['stop_requested'] = True
        raise
    finally:
        with args.output.open('x') as stream:
            stream.write(json.dumps(result, indent=2) + '\n')
        print(json.dumps(result, indent=2))
        node.destroy_node()
        rclpy.shutdown()


if __name__ == '__main__':
    main()
