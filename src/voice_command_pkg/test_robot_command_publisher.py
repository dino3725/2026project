#!/usr/bin/env python3
"""Local ROS2 publisher/subscriber verification without the LLM or microphone."""

from __future__ import annotations

import argparse
import time

from eated_interfaces.msg import RobotCommand
from rclpy.executors import SingleThreadedExecutor
from rclpy.node import Node

from robot_command_publisher import RobotCommandPublisher, TOPIC_NAME


PUBLISHABLE_COMMANDS = [
    {"action": "fetch", "object": "coke"},
    {"action": "fetch", "object": "vaseline"},
    {"action": "fetch", "object": "tissue"},
    {"action": "fetch", "object": "airpod"},
    {"action": "stop", "object": ""},
]
BLOCKED_COMMANDS = [
    {"action": "unknown", "object": ""},
    {"action": "fetch", "object": "water"},
    {"action": "fetch", "object": "snack"},
    {"action": "move", "object": "coke"},
    {"action": "fetch", "object": ""},
    {"action": "fetch", "object": "none"},
    {"action": "stop", "object": "coke"},
    {"action": "stop", "object": "none"},
    {"action": "unknown", "object": "none"},
    "not a command",
]


def spin_until(executor: SingleThreadedExecutor, predicate: object, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        executor.spin_once(timeout_sec=0.05)
        if predicate():  # type: ignore[operator]
            return True
    return False


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hold-seconds", type=float, default=0.0)
    args = parser.parse_args()

    received: list[RobotCommand] = []
    with RobotCommandPublisher("robot_command_publisher_test") as publisher:
        subscriber = Node("robot_command_subscriber_test", context=publisher.context)
        subscriber.create_subscription(
            RobotCommand,
            TOPIC_NAME,
            received.append,
            10,
        )
        executor = SingleThreadedExecutor(context=publisher.context)
        executor.add_node(publisher.node)
        executor.add_node(subscriber)
        try:
            discovered = spin_until(
                executor,
                lambda: publisher.publisher.get_subscription_count() >= 1,
                5.0,
            )
            if not discovered:
                raise RuntimeError("local subscriber discovery timed out")

            decisions: list[bool] = []
            for command in PUBLISHABLE_COMMANDS + BLOCKED_COMMANDS:
                transcript = f"test:{command!r}"
                decisions.append(publisher.publish_command(command, transcript))
            delivered = spin_until(executor, lambda: len(received) >= 5, 5.0)
            if not delivered:
                raise RuntimeError(f"expected 5 messages, received {len(received)}")

            actual = [
                {
                    "action": message.action,
                    "object": message.target_class,
                    "transcript": message.transcript,
                }
                for message in received
            ]
            expected = [
                {
                    "action": command["action"],
                    "object": command["object"],
                    "transcript": f"test:{command!r}",
                }
                for command in PUBLISHABLE_COMMANDS
            ]
            if actual != expected:
                raise RuntimeError(f"message mismatch: {actual!r}")
            if any(
                message.header.stamp.sec == 0
                and message.header.stamp.nanosec == 0
                for message in received
            ):
                raise RuntimeError("message header timestamp was not populated")
            expected_decisions = [True] * len(PUBLISHABLE_COMMANDS) + [False] * len(BLOCKED_COMMANDS)
            if decisions != expected_decisions:
                raise RuntimeError(f"safety filter mismatch: {decisions!r}")

            for message in received:
                print(
                    "PUBLISHED "
                    f"{message.action}/{message.target_class}: {message.transcript}"
                )
            print(f"UNKNOWN_AND_INVALID_BLOCKED: {len(BLOCKED_COMMANDS)}/{len(BLOCKED_COMMANDS)}")
            print(f"RECEIVED_COUNT: {len(received)}")
            print("RESULT: PASS", flush=True)

            if args.hold_seconds > 0:
                deadline = time.monotonic() + args.hold_seconds
                print(f"HOLDING_TOPIC: {args.hold_seconds:.1f}s", flush=True)
                while time.monotonic() < deadline:
                    executor.spin_once(timeout_sec=0.1)
        finally:
            executor.remove_node(subscriber)
            executor.remove_node(publisher.node)
            subscriber.destroy_node()
            executor.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
