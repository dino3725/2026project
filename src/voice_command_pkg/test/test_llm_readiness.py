"""Exercise readiness races, recovery and deadlines without ROS or real models."""

import threading
import time

import pytest

from voice_command_pkg import llm_runtime, pipeline
from voice_command_pkg.command_classifier import ClassifierConfig
from voice_command_pkg.llm_runtime import LlmRuntime, LlmStopped, LlmUnavailable
from voice_command_pkg.parameters import build_pipeline_config
from voice_command_pkg.utterance_buffer import Utterance


FETCH = {'action': 'fetch', 'object': 'coke'}
METRICS = {'wall_seconds': 0.001}


def wait_until(predicate):
    deadline = time.perf_counter() + 2
    while not predicate():
        assert time.perf_counter() < deadline, 'worker did not reach expected state'
        time.sleep(0.002)


@pytest.fixture
def runtime_factory(monkeypatch):
    instances = []

    def create(call, **overrides):
        monkeypatch.setattr(llm_runtime, 'classify_command_with_metrics', call)
        config = pipeline.PipelineConfig(**overrides)
        logs = []
        runtime = LlmRuntime(config, logs.append, threading.Event())
        instances.append(runtime)
        return runtime, logs

    yield create
    for instance in instances:
        instance.close()
        instance.worker.join(timeout=2)
        assert not instance.worker.is_alive()


def wait_ready(runtime):
    wait_until(lambda: runtime.drop_reason(time.perf_counter()) == '')


def classify(runtime, start=None):
    return runtime.classify(
        '휴지 줘', time.perf_counter() if start is None else start,
        time.perf_counter() + 10,
    )


def test_warmup_uses_production_config_and_discards_result(runtime_factory):
    calls = []

    def call(text, config):
        calls.append((text, config))
        return FETCH, METRICS

    runtime, logs = runtime_factory(call)
    wait_ready(runtime)
    assert len(calls) == 1
    assert calls[0][1].timeout_seconds == 120
    assert calls[0][1].keep_alive == -1
    assert runtime.drop_reason(0) == 'llm_before_ready'
    assert classify(runtime) == (FETCH, METRICS)
    assert 0 < calls[1][1].timeout_seconds <= 10
    assert calls[0][1].model == calls[1][1].model
    assert any('[LLM READY]' in line for line in logs)


def test_speech_captured_during_warmup_never_replays(runtime_factory):
    entered, release = threading.Event(), threading.Event()
    calls = []

    def call(*args):
        calls.append(args)
        entered.set()
        assert release.wait(1)
        return FETCH, METRICS

    runtime, _ = runtime_factory(call)
    try:
        assert entered.wait(1)
        recorded_at = time.perf_counter()
        with pytest.raises(LlmUnavailable, match='llm_not_ready'):
            classify(runtime, recorded_at)
        release.set()
        wait_ready(runtime)
        with pytest.raises(LlmUnavailable, match='llm_before_ready'):
            classify(runtime, recorded_at)
        assert len(calls) == 1
        assert classify(runtime)[0] == FETCH
    finally:
        release.set()


@pytest.mark.parametrize('failure', [ConnectionError('offline'), ValueError('bad JSON')])
def test_failure_recovers_at_bounded_interval(runtime_factory, failure):
    times = []

    def call(*args):
        times.append(time.perf_counter())
        if len(times) == 1:
            raise failure
        return FETCH, METRICS

    runtime, logs = runtime_factory(call, llm_retry_seconds=0.04)
    wait_ready(runtime)
    assert len(times) == 2
    assert times[1] - times[0] >= 0.04
    assert any('[LLM ERROR] warmup' in line for line in logs)
    assert classify(runtime)[0] == FETCH


def test_request_error_discards_and_recovery_rejects_old_speech(runtime_factory):
    calls = []

    def call(*args):
        calls.append(args)
        if len(calls) == 2:
            raise ValueError('invalid action')
        return FETCH, METRICS

    runtime, _ = runtime_factory(call, llm_retry_seconds=0.04)
    wait_ready(runtime)
    old_start = time.perf_counter()
    with pytest.raises(LlmUnavailable, match='llm_error'):
        classify(runtime, old_start)
    assert runtime.drop_reason(time.perf_counter()) == 'llm_not_ready'
    wait_ready(runtime)
    assert runtime.drop_reason(old_start) == 'llm_before_ready'
    assert classify(runtime)[0] == FETCH


def test_deadline_ignores_late_success_and_never_starts_overlapping_request(runtime_factory):
    entered, release = threading.Event(), threading.Event()
    calls = []

    def call(*args):
        calls.append(args)
        if len(calls) == 2:
            entered.set()
            assert release.wait(1)
        return FETCH, METRICS

    runtime, _ = runtime_factory(
        call, classifier=ClassifierConfig(timeout_seconds=0.04), llm_retry_seconds=0.03
    )
    try:
        wait_ready(runtime)
        started = time.perf_counter()
        with pytest.raises(LlmUnavailable, match='llm_timeout'):
            classify(runtime)
        assert entered.is_set()
        assert time.perf_counter() - started < 0.5
        for _ in range(3):
            with pytest.raises(LlmUnavailable, match='llm_not_ready'):
                classify(runtime)
        assert len(calls) == 2  # No replacement worker or request after timeout.
        release.set()
        wait_ready(runtime)
        assert len(calls) == 3  # Recovery warms; the old request is never replayed.
    finally:
        release.set()


def test_remaining_utterance_age_shortens_request_deadline(runtime_factory):
    def call(text, config):
        if text == '휴지 줘':
            time.sleep(0.05)
        return FETCH, METRICS

    runtime, _ = runtime_factory(call)
    wait_ready(runtime)
    started = time.perf_counter()
    with pytest.raises(LlmUnavailable, match='llm_timeout'):
        runtime.classify('휴지 줘', started, started + 0.02)
    assert time.perf_counter() - started < 0.3


def test_shutdown_while_http_is_blocked_returns_without_late_ready(runtime_factory):
    entered, release = threading.Event(), threading.Event()

    def call(*args):
        entered.set()
        assert release.wait(1)
        return FETCH, METRICS

    runtime, logs = runtime_factory(call)
    try:
        assert entered.wait(1)
        runtime.close()
        with pytest.raises(LlmStopped):
            classify(runtime)
        release.set()
        runtime.worker.join(1)
        assert not runtime.worker.is_alive()
        assert not any('[LLM READY]' in line for line in logs)
    finally:
        release.set()


def test_shutdown_interrupts_active_classification(runtime_factory):
    entered, release = threading.Event(), threading.Event()

    def call(text, config):
        if text == '휴지 줘':
            entered.set()
            assert release.wait(1)
        return FETCH, METRICS

    runtime, _ = runtime_factory(call)
    wait_ready(runtime)
    outcomes = []

    def request():
        try:
            classify(runtime)
        except LlmStopped:
            outcomes.append('stopped')

    consumer = threading.Thread(target=request)
    consumer.start()
    try:
        assert entered.wait(1)
        runtime.stop_event.set()  # Same path used by VoiceCommandNode.stop().
        consumer.join(0.5)
        assert not consumer.is_alive()
        assert outcomes == ['stopped']
    finally:
        release.set()
        consumer.join(1)


def test_warmup_finishing_after_deadline_does_not_mark_ready(runtime_factory):
    def call(*args):
        time.sleep(0.03)
        return FETCH, METRICS

    runtime, logs = runtime_factory(call, llm_warmup_timeout_seconds=0.01)
    wait_until(lambda: any('deadline exceeded' in line for line in logs))
    assert runtime.drop_reason(time.perf_counter()) == 'llm_not_ready'
    assert not any('[LLM READY]' in line for line in logs)


@pytest.mark.parametrize('mode', ['warming', 'offline'])
def test_pipeline_stop_bypasses_unavailable_ollama(monkeypatch, mode):
    release = threading.Event()
    logs, published = [], []

    def call(*args):
        if mode == 'offline':
            raise ConnectionError('offline')
        assert release.wait(2)
        return FETCH, METRICS

    monkeypatch.setattr(llm_runtime, 'classify_command_with_metrics', call)
    monkeypatch.setattr(pipeline, 'load_whisper_model', lambda config: (object(), 0))
    monkeypatch.setattr(pipeline, 'transcribe_audio', lambda *args: ('콜라 줘', 0))
    instance = pipeline.VoiceCommandPipeline(
        pipeline.PipelineConfig(),
        lambda command, text, capture: published.append(command) or True, logs.append,
    )

    def utterance():
        now = time.perf_counter()
        return Utterance(1, b'\x01\x00' * 480, {
            'voice_start_wall': now, 'voice_end_wall': now,
            'utterance_seconds': 0.03, 'vad_end_silence_seconds': 0,
        })

    try:
        result = instance.process_utterance(utterance())
        assert result['dropped_reason'] == 'llm_not_ready'
        assert published == []
        monkeypatch.setattr(pipeline, 'transcribe_audio', lambda *args: ('멈춰', 0))
        result = instance.process_utterance(utterance())
        assert result['decided_by'] == 'rule:멈춰'
        assert result['llm_seconds'] == 0
        assert published == [{'action': 'stop', 'object': ''}]
    finally:
        instance.request_stop()
        release.set()
        instance.llm.worker.join(1)
        assert not instance.llm.worker.is_alive()


@pytest.mark.parametrize('name', [
    'llm.timeout_seconds', 'llm.warmup_timeout_seconds', 'llm.retry_seconds',
])
@pytest.mark.parametrize('value', [0, -1, float('nan'), float('inf')])
def test_invalid_llm_time_limits_are_rejected(name, value):
    with pytest.raises(ValueError, match='finite and positive'):
        build_pipeline_config({name: value})


def test_pipeline_blocks_pre_ready_and_late_fetch_but_handles_stop(monkeypatch):
    release = threading.Event()
    calls, published = [], []

    def call(*args):
        calls.append(args)
        if len(calls) > 1:
            assert release.wait(2)
        return FETCH, METRICS

    monkeypatch.setattr(llm_runtime, 'classify_command_with_metrics', call)
    monkeypatch.setattr(pipeline, 'load_whisper_model', lambda config: (object(), 0))
    monkeypatch.setattr(pipeline, 'transcribe_audio', lambda *args: ('콜라 줘', 0))
    instance = pipeline.VoiceCommandPipeline(
        pipeline.PipelineConfig(classifier=ClassifierConfig(timeout_seconds=0.03)),
        lambda command, text, capture: published.append(command) or True, lambda line: None,
    )

    def utterance(start=None):
        now = time.perf_counter()
        return Utterance(1, b'\x01\x00' * 480, {
            'voice_start_wall': now,
            'utterance_start_wall': now if start is None else start,
            'voice_end_wall': now, 'utterance_seconds': 0.03, 'vad_end_silence_seconds': 0,
        })

    try:
        wait_ready(instance.llm)
        assert published == []  # A warmup fetch must never invoke the callback.
        result = instance.process_utterance(utterance(start=0))
        assert result['dropped_reason'] == 'llm_before_ready'
        assert len(calls) == 1
        result = instance.process_utterance(utterance())
        assert result['dropped_reason'] == 'llm_timeout'
        assert published == []
        monkeypatch.setattr(pipeline, 'transcribe_audio', lambda *args: ('멈춰', 0))
        result = instance.process_utterance(utterance())
        assert result['published']
        assert published == [{'action': 'stop', 'object': ''}]
        release.set()
        wait_until(lambda: instance.llm._request is None)
        assert published == [{'action': 'stop', 'object': ''}]
    finally:
        instance.request_stop()
        release.set()
        instance.llm.worker.join(1)
        assert not instance.llm.worker.is_alive()


def test_llm_settings_and_templates_reach_runtime_config():
    config = build_pipeline_config({
        'llm.warmup_timeout_seconds': 90.0, 'llm.retry_seconds': 7.0,
        'messages.llm_preparing': '준비', 'messages.llm_ready': '완료',
        'messages.llm_failure': '재시도',
    })
    assert config.llm_warmup_timeout_seconds == 90
    assert config.llm_retry_seconds == 7
    assert config.classifier.keep_alive == -1
    assert config.llm_preparing_message == '준비'
    assert config.llm_ready_message == '완료'
    assert config.llm_failure_message == '재시도'
