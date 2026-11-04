"""Pure validation rules for commands sent to the robot (no ROS imports)."""

from __future__ import annotations

from typing import Any


TOPIC_NAME = '/voice/robot_command'
VALID_OBJECTS = frozenset({'coke', 'vaseline', 'tissue', 'airpod'})


def command_payload(command: Any) -> tuple[str, str] | None:
    """
    Return canonical (action, target_class) for an allowed command.

    Only ``fetch`` with one of the four supported objects and ``stop`` with an
    empty object pass. Everything else, including ``unknown``, returns None and
    must not be published.
    """
    if not isinstance(command, dict) or set(command) != {'action', 'object'}:
        return None
    action = command.get('action')
    obj = command.get('object')
    if action == 'stop' and obj == '':
        return 'stop', ''
    if action == 'fetch' and obj in VALID_OBJECTS:
        return 'fetch', obj
    return None
