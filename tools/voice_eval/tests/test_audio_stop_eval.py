"""Keep development event matching and synthetic tail accounting explicit."""

from pathlib import Path
import sys
from types import SimpleNamespace
import wave

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from evaluate_audio_stop import read_pcm, replay, score_events  # noqa: E402


def test_one_detection_cannot_satisfy_two_expected_stops():
    result = score_events([{'audio_end': 1.0}], [
        {'start': 0, 'end': 1.2}, {'start': 0.9, 'end': 2},
    ])
    assert result['matched_events'] == [0, None]
    assert result['extra_hit_indices'] == []


def test_extra_and_out_of_window_detections_remain_visible():
    result = score_events([{'audio_end': t} for t in [0.5, 1.0, 1.1, 3.0]], [
        {'start': 0.9, 'end': 1.5},
    ])
    assert result['matched_events'] == [1]
    assert result['extra_hit_indices'] == [0, 2, 3]


def test_synthetic_tail_detection_is_not_reported_inside_real_audio():
    calls = []

    def accept(pcm):
        calls.append(pcm)
        return ['멈춰'] if len(calls) == 2 else []

    result = replay(SimpleNamespace(reset=lambda: None, accept=accept), bytes(960), 0.03)
    assert result['duration_seconds'] == 0.03
    assert result['hits'] == [
        {'keyword': '멈춰', 'audio_end': 0.06, 'in_synthetic_tail': True},
    ]


def test_wrong_sample_rate_is_rejected(tmp_path):
    path = tmp_path / 'audio.wav'
    with wave.open(str(path), 'wb') as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(8000)
        wav.writeframes(bytes(160))
    with pytest.raises(ValueError, match='16 kHz'):
        read_pcm(path)
