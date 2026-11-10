"""Exercise real pipe reads and thread handoffs without microphone, models or ROS."""

import os
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from voice_command_pkg import pipeline
from voice_command_pkg.audio_capture import (
    AudioCaptureError,
    FRAME_BYTES,
    read_frame_interruptible,
    VadConfig,
)
from voice_command_pkg.utterance_buffer import Utterance, UtteranceBuffer


def make_utterance(sequence=1, age=0.0):
    now = time.perf_counter() - age
    return Utterance(sequence, b'\x01\x00' * (FRAME_BYTES // 2), {
        'voice_start_wall': now - 0.03,
        'voice_end_wall': now,
        'utterance_seconds': 0.03,
        'vad_end_silence_seconds': 0.0,
    })


def make_pipeline(monkeypatch, tmp_path, **overrides):
    published, logs = [], []
    monkeypatch.setattr(pipeline, 'load_whisper_model', lambda config: (object(), 0.0))

    def runtime(config, log, stop_event):
        def classify(text, start, deadline):
            command, seconds = pipeline.classify_text(text, config.classifier)
            return command, {'wall_seconds': seconds}
        return SimpleNamespace(
            drop_reason=lambda start: '', classify=classify, close=lambda: None
        )

    monkeypatch.setattr(pipeline, 'LlmRuntime', runtime)
    monkeypatch.setattr(pipeline, 'transcribe_audio', lambda *args: ('콜라 줘', 0.0))
    monkeypatch.setattr(pipeline, 'classify_text', lambda *args: (
        {'action': 'fetch', 'object': 'coke'}, 0.0
    ))

    def publish(command, text):
        published.append(command['action'])
        return True

    instance = pipeline.VoiceCommandPipeline(
        pipeline.PipelineConfig(recordings_dir=tmp_path, **overrides), publish, logs.append
    )
    return instance, published, logs


def test_buffer_drops_oldest_and_close_discards_pending():
    buffer = UtteranceBuffer(2)
    first, second, third = [make_utterance(i) for i in range(1, 4)]
    assert buffer.put(first) is None
    assert buffer.put(second) is None
    assert buffer.put(third) == first
    assert buffer.get() == second
    buffer.close()
    assert buffer.get() is None
    assert buffer.put(first) == first
    assert buffer.get() is None


def test_stalled_pipe_can_be_interrupted():
    reader, writer = os.pipe()
    stopped = threading.Event()
    entered = threading.Event()
    outcomes = []
    with os.fdopen(reader, 'rb', buffering=0) as stream:
        def read():
            entered.set()
            try:
                read_frame_interruptible(stream, stopped)
            except InterruptedError:
                outcomes.append('stopped')

        worker = threading.Thread(target=read)
        worker.start()
        try:
            assert entered.wait(1)
            stopped.set()
            worker.join(timeout=1)
            assert not worker.is_alive()
            assert outcomes == ['stopped']
        finally:
            os.close(writer)
            worker.join(timeout=1)


def test_stalled_pipe_reports_timeout():
    reader, writer = os.pipe()
    try:
        with os.fdopen(reader, 'rb', buffering=0) as stream:
            with pytest.raises(AudioCaptureError):
                read_frame_interruptible(stream, threading.Event(), timeout_seconds=0.02)
    finally:
        os.close(writer)


def test_incomplete_pipe_frame_is_not_returned():
    reader, writer = os.pipe()
    os.write(writer, b'\x00' * 10)
    os.close(writer)
    with os.fdopen(reader, 'rb', buffering=0) as stream:
        assert read_frame_interruptible(stream, threading.Event()) is None


def install_capture_pipe(monkeypatch, frames):
    reader, writer = os.pipe()
    stream = os.fdopen(reader, 'rb', buffering=0)
    for frame in frames:
        os.write(writer, frame)
    os.close(writer)
    process = SimpleNamespace(stdout=stream, poll=lambda: 0)
    opens, closes = [], []
    monkeypatch.setattr(pipeline, 'get_default_source', lambda: 'fake-source')

    def start(source):
        opens.append(source)
        return process

    def stop(proc):
        closes.append(proc)
        proc.stdout.close()

    monkeypatch.setattr(pipeline, 'start_capture', start)
    monkeypatch.setattr(pipeline, 'stop_capture', stop)
    monkeypatch.setitem(sys.modules, 'webrtcvad', SimpleNamespace(
        Vad=lambda mode: SimpleNamespace(is_speech=lambda frame, rate: frame[0] != 0)
    ))
    return opens, closes


def test_one_stream_one_warmup_two_distinct_utterances(monkeypatch):
    silent = bytes(FRAME_BYTES)
    first, second = [bytes([value, 0]) * (FRAME_BYTES // 2) for value in (1, 2)]
    opens, closes = install_capture_pipe(monkeypatch, [first, first, silent, second, silent])
    config = VadConfig(
        stream_warmup_ms=30, start_window_ms=30, start_speech_ratio=1.0,
        end_silence_ms=30, pre_roll_ms=30, start_min_rms=0,
    )
    stream = pipeline.capture_utterances(config, lambda text: None, threading.Event())
    try:
        audio_a, capture_a = next(stream)
        audio_b, capture_b = next(stream)
        assert audio_a == first + silent
        assert audio_b == second + silent
        assert capture_a['voice_start_audio'] < capture_b['voice_start_audio']
        assert opens == ['fake-source']
        assert closes == []
    finally:
        stream.close()
    assert len(closes) == 1


def test_eof_closes_stream_and_reports_failure(monkeypatch):
    _, closes = install_capture_pipe(monkeypatch, [])
    stream = pipeline.capture_utterances(
        VadConfig(stream_warmup_ms=0), lambda text: None, threading.Event()
    )
    with pytest.raises(pipeline.PipelineError, match='종료'):
        next(stream)
    assert len(closes) == 1


def test_single_trial_helper_still_closes_stream(monkeypatch):
    audio = b'\x01\x00' * (FRAME_BYTES // 2)
    _, closes = install_capture_pipe(monkeypatch, [audio, bytes(FRAME_BYTES)])
    config = VadConfig(
        stream_warmup_ms=0, start_window_ms=30, start_speech_ratio=1.0,
        end_silence_ms=30, pre_roll_ms=30, start_min_rms=0,
    )
    result, _ = pipeline.capture_utterance(config, lambda text: None, threading.Event())
    assert result == audio + bytes(FRAME_BYTES)
    assert len(closes) == 1


def test_max_duration_starts_next_utterance_on_same_stream(monkeypatch):
    speech = b'\x01\x00' * (FRAME_BYTES // 2)
    opens, closes = install_capture_pipe(monkeypatch, [speech] * 3 + [bytes(FRAME_BYTES)])
    config = VadConfig(
        stream_warmup_ms=0, start_window_ms=30, start_speech_ratio=1.0,
        end_silence_ms=30, pre_roll_ms=30, start_min_rms=0,
        max_utterance_seconds=0.06,
    )
    stream = pipeline.capture_utterances(config, lambda text: None, threading.Event())
    try:
        first, info = next(stream)
        assert first == speech * 2
        assert info['max_duration_reached'] is True
        second, info = next(stream)
        assert second == speech + bytes(FRAME_BYTES)
        assert info['max_duration_reached'] is False
        assert len(opens) == 1
    finally:
        stream.close()
    assert len(closes) == 1


@pytest.mark.parametrize('stage', ['stt', 'llm'])
def test_shutdown_during_processing_prevents_publication(monkeypatch, tmp_path, stage):
    instance, published, _ = make_pipeline(monkeypatch, tmp_path)

    def interrupted(*args):
        instance.request_stop()
        if stage == 'stt':
            return '콜라 줘', 0.0
        return {'action': 'fetch', 'object': 'coke'}, 0.0

    monkeypatch.setattr(
        pipeline, 'transcribe_audio' if stage == 'stt' else 'classify_text', interrupted
    )
    with pytest.raises(pipeline.PipelineStopped):
        instance.process_utterance(make_utterance())
    assert published == []


def test_expired_fetch_skips_llm_but_expired_stop_is_published(monkeypatch, tmp_path):
    instance, published, _ = make_pipeline(monkeypatch, tmp_path)

    def unexpected(*args):
        pytest.fail('expired fetch or stop must not invoke LLM')

    monkeypatch.setattr(pipeline, 'classify_text', unexpected)
    result = instance.process_utterance(make_utterance(age=20))
    assert result['dropped_reason'] == 'expired'
    assert published == []
    monkeypatch.setattr(pipeline, 'transcribe_audio', lambda *args: ('멈춰', 0.0))
    result = instance.process_utterance(make_utterance(2, age=20))
    assert result['published'] is True
    assert published == ['stop']


def test_fetch_expiring_during_llm_is_not_published(monkeypatch, tmp_path):
    instance, published, _ = make_pipeline(monkeypatch, tmp_path)
    utterance = make_utterance()

    def delayed(*args):
        utterance.capture['voice_end_wall'] -= 20
        return {'action': 'fetch', 'object': 'coke'}, 20.0

    monkeypatch.setattr(pipeline, 'classify_text', delayed)
    result = instance.process_utterance(utterance)
    assert result['dropped_reason'] == 'expired'
    assert published == []


def test_stop_invalidates_earlier_fetch_but_not_new_fetch(monkeypatch, tmp_path):
    instance, published, _ = make_pipeline(monkeypatch, tmp_path)
    earlier = make_utterance(2)
    monkeypatch.setattr(pipeline, 'transcribe_audio', lambda *args: ('정지', 0.0))
    instance.process_utterance(make_utterance())
    monkeypatch.setattr(pipeline, 'transcribe_audio', lambda *args: ('콜라 줘', 0.0))
    result = instance.process_utterance(earlier)
    assert result['dropped_reason'] == 'before_stop'
    newer = make_utterance(3)
    newer.capture['voice_start_wall'] = time.perf_counter()
    assert instance.process_utterance(newer)['published'] is True
    assert published == ['stop', 'fetch']


def test_pre_roll_before_stop_also_invalidates_fetch(monkeypatch, tmp_path):
    instance, published, _ = make_pipeline(monkeypatch, tmp_path)
    instance._fetch_not_before = time.perf_counter()
    item = make_utterance()
    item.capture['voice_start_wall'] = time.perf_counter()
    item.capture['utterance_start_wall'] = instance._fetch_not_before - 0.1
    result = instance.process_utterance(item)
    assert result['dropped_reason'] == 'before_stop'
    assert published == []


@pytest.mark.parametrize('slow_stage', ['stt', 'llm'])
def test_recording_continues_during_processing(monkeypatch, tmp_path, slow_stage):
    instance, published, logs = make_pipeline(monkeypatch, tmp_path)
    processing = threading.Event()
    recorded_more = threading.Event()
    processed = []
    failures = []

    def capture(config, log, stop_event):
        first = make_utterance()
        yield first.audio, first.capture
        assert processing.wait(2)
        # Fill the bounded buffer while the consumer is deliberately blocked.
        for number in (2, 3, 4):
            item = make_utterance(number)
            yield item.audio, item.capture
        recorded_more.set()
        assert stop_event.wait(2)

    def slow(*args):
        processing.set()
        assert recorded_more.wait(2), 'capture must not wait for STT/LLM'
        if slow_stage == 'stt':
            return '콜라 줘', 0.0
        return {'action': 'fetch', 'object': 'coke'}, 0.0

    original = instance.process_utterance

    def process(item):
        result = original(item)
        processed.append(item.sequence)
        if item.sequence == 4:
            instance.request_stop()
        return result

    monkeypatch.setattr(pipeline, 'capture_utterances', capture)
    monkeypatch.setattr(
        pipeline, 'transcribe_audio' if slow_stage == 'stt' else 'classify_text', slow
    )
    monkeypatch.setattr(instance, 'process_utterance', process)

    def run():
        try:
            instance.run_forever()
        except Exception as exc:
            failures.append(exc)

    worker = threading.Thread(target=run)
    worker.start()
    try:
        worker.join(timeout=4)
        assert not worker.is_alive()
        assert failures == []
        assert processed == [1, 3, 4]
        assert published == ['fetch', 'fetch', 'fetch']
        assert any('sequence=2 reason=queue_full' in line for line in logs)
        assert not instance.capture_worker.is_alive()
    finally:
        instance.request_stop()
        worker.join(timeout=3)


def test_capture_failure_during_stt_blocks_result_and_propagates(monkeypatch, tmp_path):
    instance, published, _ = make_pipeline(monkeypatch, tmp_path)
    processing = threading.Event()

    def capture(config, log, stop_event):
        item = make_utterance()
        yield item.audio, item.capture
        assert processing.wait(2)
        raise pipeline.PipelineError('녹음', 'device disconnected')

    def transcribe(*args):
        processing.set()
        assert instance.stop_event.wait(2)
        return '콜라 줘', 0.0

    monkeypatch.setattr(pipeline, 'capture_utterances', capture)
    monkeypatch.setattr(pipeline, 'transcribe_audio', transcribe)
    with pytest.raises(pipeline.PipelineError, match='device disconnected'):
        instance.run_forever()
    assert published == []
    assert not instance.capture_worker.is_alive()


@pytest.mark.parametrize('overrides', [
    {'queue_capacity': 0}, {'max_fetch_age_seconds': 0},
    {'max_fetch_age_seconds': float('nan')}, {'max_fetch_age_seconds': float('inf')},
])
def test_invalid_buffer_config_is_rejected(overrides):
    with pytest.raises(ValueError):
        pipeline.PipelineConfig(**overrides)
