"""Keep discarded detections and unconfirmed sounds out of success counts."""

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import evaluate_audio_stop_phases as phases  # noqa: E402


def test_late_hit_is_not_counted(monkeypatch):
    times = iter([1.0, 1.6])
    monkeypatch.setattr(phases.time, 'perf_counter', lambda: next(times))
    monkeypatch.setattr(phases, 'recheck_audio', lambda *a, **k: ('정지', 0.1))
    result = phases.trial(None, b'', True, .5)
    assert result['hit'] is None
    assert result['expired']


def test_summary_counts_recordings_and_keeps_unconfirmed_separate():
    hit = {'hit': ('정지', .5), 'expired': False, 'compute_seconds': .1}
    expired = {'hit': None, 'expired': True, 'compute_seconds': .51}
    rows = [{'category': category, 'original': hit, 'phases': hit, 'bounded_phases': expired}
            for category in ('target_stop', 'negative', 'unconfirmed_input')]
    summary = phases.summarize(rows)
    assert set(summary) == {'target_stop', 'negative', 'unconfirmed_input'}
    assert summary['target_stop']['files'] == 1
    assert summary['target_stop']['phases']['hits'] == 1
    assert summary['target_stop']['bounded_phases']['hits'] == 0
    assert summary['negative']['bounded_phases']['expired'] == 1


def test_offsets_are_trials_not_independent_recordings():
    hit = {'hit': ('스톱', .5), 'expired': False, 'compute_seconds': .1}
    rows = [{'category': 'target_stop', 'pcm_sha256': 'same_audio', 'original': hit,
             'phases': hit, 'bounded_phases': hit} for _ in range(6)]
    summary = phases.summarize(rows)['target_stop']
    assert summary['files'] == 1
    assert summary['trials'] == 6
