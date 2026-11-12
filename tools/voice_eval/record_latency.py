#!/usr/bin/env python3
"""Subscribe to timing evidence only; never create a command publisher."""

import argparse
import json
import os
from pathlib import Path
import socket
import subprocess
import time


def chrony_snapshot():
    result = {}
    for name, command in [('tracking', ['chronyc', '-n', 'tracking']),
                          ('sources', ['chronyc', '-n', 'sources', '-v'])]:
        try:
            run = subprocess.run(command, capture_output=True, text=True, timeout=3,
                                 env={**os.environ, 'LC_ALL': 'C'})
            result[name] = {'returncode': run.returncode, 'stdout': run.stdout,
                            'stderr': run.stderr}
        except (OSError, subprocess.TimeoutExpired) as exc:
            result[name] = {'error': str(exc)}
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--role', choices=['laptop', 'jetson'], required=True)
    parser.add_argument('--seconds', type=float, default=40)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if not 1 <= args.seconds <= 600:
        parser.error('seconds must be between 1 and 600')
    args.output.parent.mkdir(parents=True, exist_ok=True)
    # Refuse overwriting before opening any ROS resources.
    output = args.output.open('x', encoding='utf-8')
    from eated_interfaces.msg import RobotCommand, RobotCommandStatus
    import rclpy
    from rclpy.node import Node
    from rclpy.qos import DurabilityPolicy, QoSProfile, ReliabilityPolicy
    from std_msgs.msg import String

    events = []
    metadata = {'schema': 1, 'role': args.role, 'read_only': True,
                'clock_domain': socket.gethostname() + ':' + Path(
                    '/proc/sys/kernel/random/boot_id').read_text().strip(),
                'chrony_start': chrony_snapshot()}
    rclpy.init(args=[])
    node = Node('latency_observer_' + args.role)
    metadata['use_sim_time'] = node.get_parameter('use_sim_time').value
    topics = {'command': '/voice/robot_command', 'status': '/control/command_status',
              'state': '/control/state'}

    def observe(kind, message):
        row = {'event': kind, 'unix_ns': time.time_ns(),
               'monotonic_ns': time.perf_counter_ns()}
        if kind == 'state':
            row['state'] = message.data
        else:
            row.update(stamp_ns=message.header.stamp.sec * 1000000000
                       + message.header.stamp.nanosec,
                       action=message.action, target_class=message.target_class)
            if kind == 'status':
                row['accepted'] = message.accepted
        row['publishers'] = [
            {'name': e.node_name, 'namespace': e.node_namespace,
             'type': e.topic_type, 'gid': bytes(e.endpoint_gid).hex()}
            for e in node.get_publishers_info_by_topic(topics[kind])
        ]
        events.append(row)
        print(json.dumps(row, ensure_ascii=False), flush=True)

    qos = QoSProfile(depth=10, reliability=ReliabilityPolicy.RELIABLE,
                     durability=DurabilityPolicy.TRANSIENT_LOCAL)
    node.create_subscription(RobotCommand, topics['command'],
                             lambda msg: observe('command', msg), 10)
    node.create_subscription(RobotCommandStatus, topics['status'],
                             lambda msg: observe('status', msg), 10)
    node.create_subscription(String, topics['state'], lambda msg: observe('state', msg), qos)
    metadata['start_unix_ns'] = time.time_ns()
    print('READY: read-only observer; perform ONE command trial, then wait.', flush=True)
    deadline = time.perf_counter() + args.seconds
    try:
        while rclpy.ok() and time.perf_counter() < deadline:
            rclpy.spin_once(node, timeout_sec=0.1)
    except KeyboardInterrupt:
        pass
    finally:
        metadata['end_unix_ns'] = time.time_ns()
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()
        metadata['chrony_end'] = chrony_snapshot()
        json.dump({'metadata': metadata, 'events': events}, output, ensure_ascii=False, indent=2)
        output.write('\n')
        output.close()
    print(f'results: {args.output}')


if __name__ == '__main__':
    main()
