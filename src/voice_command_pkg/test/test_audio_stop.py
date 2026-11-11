"""Test fast stop concurrency and fallback without a microphone, ROS or models."""

from dataclasses import replace
import hashlib
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from test_continuous_capture import make_pipeline, make_utterance
from voice_command_pkg import audio_stop_model, pipeline
from voice_command_pkg.audio_capture import FRAME_BYTES, VadConfig
from voice_command_pkg.audio_stop import AudioStopEvent, AudioStopWorker
from voice_command_pkg.audio_stop_model import AudioStopConfig, verify_model


PCM = bytes(FRAME_BYTES)


@pytest.mark.parametrize('values', [
    {'mode': 'enabled'}, {'model_dir': ''}, {'threshold': 0}, {'threshold': 1.1},
    {'threshold': float('nan')}, {'score': float('inf')}, {'queue_frames': 0},
    {'queue_frames': True}, {'queue_frames': 1.5}, {'max_lag_seconds': -1},
])
def test_invalid_config(values):
    with pytest.raises(ValueError):
        AudioStopConfig(**values)


def test_model_hash_checked_before_native_load(tmp_path, monkeypatch):
    monkeypatch.setattr(audio_stop_model, 'MODEL_HASHES', {
        'tokens.txt': hashlib.sha256(b'correct').hexdigest(),
    })
    with pytest.raises(FileNotFoundError):
        verify_model(tmp_path)
    (tmp_path / 'tokens.txt').write_bytes(b'incorrect')
    with pytest.raises(ValueError, match='SHA256'):
        verify_model(tmp_path)
    (tmp_path / 'tokens.txt').write_bytes(b'correct')
    verify_model(tmp_path)


def test_overflow_does_not_block_capture_and_resets_context():
    entered, release, delivered = [threading.Event() for _ in range(3)]
    calls, events, resets = [], [], []

    def accept(pcm):
        calls.append(pcm)
        if len(calls) == 1:
            entered.set()
            assert release.wait(2)
        return ['멈춰']

    def on_stop(event):
        events.append(event)
        delivered.set()

    worker = AudioStopWorker(
        AudioStopConfig(queue_frames=1, max_lag_seconds=5),
        SimpleNamespace(accept=accept, reset=lambda: resets.append(True)), on_stop, lambda _: None,
    )
    try:
        worker.submit(PCM, 0.03, time.perf_counter())
        assert entered.wait(1)
        # This completes while inference is blocked; queued context is dropped.
        worker.submit(PCM, 0.06, time.perf_counter())
        worker.submit(PCM, 0.09, time.perf_counter())
        assert worker.dropped_frames == 1
        release.set()
        assert delivered.wait(1)
        assert [event.audio_end for event in events] == [0.09]
        assert resets == [True]
    finally:
        release.set()
        worker.close()


def test_stale_frame_is_not_decoded_and_next_frame_resets():
    delivered = threading.Event()
    events, resets, decoded = [], [], []

    def accept(pcm):
        decoded.append(pcm)
        return ['스톱']

    def callback(event):
        events.append(event)
        delivered.set()

    worker = AudioStopWorker(AudioStopConfig(), SimpleNamespace(
        accept=accept, reset=lambda: resets.append(True)
    ), callback, lambda _: None)
    try:
        # Wait for disposal under the same condition, without arbitrary sleeps.
        with worker._condition:
            worker.submit(PCM, 0.03, time.perf_counter() - 10)
        deadline = time.perf_counter() + 1
        while worker.dropped_frames == 0 and time.perf_counter() < deadline:
            threading.Event().wait(0.001)
        assert worker.dropped_frames == 1
        worker.submit(PCM, 0.06, time.perf_counter())
        assert delivered.wait(1)
        assert len(decoded) == 1
        assert resets == [True]
        assert events[0].audio_end == 0.06
    finally:
        worker.close()


def test_inference_that_finishes_after_close_cannot_dispatch():
    entered, release = threading.Event(), threading.Event()
    events = []

    def accept(pcm):
        entered.set()
        assert release.wait(3)
        return ['정지']

    worker = AudioStopWorker(AudioStopConfig(max_lag_seconds=5), SimpleNamespace(
        accept=accept, reset=lambda: None
    ), events.append, lambda _: None)
    try:
        worker.submit(PCM, 0.03, time.perf_counter())
        assert entered.wait(1)
        worker.close()
        release.set()
        worker.worker.join(timeout=1)
        assert not worker.worker.is_alive()
        assert events == []
    finally:
        release.set()
        worker.close()


def test_backend_exception_disables_only_optional_worker():
    logs, events = [], []

    def fail(pcm):
        raise RuntimeError('decoder failed')

    worker = AudioStopWorker(AudioStopConfig(), SimpleNamespace(accept=fail), events.append,
                             logs.append)
    worker.submit(PCM, 0.03, time.perf_counter())
    worker.worker.join(timeout=1)
    assert not worker.worker.is_alive()
    assert events == []
    assert 'transcript fallback retained' in logs[-1]
    worker.submit(PCM, 0.06, time.perf_counter())
    assert not worker._frames


@pytest.mark.parametrize('mode, expected', [('active', ['stop']), ('shadow', ['fetch'])])
def test_stop_during_llm_blocks_late_fetch_only_in_active_mode(
    monkeypatch, tmp_path, mode, expected,
):
    instance, published, logs = make_pipeline(
        monkeypatch, tmp_path, audio_stop=AudioStopConfig(mode=mode)
    )
    entered, release = threading.Event(), threading.Event()
    results = []

    def classify(*args):
        entered.set()
        assert release.wait(2)
        return {'action': 'fetch', 'object': 'coke'}, {}

    instance.llm.classify = classify
    thread = threading.Thread(target=lambda: results.append(
        instance.process_utterance(make_utterance())
    ))
    try:
        thread.start()
        assert entered.wait(1)
        instance._on_audio_stop(AudioStopEvent('멈춰', 0.5, time.perf_counter()))
        assert published == (['stop'] if mode == 'active' else [])
        release.set()
        thread.join(timeout=1)
        assert not thread.is_alive()
        assert published == expected
        assert results[0]['dropped_reason'] == ('before_stop' if mode == 'active' else '')
    finally:
        release.set()
        thread.join(timeout=1)
        instance.request_stop()


def test_fast_stop_bypasses_robot_and_llm_readiness(monkeypatch, tmp_path):
    instance, published, _ = make_pipeline(
        monkeypatch, tmp_path, audio_stop=AudioStopConfig(mode='active')
    )
    instance.fetch_gate = lambda start: 'robot_error'
    instance.llm.drop_reason = lambda start: 'llm_preparing'
    instance._on_audio_stop(AudioStopEvent('정지', 0.5, time.perf_counter()))
    assert published == ['stop']
    instance.request_stop()
    instance._on_audio_stop(AudioStopEvent('스톱', 1.5, time.perf_counter()))
    assert published == ['stop']


@pytest.mark.parametrize('mode, keyword, age', [
    ('off', '멈춰', 0), ('active', '콜라', 0), ('active', '멈춰', 1),
])
def test_invalid_or_stale_event_never_publishes(monkeypatch, tmp_path, mode, keyword, age):
    instance, published, _ = make_pipeline(
        monkeypatch, tmp_path, audio_stop=AudioStopConfig(mode=mode)
    )
    instance._on_audio_stop(AudioStopEvent(keyword, 0.5, time.perf_counter() - age))
    assert published == []
    assert instance._fetch_not_before == float('-inf')
    instance.request_stop()


@pytest.mark.parametrize('success', [True, False])
def test_transcript_duplicate_only_suppressed_after_confirmed_send(
    monkeypatch, tmp_path, success,
):
    instance, _, _ = make_pipeline(
        monkeypatch, tmp_path, audio_stop=AudioStopConfig(mode='active')
    )
    calls = []

    def publish(command, text, capture):
        calls.append(command['action'])
        return success

    instance.publish = publish
    instance._on_audio_stop(AudioStopEvent('멈춰', 0.5, time.perf_counter()))
    monkeypatch.setattr(pipeline, 'transcribe_audio', lambda *args: ('멈춰', 0.0))
    utterance = make_utterance()
    capture = {**utterance.capture, 'voice_end_audio': 1.0, 'utterance_seconds': 0.8}
    result = instance.process_utterance(replace(utterance, capture=capture))
    assert calls == (['stop'] if success else ['stop', 'stop'])
    assert result['dropped_reason'] == ('audio_stop_already_published' if success else '')
    # A later distinct utterance must still stop; no global cooldown.
    capture = {**capture, 'voice_end_audio': 2.0}
    instance.process_utterance(replace(utterance, capture=capture))
    assert len(calls) == (2 if success else 3)
    instance.request_stop()


def test_missing_model_keeps_transcript_stop(monkeypatch, tmp_path):
    instance, published, logs = make_pipeline(
        monkeypatch, tmp_path,
        audio_stop=AudioStopConfig(mode='active', model_dir=str(tmp_path / 'missing')),
    )
    instance._start_audio_stop()
    assert instance.audio_stop_worker is None
    assert any('transcript fallback retained' in line for line in logs)
    monkeypatch.setattr(pipeline, 'transcribe_audio', lambda *args: ('멈춰', 0.0))
    instance.process_utterance(make_utterance())
    assert published == ['stop']
    instance.request_stop()


def test_audio_position_blocks_old_fetch_despite_late_burst_timestamp(monkeypatch, tmp_path):
    instance, published, _ = make_pipeline(
        monkeypatch, tmp_path, audio_stop=AudioStopConfig(mode='active')
    )
    instance._on_audio_stop(AudioStopEvent('멈춰', 2.0, time.perf_counter()))
    utterance = make_utterance()
    capture = {**utterance.capture, 'utterance_start_wall': time.perf_counter(),
               'utterance_start_audio': 1.0}
    result = instance.process_utterance(replace(utterance, capture=capture))
    assert result['dropped_reason'] == 'before_stop'
    assert published == ['stop']
    # Both a new audio position and a new wall time permit a new request.
    capture['utterance_start_audio'] = 3.0
    result = instance.process_utterance(replace(utterance, capture=capture))
    assert result['published']
    assert published == ['stop', 'fetch']
    instance.request_stop()


def test_shared_frame_tap_precedes_vad_and_skips_only_startup(monkeypatch):
    frames = iter([PCM] * 5 + [None])
    taps, sources, closed = [], [], []
    monkeypatch.setattr(pipeline, 'get_default_source', lambda: 'fake')

    def start(source):
        sources.append(source)
        return SimpleNamespace(stdout=object(), poll=lambda: 0)

    monkeypatch.setattr(pipeline, 'start_capture', start)
    monkeypatch.setattr(pipeline, 'stop_capture', lambda process: closed.append(True))
    monkeypatch.setattr(pipeline, 'read_frame_interruptible', lambda *args: next(frames))
    monkeypatch.setitem(sys.modules, 'webrtcvad', SimpleNamespace(
        Vad=lambda mode: SimpleNamespace(is_speech=lambda *args: False)
    ))
    stream = pipeline.capture_utterances(
        VadConfig(stream_warmup_ms=60), lambda _: None, threading.Event(),
        frame_sink=lambda pcm, offset, stamp: taps.append((pcm, offset, stamp)),
    )
    with pytest.raises(pipeline.PipelineError):
        list(stream)
    assert sources == ['fake']
    assert closed == [True]
    assert len(taps) == 3
    assert [tap[1] for tap in taps] == pytest.approx([0.03, 0.06, 0.09])
