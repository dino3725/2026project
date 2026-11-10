"""Bounded handoff between continuous recording and sequential processing."""

from collections import deque
from dataclasses import dataclass
import threading
from typing import Any


@dataclass(frozen=True)
class Utterance:
    """One complete PCM utterance with its sequence and capture timeline."""

    sequence: int
    audio: bytes
    capture: dict[str, Any]


class UtteranceBuffer:
    """Keep recent utterances without ever blocking the recording thread."""

    def __init__(self, capacity: int) -> None:
        if capacity < 1:
            raise ValueError('utterance buffer capacity must be positive')
        self.capacity = capacity
        self._items: deque[Utterance] = deque()
        self._condition = threading.Condition()
        self._closed = False

    def put(self, item: Utterance) -> Utterance | None:
        """Append an utterance, returning the oldest item if it was dropped."""
        with self._condition:
            if self._closed:
                return item
            dropped = self._items.popleft() if len(self._items) >= self.capacity else None
            self._items.append(item)
            self._condition.notify()
            return dropped

    def get(self, timeout: float = 0.1) -> Utterance | None:
        """Wait briefly for an utterance, or return None when empty/closed."""
        with self._condition:
            self._condition.wait_for(lambda: self._items or self._closed, timeout)
            return self._items.popleft() if self._items else None

    def close(self) -> None:
        """Discard pending work and wake the consumer during shutdown."""
        with self._condition:
            self._closed = True
            self._items.clear()
            self._condition.notify_all()
