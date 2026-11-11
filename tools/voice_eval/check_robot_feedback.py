#!/usr/bin/env python3
"""Exercise real DDS feedback on unique test topics; never send a robot command."""

import argparse
import json
import os
from pathlib import Path
import time
import uuid

# Keep discovery local even if the caller's voice_env uses the robot's domain.
os.environ['ROS_LOCALHOST_ONLY'] = '1'

from eated_interfaces.msg import RobotCommand, RobotCommandStatus  # noqa: E402
import rclpy  # noqa: E402
from rclpy.executors import SingleThreadedExecutor  # noqa: E402
from rclpy.node import Node  # noqa: E402
from rclpy.parameter import Parameter  # noqa: E402
from rclpy.qos import DurabilityPolicy, QoSProfile, ReliabilityPolicy  # noqa: E402
from std_msgs.msg import String  # noqa: E402

from voice_command_pkg.voice_command_node import VoiceCommandNode  # noqa: E402


def main():
    """Verify retained state, status ordering, disconnects and dry-run publication."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error('output already exists; choose a new file')
    prefix = '/feedback_check_' + uuid.uuid4().hex
    state_topic, status_topic, command_topic = [prefix + '/' + name for name in (
        'state', 'status', 'command'
    )]
    ros_args = ['--ros-args']
    for original, target in (
        ('/control/state', state_topic), ('/control/command_status', status_topic),
        ('/voice/robot_command', command_topic),
    ):
        ros_args.extend(['-r', original + ':=' + target])
    rclpy.init(args=ros_args, domain_id=91)
    executor = SingleThreadedExecutor()
    controller = Node('feedback_fixture')
    nodes = [controller]
    checks, events, commands = [], [], []
    state_qos = QoSProfile(
        depth=1, reliability=ReliabilityPolicy.RELIABLE,
        durability=DurabilityPolicy.TRANSIENT_LOCAL,
    )
    report = {'domain_id': 91, 'localhost_only': True, 'topic_prefix': prefix, 'checks': checks}

    def wait_until(predicate, timeout=5):
        deadline = time.perf_counter() + timeout
        while not predicate():
            if time.perf_counter() >= deadline:
                raise TimeoutError('DDS condition not reached')
            executor.spin_once(timeout_sec=0.05)

    def record(name, condition):
        if not condition:
            raise AssertionError(name)
        checks.append(name)
        print('[PASS] ' + name, flush=True)

    def timing():
        now = time.perf_counter()
        return {'voice_start_wall': now, 'utterance_start_wall': now, 'voice_end_wall': now}

    try:
        state_writer = controller.create_publisher(String, state_topic, state_qos)
        status_writer = controller.create_publisher(RobotCommandStatus, status_topic, 10)
        controller.create_subscription(RobotCommand, command_topic, commands.append, 10)
        state_writer.publish(String(data='IDLE'))  # Publish before creating the subscriber.
        voice = VoiceCommandNode(parameter_overrides=[Parameter('publish_commands', value=False)])
        nodes.append(voice)
        original_notice = voice._notice

        def notice(event):
            events.append(event)
            original_notice(event)

        voice._notice = notice
        for node in nodes:
            executor.add_node(node)
        record('all robot topics remapped and publication disabled', (
            not voice.publish_enabled and voice.publisher.topic_name == command_topic
            and voice.state_subscription.topic_name == state_topic
            and voice.status_subscription.topic_name == status_topic
        ))
        wait_until(lambda: voice.feedback.state == 'IDLE')
        record('late subscriber receives retained IDLE', True)
        record('fresh IDLE speech admitted', voice.check_fetch_allowed(time.perf_counter()) == '')
        record('dry-run fetch neither publishes nor reserves', (
            not voice.publish_command({'action': 'fetch', 'object': 'coke'}, '콜라', timing())
            and voice.feedback.pending is None
        ))

        # Establish an in-memory pending command to test status callbacks without
        # emitting a fetch message, even on the isolated command topic.
        voice.feedback.mark_sent('fetch', 'coke', time.perf_counter())
        state_writer.publish(String(data='MOVE_TO_RECOGNITION'))
        wait_until(lambda: voice.feedback.state == 'MOVE_TO_RECOGNITION')
        status_writer.publish(RobotCommandStatus(
            accepted=True, action='fetch', target_class='coke', message='test accepted'
        ))
        wait_until(lambda: ('robot_accepted', 'coke') in events)
        record('status after state transition gives acceptance feedback', True)
        record('busy fetch blocked', (
            voice.check_fetch_allowed(time.perf_counter()) == 'robot_busy'
        ))
        state_writer.publish(String(data='DRIVE_TO_PLACE'))
        wait_until(lambda: ('robot_carrying', 'coke') in events)
        record('carry feedback follows accepted target and state', True)
        old_start = time.perf_counter()
        state_writer.publish(String(data='IDLE'))
        wait_until(lambda: voice.feedback.state == 'IDLE')
        record('busy-era speech rejected after return to IDLE', (
            voice.check_fetch_allowed(old_start) == 'robot_before_idle'
        ))

        voice.feedback.mark_sent('fetch', 'tissue', time.perf_counter() - 6)
        wait_until(lambda: voice.feedback.pending.timed_out)
        record('timer reports missing response without unlocking', (
            ('robot_response_timeout', '') in events
            and voice.check_fetch_allowed(time.perf_counter()) == 'robot_response_timeout'
        ))
        # The stop path is exercised with publication disabled.
        record('dry-run stop remains independent of pending fetch', not voice.publish_command(
            {'action': 'stop', 'object': ''}, '멈춰', timing()
        ))

        controller.destroy_publisher(state_writer)
        wait_until(lambda: voice.feedback.state is None)
        record('publisher disappearance clears cached IDLE and pending request', (
            voice.check_fetch_allowed(time.perf_counter()) == 'robot_unknown'
            and voice.feedback.pending is None
        ))
        state_writer = controller.create_publisher(String, state_topic, state_qos)
        state_writer.publish(String(data='IDLE'))
        wait_until(lambda: voice.feedback.state == 'IDLE')
        record('new state publisher restores readiness from a new sample', (
            voice.check_fetch_allowed(time.perf_counter()) == ''
        ))

        second = Node('second_feedback_fixture')
        nodes.append(second)
        executor.add_node(second)
        duplicate = second.create_publisher(String, state_topic, state_qos)
        duplicate.publish(String(data='IDLE'))
        wait_until(lambda: voice.feedback.state is None)
        record('ambiguous multiple state publishers block fetch', (
            voice.check_fetch_allowed(time.perf_counter()) == 'robot_unknown'
        ))
        second.destroy_publisher(duplicate)
        state_writer.publish(String(data='IDLE'))
        wait_until(lambda: voice.feedback.state == 'IDLE')
        record('single controller recovers after ambiguity', True)

        voice.feedback.mark_sent('stop', '', time.perf_counter())
        state_writer.publish(String(data='ERROR'))
        wait_until(lambda: voice.feedback.state == 'ERROR')
        status_writer.publish(RobotCommandStatus(accepted=True, action='stop', target_class=''))
        wait_until(lambda: ('robot_stop_accepted', '') in events)
        record('stop acceptance is distinct from sending or physical completion', True)
        state_writer.publish(String(data='RETURN_HOME'))
        wait_until(lambda: voice.feedback.state == 'RETURN_HOME')
        state_writer.publish(String(data='IDLE'))
        wait_until(lambda: voice.feedback.state == 'IDLE')
        record('recovery to IDLE admits new speech', (
            voice.check_fetch_allowed(time.perf_counter()) == ''
        ))
        record('no robot command messages emitted', commands == [])
        report['passed'] = True
    except Exception as exc:
        report['passed'] = False
        report['error'] = f'{type(exc).__name__}: {exc}'
        raise
    finally:
        report['feedback_events'] = events
        report['command_messages'] = len(commands)
        for node in reversed(nodes):
            if isinstance(node, VoiceCommandNode):
                node.stop()
            executor.remove_node(node)
            node.destroy_node()
        executor.shutdown()
        rclpy.shutdown()
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open('x', encoding='utf-8') as handle:
            json.dump(report, handle, ensure_ascii=False, indent=2)
            handle.write('\n')
        print('[REPORT] ' + str(args.output), flush=True)


if __name__ == '__main__':
    main()
