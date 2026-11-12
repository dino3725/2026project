"""Software timing evidence; no physical speech-end or robot-stop claims."""

from functools import lru_cache
import json
import math
from pathlib import Path
import socket
import time


@lru_cache(maxsize=1)
def clock_domain():
    """Identify the host boot whose monotonic clock produced a timestamp."""
    return socket.gethostname() + ':' + Path('/proc/sys/kernel/random/boot_id').read_text().strip()


def publication_trace(message, capture, started, finished, published):
    """Describe actual publication or dry run without inventing a send timestamp."""
    capture = capture or {}
    row = {
        'schema': 1, 'event': 'published' if published else 'dry_run',
        'clock_domain': clock_domain(), 'unix_ns': time.time_ns(),
        'monotonic_ns': time.perf_counter_ns(), 'action': message.action,
        'target_class': message.target_class,
        'command_stamp_ns': (message.header.stamp.sec * 1000000000
                             + message.header.stamp.nanosec) if published else None,
        'publish_started_monotonic_ns': round(started * 1000000000),
        'publish_call_seconds': finished - started if published else None,
        'sequence': capture.get('sequence'),
        'detector': capture.get('detector', 'transcript'),
    }
    for key, label in (
        ('voice_end_wall', 'vad_end_to_publish_call_seconds'),
        ('vad_end_wall', 'vad_end_to_publish_call_seconds'),
        ('last_speech_frame_read_wall', 'last_speech_frame_read_to_publish_call_seconds'),
        ('frame_received_wall', 'frame_read_to_publish_call_seconds'),
    ):
        value = capture.get(key)
        if isinstance(value, (int, float)) and math.isfinite(value) and value <= started:
            row[label] = started - value
    return row


def log_publication(log, message, capture, started, finished, published):
    """Keep optional measurement failures from changing command dispatch."""
    try:
        row = publication_trace(message, capture, started, finished, published)
        log('[LATENCY] ' + json.dumps(row, ensure_ascii=False, allow_nan=False))
    except Exception:
        # Logging must not turn a successful stop publication into a failure.
        pass
