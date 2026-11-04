"""
Deterministic stop detection on Whisper transcripts (no ROS, no LLM).

Stop is decided here, before the LLM, and never by the LLM. A missed stop is
dangerous while a false stop only halts the robot, so any stop keyword anywhere
in the utterance counts as stop, even inside a negation or another context
(for example "음악 정지" or "멈추지 마" also stop the robot).
"""

from __future__ import annotations

import re
import unicodedata


# Frequent Whisper misrecognitions, applied after whitespace/punctuation removal.
MISRECOGNITIONS: tuple[tuple[str, str], ...] = (
    ('멈쳐', '멈춰'),
    ('멈처', '멈춰'),
    ('스탑', '스톱'),
    ('스땁', '스톱'),
    ('스텁', '스톱'),
)

# Substrings of the normalized transcript that mean stop.
STOP_KEYWORDS: tuple[str, ...] = (
    '멈춰',      # 멈춰, 멈춰요, 멈춰라, 멈춰줘
    '멈추',      # 멈추어, 멈추라고, 멈추세요
    '멈춤',      # 일시 멈춤
    '정지',      # 정지, 정지해, 비상 정지
    '스톱',
    'stop',
    '중단',      # 이동을 중단해
    '움직이지마',
    '그만움직',  # 그만 움직여
    '그대로서',  # 거기 그대로 서
)

_NOT_KEPT = re.compile(r'[^0-9a-z가-힣]')


def normalize_transcript(text: str) -> str:
    """
    Return a canonical form for keyword matching.

    Unicode NFC, lower case, keep only Hangul syllables, Latin letters and
    digits (drops spaces and punctuation), then fix known misrecognitions.
    """
    normalized = unicodedata.normalize('NFC', text).lower()
    normalized = _NOT_KEPT.sub('', normalized)
    for wrong, right in MISRECOGNITIONS:
        normalized = normalized.replace(wrong, right)
    return normalized


def find_stop_keyword(text: str) -> str | None:
    """Return the first stop keyword found in the transcript, or None."""
    normalized = normalize_transcript(text)
    for keyword in STOP_KEYWORDS:
        if keyword in normalized:
            return keyword
    return None


def is_stop_command(text: str) -> bool:
    """Return True when the transcript must be treated as a stop command."""
    return find_stop_keyword(text) is not None


def stop_command() -> dict[str, str]:
    """Return a fresh stop command in the classifier result format."""
    return {'action': 'stop', 'object': ''}
