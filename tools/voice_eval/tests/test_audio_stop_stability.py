"""Validate original-audio coordinates and repeated-trial denominators."""

from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import evaluate_audio_stop_stability as stability  # noqa: E402


def test_trim_and_prefix_preserve_original_coordinates(monkeypatch):
    observed = []

    def replay(backend, pcm):
        observed.append(pcm)
        return {'hits': [{'audio_end': 0.03}, {'audio_end': 0.36}]}

    monkeypatch.setattr(stability, 'replay', replay)
    pcm = b'\x01\x00' * 16000
    result = stability.phase_replay(None, pcm, prefix_ms=60, trim_ms=300)
    assert observed == [bytes(1920) + pcm[9600:]]
    assert result['hits'][0]['in_synthetic_prefix']
    assert not result['hits'][1]['in_synthetic_prefix']
    assert result['hits'][1]['audio_end'] == pytest.approx(0.6)


@pytest.mark.parametrize('prefix,trim', [(-1, 0), (0, -1), (0.5, 0), (0, 1000)])
def test_invalid_transform_rejected(prefix, trim):
    with pytest.raises(ValueError):
        stability.phase_replay(None, bytes(32000), prefix, trim)


def test_prefix_hallucination_cannot_count_as_detected_target():
    row = {'category': 'target_stop', 'hits': [{'in_synthetic_prefix': True}]}
    assert stability.classify_trial(row) == {
        'expected_stops': 1, 'detected_stops': 0, 'extra_hits': 1,
    }


def test_continuous_offsets_score_once_and_keep_extra_hits():
    row = {'category': 'continuous_stop', 'events': [{'start': 10, 'end': 11}],
           'hits': [{'audio_end': 9.5}, {'audio_end': 10.3}, {'audio_end': 10.4}]}
    result = stability.classify_trial(row)
    assert result['detected_stops'] == 1
    assert result['extra_hits'] == 2


def test_summary_distinguishes_recordings_from_repeated_phases():
    rows = [
        {'id': 'a', 'category': 'target_stop', 'detected_stops': n, 'extra_hits': 0,
         'compute_seconds': 0.1, 'max_step_seconds': 0.01} for n in [1, 0, 1]
    ]
    summary = stability.summarize(rows)
    assert summary['target_files'] == 1
    assert summary['target_files_detected_at_every_phase'] == 0
    assert summary['target_trials'] == 3
    assert summary['target_trials_detected'] == 2
