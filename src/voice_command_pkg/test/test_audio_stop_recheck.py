"""Exercise segment recovery, publication races and bounded recheck shutdown."""

from dataclasses import replace
import threading
import time
from types import SimpleNamespace

import pytest

from test_continuous_capture import make_pipeline, make_utterance
from voice_command_pkg import pipeline
from voice_command_pkg.audio_stop import AudioStopEvent
from voice_command_pkg.audio_stop_model import AudioStopConfig
from voice_command_pkg.audio_stop_recheck import AudioStopRecheckWorker, recheck_audio


def utterance(sequence=1, age=0):
    item = make_utterance(sequence, age)
    return replace(item, capture={**item.capture, 'voice_end_audio': float(sequence)})


def test_recheck_fresh_stream_no_synthetic_audio_and_early_exit():
    calls = []
    backend = SimpleNamespace(reset=lambda: calls.append('reset'), accept=lambda pcm: (
        calls.append(pcm) or ['정지']
    ))
    pcm = utterance().audio
    assert recheck_audio(backend, pcm * 3) == ('정지', 0.03)
    assert calls == ['reset', pcm]


def test_phase_retry_preserves_original_audio_coordinates():
    calls, attempts = [], []

    def reset():
        attempts.append([])

    def accept(pcm):
        attempts[-1].append(pcm)
        calls.append(pcm)
        return ['스톱'] if len(attempts) == 3 and pcm == b'\1' * 960 else []

    pcm = b'\1' * 960
    hit = recheck_audio(SimpleNamespace(reset=reset, accept=accept), pcm, phase_shifts=True)
    assert hit == ('스톱', 0.03)
    assert [b''.join(a) for a in attempts] == [pcm, bytes(3840) + pcm, bytes(9600) + pcm]


def test_phase_retry_ignores_prefix_hits_and_keeps_partial_frame_end():
    attempt = [0]

    def reset():
        attempt[0] += 1

    backend = SimpleNamespace(reset=reset, accept=lambda pcm: (
        ['정지'] if attempt[0] > 1 else []
    ))
    assert recheck_audio(backend, b'\1' * 100, phase_shifts=True) == ('정지', 100 / 32000)


def test_phase_retry_cancellation_does_not_reset_deadline():
    attempts = []
    cancelled = [False]

    def accept(pcm):
        if len(attempts) == 2:
            cancelled[0] = True
        return ['정지'] if cancelled[0] else []

    backend = SimpleNamespace(reset=lambda: attempts.append(True), accept=accept)
    assert recheck_audio(backend, b'\1' * 960, lambda: cancelled[0],
                         phase_shifts=True) is None
    assert len(attempts) == 2


def test_phase_retry_does_not_delay_original_hit():
    calls = []
    backend = SimpleNamespace(reset=lambda: calls.append('reset'), accept=lambda pcm: ['정지'])
    assert recheck_audio(backend, b'\1' * 960, phase_shifts=True) == ('정지', 0.03)
    assert calls == ['reset']


def test_phase_retry_exhausts_bounded_offsets_without_rewriting_pcm():
    attempts = []
    backend = SimpleNamespace(
        reset=lambda: attempts.append([]), accept=lambda pcm: attempts[-1].append(pcm) or [],
    )
    pcm = b'\1' * 100
    assert recheck_audio(backend, pcm, phase_shifts=True) is None
    assert [b''.join(a) for a in attempts] == [
        bytes(ms * 32) + pcm for ms in (0, 120, 300, 240, 180, 60)
    ]


def test_gain_retry_keeps_original_first_and_its_coordinates():
    from voice_command_pkg.audio_samples import amplify_pcm
    pcm = b'\1\0' * 480
    amplified = amplify_pcm(pcm, 2)
    calls = []
    backend = SimpleNamespace(reset=lambda: None, accept=lambda chunk: (
        calls.append(chunk) or (['스톱'] if chunk == amplified else [])
    ))
    assert recheck_audio(backend, pcm, gain=2) == ('스톱', .03)
    assert calls == [pcm, amplified]
    assert pcm == b'\1\0' * 480


def test_original_hit_never_uses_gain_retry():
    calls = []
    backend = SimpleNamespace(reset=lambda: None, accept=lambda pcm: (
        calls.append(pcm) or ['정지']
    ))
    pcm = b'\1\0' * 480
    assert recheck_audio(backend, pcm, phase_shifts=True, gain=2) == ('정지', .03)
    assert calls == [pcm]


def test_gain_retry_checks_shared_cancellation_before_dispatch():
    calls = []
    backend = SimpleNamespace(reset=lambda: None, accept=lambda pcm: (
        calls.append(pcm) or (['정지'] if len(calls) == 2 else [])
    ))
    assert recheck_audio(backend, b'\1\0' * 480, lambda: len(calls) >= 2, gain=2) is None


def test_recheck_worker_original_coordinates_and_expired_input():
    events, logs = [], []
    done = threading.Event()
    backend = SimpleNamespace(reset=lambda: None, accept=lambda pcm: ['정지'])

    def log(line):
        logs.append(line)
        done.set()

    worker = AudioStopRecheckWorker(AudioStopConfig(), backend, events.append, log)
    try:
        worker.submit(utterance(age=10))
        assert done.wait(1)
        assert events == []
        assert 'expired' in logs[-1]
        done.clear()
        worker.submit(utterance())
        assert done.wait(1)
        assert len(events) == 1
        assert events[0].audio_end == pytest.approx(1.0)
        assert events[0].window == pytest.approx((0.97, 1.0))
        assert events[0].source == 'utterance_recheck'
    finally:
        worker.close()


def test_phase_worker_uses_shifted_retry_but_original_window():
    events, attempts = [], []
    done = threading.Event()
    backend = SimpleNamespace(
        reset=lambda: attempts.append(True),
        accept=lambda pcm: ['정지'] if len(attempts) == 2 and any(pcm) else [],
    )
    worker = AudioStopRecheckWorker(
        AudioStopConfig(recheck_phase_shifts=True), backend, events.append,
        lambda line: done.set(),
    )
    try:
        worker.submit(replace(utterance(), audio=b'\1' * 960))
        assert done.wait(1)
        assert len(events) == 1
        assert events[0].audio_end == pytest.approx(1.0)
        assert events[0].window == pytest.approx((.97, 1.0))
    finally:
        worker.close()


def test_recheck_has_one_pending_slot_and_no_late_callback():
    entered, release = threading.Event(), threading.Event()
    events, logs = [], []

    def accept(pcm):
        entered.set()
        assert release.wait(3)
        return ['스톱']

    worker = AudioStopRecheckWorker(AudioStopConfig(max_lag_seconds=5), SimpleNamespace(
        reset=lambda: None, accept=accept,
    ), events.append, logs.append)
    try:
        worker.submit(utterance(1))
        assert entered.wait(1)
        worker.submit(utterance(2))
        worker.submit(utterance(3))
        assert 'sequence=2 reason=queue_full' in logs[-1]
        worker.close()
        release.set()
        worker.worker.join(timeout=1)
        assert events == []
        assert not worker.worker.is_alive()
    finally:
        release.set()
        worker.close()


@pytest.mark.parametrize('order', [('stream', 'recheck'), ('recheck', 'stream')])
@pytest.mark.parametrize('success', [True, False])
def test_two_audio_paths_deduplicate_only_success_same_window(
    monkeypatch, tmp_path, order, success,
):
    instance, _, logs = make_pipeline(
        monkeypatch, tmp_path, audio_stop=AudioStopConfig(mode='active')
    )
    calls = []
    instance.publish = lambda *args: calls.append(args) or success
    events = {
        'stream': AudioStopEvent('정지', 0.99, time.perf_counter()),
        'recheck': AudioStopEvent('정지', 0.98, time.perf_counter(),
                                  'utterance_recheck', (0.97, 1.0)),
    }
    try:
        for source in order:
            instance._on_audio_stop(events[source])
        assert len(calls) == (1 if success else 2)
        # A later distinct stop is never suppressed by a global cooldown.
        instance._on_audio_stop(AudioStopEvent('정지', 1.99, time.perf_counter(),
                                               'utterance_recheck', (1.97, 2.0)))
        assert len(calls) == (2 if success else 3)
        # A fetch between detections also prevents duplicate suppression.
        instance._last_published_action = 'fetch'
        instance._on_audio_stop(events['recheck'])
        assert len(calls) == (3 if success else 4)
    finally:
        instance.request_stop()


def test_transcript_first_suppresses_late_recheck(monkeypatch, tmp_path):
    instance, published, _ = make_pipeline(
        monkeypatch, tmp_path, audio_stop=AudioStopConfig(mode='active')
    )
    monkeypatch.setattr(pipeline, 'transcribe_audio', lambda *args: ('정지', 0))
    try:
        instance.process_utterance(utterance())
        instance._on_audio_stop(AudioStopEvent('정지', 0.99, time.perf_counter(),
                                               'utterance_recheck', (0.97, 1.0)))
        assert published == ['stop']
    finally:
        instance.request_stop()


@pytest.mark.parametrize('mode', ['active', 'shadow'])
def test_recheck_runs_during_blocked_llm_and_blocks_old_fetch(monkeypatch, tmp_path, mode):
    instance, published, logs = make_pipeline(
        monkeypatch, tmp_path, audio_stop=AudioStopConfig(mode=mode)
    )
    entered, release, done = [threading.Event() for _ in range(3)]
    results = []

    def classify(*args):
        entered.set()
        assert release.wait(3)
        return {'action': 'fetch', 'object': 'coke'}, {}

    instance.llm.classify = classify
    thread = threading.Thread(target=lambda: results.append(
        instance.process_utterance(make_utterance())
    ))
    worker = AudioStopRecheckWorker(instance.config.audio_stop, SimpleNamespace(
        reset=lambda: None, accept=lambda pcm: ['정지'],
    ), instance._on_audio_stop, lambda line: done.set())
    try:
        thread.start()
        assert entered.wait(1)
        worker.submit(utterance())
        assert done.wait(1)
        assert published == (['stop'] if mode == 'active' else [])
        release.set()
        thread.join(timeout=1)
        assert published == (['stop'] if mode == 'active' else ['fetch'])
        assert results[0]['dropped_reason'] == ('before_stop' if mode == 'active' else '')
    finally:
        release.set()
        thread.join(timeout=1)
        worker.close()
        instance.request_stop()


def test_recheck_backend_failure_does_not_stop_pipeline(monkeypatch, tmp_path):
    instance, published, logs = make_pipeline(
        monkeypatch, tmp_path, audio_stop=AudioStopConfig(mode='active')
    )

    def fail(pcm):
        raise RuntimeError('recheck failed')

    worker = AudioStopRecheckWorker(instance.config.audio_stop, SimpleNamespace(
        reset=lambda: None, accept=fail,
    ), instance._on_audio_stop, logs.append)
    try:
        worker.submit(utterance())
        worker.worker.join(timeout=1)
        assert not worker.worker.is_alive()
        assert 'other stop paths retained' in logs[-1]
        instance._on_audio_stop(AudioStopEvent('멈춰', 2.0, time.perf_counter()))
        assert published == ['stop']
    finally:
        worker.close()
        instance.request_stop()


def test_inference_expiring_during_decode_is_discarded():
    entered, release, done = [threading.Event() for _ in range(3)]
    events, logs = [], []

    def accept(pcm):
        entered.set()
        assert release.wait(2)
        return ['정지']

    def log(line):
        logs.append(line)
        done.set()

    worker = AudioStopRecheckWorker(AudioStopConfig(max_lag_seconds=0.05), SimpleNamespace(
        reset=lambda: None, accept=accept,
    ), events.append, log)
    try:
        worker.submit(utterance())
        assert entered.wait(1)
        threading.Event().wait(0.06)
        release.set()
        assert done.wait(1)
        assert events == []
        assert 'expired' in logs[-1]
    finally:
        release.set()
        worker.close()


@pytest.mark.parametrize('mode,enabled,count', [
    ('off', True, 0), ('shadow', False, 1), ('shadow', True, 2),
])
def test_startup_honors_off_and_recheck_switch(monkeypatch, tmp_path, mode, enabled, count):
    instance, _, _ = make_pipeline(monkeypatch, tmp_path, audio_stop=AudioStopConfig(
        mode=mode, utterance_recheck=enabled,
    ))
    loads = []

    def backend(config):
        loads.append(True)
        return SimpleNamespace(reset=lambda: None, accept=lambda pcm: [])

    monkeypatch.setattr(pipeline, 'KoreanStopSpotter', backend)
    try:
        instance._start_audio_stop()
        assert len(loads) == count
        assert (instance.audio_stop_recheck is not None) == (count == 2)
    finally:
        instance.request_stop()
