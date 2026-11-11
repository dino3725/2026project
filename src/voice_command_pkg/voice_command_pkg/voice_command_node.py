"""ROS2 node: microphone -> VAD -> Whisper -> Ollama -> /voice/robot_command."""

from __future__ import annotations

from functools import partial
import math
import sys
import threading
import time
from typing import Any

from eated_interfaces.msg import RobotCommand, RobotCommandStatus
import rclpy
from rclpy.node import Node
from rclpy.qos import DurabilityPolicy, QoSProfile, ReliabilityPolicy
from std_msgs.msg import String

from voice_command_pkg.command_protocol import command_payload, TOPIC_NAME
from voice_command_pkg.parameters import build_pipeline_config, DEFAULT_PARAMETERS
from voice_command_pkg.pipeline import PipelineError, PipelineStopped, VoiceCommandPipeline
from voice_command_pkg.robot_feedback import (
    DEFAULT_MESSAGES,
    render_feedback,
    RobotFeedback,
    validate_templates,
)


class VoiceCommandNode(Node):
    """Run the blocking voice pipeline in a worker thread and publish results."""

    def __init__(self, **kwargs) -> None:
        super().__init__('voice_command_node', **kwargs)
        values = {
            name: self.declare_parameter(name, default).value
            for name, default in DEFAULT_PARAMETERS.items()
        }
        self.publish_enabled = bool(values.pop('publish_commands'))
        self.pipeline_config = build_pipeline_config(values)
        self.publisher = self.create_publisher(RobotCommand, TOPIC_NAME, 10)
        self.stop_event = threading.Event()
        self.publish_lock = threading.RLock()
        self._closing = False
        self.failed = False
        self.feedback = RobotFeedback(float(values['robot.response_timeout_seconds']))
        self.feedback_messages = {
            key: values[f'messages.{key}'] for key in DEFAULT_MESSAGES
        }
        validate_templates(self.feedback_messages)
        poll_seconds = float(values['robot.connection_poll_seconds'])
        if not math.isfinite(poll_seconds) or poll_seconds <= 0:
            raise ValueError('robot.connection_poll_seconds must be finite and positive')
        self._controller_source = None
        self._graph_problem = ''
        self._subscription_generation = 0
        self._state_qos = QoSProfile(
            depth=1, reliability=ReliabilityPolicy.RELIABLE,
            durability=DurabilityPolicy.TRANSIENT_LOCAL,
        )
        self.state_subscription = None
        self.status_subscription = None
        self._replace_feedback_subscriptions()
        self.feedback_timer = self.create_timer(poll_seconds, self._feedback_tick)
        self.worker = threading.Thread(target=self._run_pipeline, daemon=True)
        if not self.publish_enabled:
            self.get_logger().warning('publish_commands=false: 명령을 발행하지 않습니다.')

    def start(self) -> None:
        self.worker.start()

    def publish_command(self, command: Any, transcript: str, capture=None) -> bool:
        """Recheck robot state and timing atomically with the actual publication."""
        with self.publish_lock:
            if self._closing or self.stop_event.is_set():
                return False
            payload = command_payload(command)
            if payload is None:
                if command == {'action': 'unknown', 'object': ''}:
                    self._notice(('robot_unknown_command', ''))
                return False
            if not isinstance(transcript, str):
                return False
            action, target_class = payload
            message = RobotCommand()
            message.header.stamp = self.get_clock().now().to_msg()
            message.header.frame_id = ''
            message.action = action
            message.target_class = target_class
            message.transcript = transcript
            if action == 'fetch':
                try:
                    start = float(capture.get(
                        'utterance_start_wall', capture['voice_start_wall']
                    ))
                    deadline = float(capture['voice_end_wall'])
                    deadline += self.pipeline_config.max_fetch_age_seconds
                    if not math.isfinite(start) or not math.isfinite(deadline):
                        raise ValueError('invalid timestamp')
                except (AttributeError, KeyError, TypeError, ValueError):
                    self._notice(('robot_invalid_timing', ''))
                    return False
                if self.check_fetch_allowed(start):
                    return False
                if time.perf_counter() > deadline:
                    self._notice(('robot_expired', ''))
                    return False
            if self._closing or self.stop_event.is_set():
                return False
            if not self.publish_enabled:
                self._notice(('robot_dry_run', ''))
                return False
            self.feedback.mark_sent(action, target_class, time.perf_counter())
            try:
                self.publisher.publish(message)
            except Exception as exc:
                self.feedback.mark_send_uncertain()
                self.get_logger().error(f'[ROS ERROR] {type(exc).__name__}: {exc}')
                self._notice(('robot_send_error', ''))
                return False
            if action == 'stop':
                self._notice(('robot_stop_sent', ''))
            return True

    def _notice(self, event) -> None:
        if not self._closing and not self.stop_event.is_set():
            self.get_logger().info(
                f'[FEEDBACK] event={event[0]} ' + render_feedback(self.feedback_messages, event)
            )

    def _apply_state(self, state) -> None:
        previous = self.feedback.state
        events = self.feedback.observe_state(state, time.perf_counter())
        if previous != self.feedback.state:
            self.get_logger().info(f'[ROBOT STATE] {previous} -> {self.feedback.state}')
        for event in events:
            self._notice(event)

    def _replace_feedback_subscriptions(self) -> None:
        """Discard queued old-source callbacks and request a new retained state."""
        self._subscription_generation += 1
        for subscription in (self.state_subscription, self.status_subscription):
            if subscription is not None:
                self.destroy_subscription(subscription)
        self.state_subscription = self.create_subscription(
            String, '/control/state', partial(
                self._on_robot_state, generation=self._subscription_generation
            ), self._state_qos,
        )
        self.status_subscription = self.create_subscription(
            RobotCommandStatus, '/control/command_status', partial(
                self._on_command_status, generation=self._subscription_generation
            ), 10,
        )

    def _refresh_robot_connection(self) -> bool:
        """Require one state publisher; detect disappearance/replacement via DDS graph."""
        try:
            endpoints = self.get_publishers_info_by_topic(self.state_subscription.topic_name)
        except Exception as exc:
            endpoints = []
            problem = f'{type(exc).__name__}: {exc}'
        else:
            problem = '' if len(endpoints) == 1 else f'state_publishers={len(endpoints)}'
        if problem != self._graph_problem:
            self._graph_problem = problem
            self.get_logger().info('[ROBOT CONNECTION] ' + (problem or 'one state publisher'))
        source = None
        if len(endpoints) == 1 and endpoints[0].topic_type == 'std_msgs/msg/String':
            endpoint = endpoints[0]
            source = (bytes(endpoint.endpoint_gid), endpoint.node_name, endpoint.node_namespace)
        if source == self._controller_source:
            return False
        self._controller_source = source
        self.feedback.reset()
        self.get_logger().info('[ROBOT CONNECTION] state reset; awaiting current controller state')
        self._replace_feedback_subscriptions()
        return True

    def _on_robot_state(self, message, generation) -> None:
        with self.publish_lock:
            if self._closing or self.stop_event.is_set():
                return
            if generation != self._subscription_generation:
                return
            if self._refresh_robot_connection():
                return
            if self._controller_source is not None:
                self._apply_state(message.data)

    def _on_command_status(self, message, generation) -> None:
        with self.publish_lock:
            if self._closing or self.stop_event.is_set():
                return
            if generation != self._subscription_generation:
                return
            if self._refresh_robot_connection():
                return
            if self._controller_source is None:
                return
            try:
                endpoints = self.get_publishers_info_by_topic(self.status_subscription.topic_name)
            except Exception as exc:
                self.get_logger().info(f'[ROBOT STATUS] graph unavailable: {exc}')
                return
            if len(endpoints) != 1:
                return
            endpoint = endpoints[0]
            if (
                (endpoint.node_name, endpoint.node_namespace) != self._controller_source[1:]
                or endpoint.topic_type != 'eated_interfaces/msg/RobotCommandStatus'
            ):
                return
            now = time.perf_counter()
            for event in self.feedback.tick(now):
                self._notice(event)
            events = self.feedback.observe_status(
                message.action, message.target_class, message.accepted, now
            )
            self.get_logger().info(
                f'[ROBOT STATUS] action={message.action} target={message.target_class} '
                f'accepted={message.accepted} feedback={bool(events)} '
                f'message={message.message!r}'
            )
            for event in events:
                self._notice(event)

    def _feedback_tick(self) -> None:
        with self.publish_lock:
            if self._closing or self.stop_event.is_set():
                return
            self._refresh_robot_connection()
            for event in self.feedback.tick(time.perf_counter()):
                self._notice(event)

    def check_fetch_allowed(self, utterance_start: float) -> str:
        """Check before LLM and again at publication; stop never calls this gate."""
        with self.publish_lock:
            if self._closing or self.stop_event.is_set():
                return 'robot_shutdown'
            self._feedback_tick()
            reason = self.feedback.fetch_drop_reason(utterance_start)
            if reason:
                self.get_logger().info(f'[ROBOT GATE] reason={reason}')
                self._notice((reason, ''))
            return reason

    def _log_pipeline(self, text: str) -> None:
        with self.publish_lock:
            if not self._closing:
                self.get_logger().info(text)

    def _report_failure(self, text: str) -> None:
        with self.publish_lock:
            if not self._closing:
                self.failed = True
                self.get_logger().error(text)

    def _run_pipeline(self) -> None:
        try:
            pipeline = VoiceCommandPipeline(
                self.pipeline_config,
                publish=self.publish_command,
                log=self._log_pipeline,
                stop_event=self.stop_event,
                fetch_gate=self.check_fetch_allowed,
            )
            pipeline.run_forever()
        except PipelineStopped:
            pass
        except PipelineError as exc:
            self._report_failure(exc.describe())
        except Exception as exc:  # noqa: B902 - report any worker crash
            self._report_failure(f'음성 파이프라인 오류: {type(exc).__name__}: {exc}')

    def stop(self) -> None:
        with self.publish_lock:
            self._closing = True
            self.stop_event.set()
        if self.worker.is_alive():
            # Capture polls at most every 100 ms, even on a stalled pipe.
            # In-flight STT/LLM can finish later but cannot publish or log.
            self.worker.join(timeout=3.0)


def main(args: list[str] | None = None) -> int:
    rclpy.init(args=args)
    node = VoiceCommandNode()
    node.start()
    try:
        while rclpy.ok() and node.worker.is_alive():
            rclpy.spin_once(node, timeout_sec=0.2)
    except KeyboardInterrupt:
        pass
    finally:
        node.stop()
        failed = node.failed
        node.destroy_node()
        rclpy.try_shutdown()
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())
