#!/usr/bin/env python3
"""Thin, safety-filtered ROS2 publisher for EATED robot commands."""

from __future__ import annotations

from typing import Any

import rclpy
from eated_interfaces.msg import RobotCommand
from rclpy.context import Context
from rclpy.node import Node


TOPIC_NAME = "/voice/robot_command"
VALID_OBJECTS = frozenset({"coke", "vaseline", "tissue", "airpod"})


def command_payload(command: Any) -> tuple[str, str] | None:
    """Return canonical action/target fields for an allowed command."""
    if not isinstance(command, dict) or set(command) != {"action", "object"}:
        return None
    action = command.get("action")
    obj = command.get("object")
    if action == "stop" and obj == "":
        return "stop", ""
    if action == "fetch" and obj in VALID_OBJECTS:
        return "fetch", obj
    return None


class RobotCommandPublisher:
    """Own a local ROS context and publish validated RobotCommand messages."""

    def __init__(self, node_name: str = "robot_command_publisher") -> None:
        self.context = Context()
        self.context.init(args=None)
        self.node = Node(node_name, context=self.context)
        self.publisher = self.node.create_publisher(RobotCommand, TOPIC_NAME, 10)
        self._closed = False

    def publish_command(self, command: Any, transcript: str) -> bool:
        payload = command_payload(command)
        if payload is None:
            if command == {"action": "unknown", "object": ""}:
                self.node.get_logger().info(
                    "Unknown command. ROS message not published."
                )
            return False
        if not isinstance(transcript, str):
            return False

        action, target_class = payload
        message = RobotCommand()
        message.header.stamp = self.node.get_clock().now().to_msg()
        message.header.frame_id = ""
        message.action = action
        message.target_class = target_class
        message.transcript = transcript
        self.publisher.publish(message)
        return True

    def close(self) -> None:
        if self._closed:
            return
        self.node.destroy_node()
        self.context.shutdown()
        self._closed = True

    def __enter__(self) -> "RobotCommandPublisher":
        return self

    def __exit__(self, _exc_type: object, _exc: object, _traceback: object) -> None:
        self.close()


__all__ = [
    "RobotCommandPublisher",
    "TOPIC_NAME",
    "VALID_OBJECTS",
    "command_payload",
]
