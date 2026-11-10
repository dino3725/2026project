"""ROS2 node: microphone -> VAD -> Whisper -> Ollama -> /voice/robot_command."""

from __future__ import annotations

import sys
import threading
from typing import Any

from eated_interfaces.msg import RobotCommand
import rclpy
from rclpy.node import Node

from voice_command_pkg.command_protocol import command_payload, TOPIC_NAME
from voice_command_pkg.parameters import build_pipeline_config, DEFAULT_PARAMETERS
from voice_command_pkg.pipeline import PipelineError, PipelineStopped, VoiceCommandPipeline


class VoiceCommandNode(Node):
    """Run the blocking voice pipeline in a worker thread and publish results."""

    def __init__(self) -> None:
        super().__init__('voice_command_node')
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
        self.worker = threading.Thread(target=self._run_pipeline, daemon=True)
        if not self.publish_enabled:
            self.get_logger().warning('publish_commands=false: 명령을 발행하지 않습니다.')

    def start(self) -> None:
        self.worker.start()

    def publish_command(self, command: Any, transcript: str) -> bool:
        """Publish only validated fetch/stop commands; unknown is never published."""
        payload = command_payload(command)
        if payload is None:
            if command == {'action': 'unknown', 'object': ''}:
                self.get_logger().info('unknown 명령: 발행하지 않습니다.')
            return False
        if not isinstance(transcript, str):
            return False
        if not self.publish_enabled:
            return False

        action, target_class = payload
        message = RobotCommand()
        message.header.stamp = self.get_clock().now().to_msg()
        message.header.frame_id = ''
        message.action = action
        message.target_class = target_class
        message.transcript = transcript
        with self.publish_lock:
            if self.stop_event.is_set():
                return False
            self.publisher.publish(message)
            return True

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
