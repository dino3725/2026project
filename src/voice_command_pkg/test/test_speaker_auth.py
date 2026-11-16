"""Verify fetch admission and unconditional stop without microphone, ROS or model downloads."""

from concurrent.futures import Future
import json
import threading
import time
from types import SimpleNamespace

import numpy as np
import pytest
from test_continuous_capture import make_pipeline, make_utterance

from voice_command_pkg import pipeline, speaker_runtime
from voice_command_pkg.parameters import build_pipeline_config
from voice_command_pkg.speaker_backend import MODEL_SHA256, SpeakerEncoder
from voice_command_pkg.speaker_profile import (
    centroid, choose_threshold, load_profile, similarity, SpeakerConfig, unit_vector,
)
from voice_command_pkg.speaker_runtime import SpeakerDecision, SpeakerRuntime


def profile():
    return {'schema': 1, 'backend': 'resemblyzer-0.1.4', 'model_sha256': MODEL_SHA256,
            'sample_rate': 16000, 'min_speech_seconds': .6, 'embedding': [1.] + [0.] * 255,
            'threshold': .75, 'calibrated': False,
            'enrollment_hashes': [f'{i:064x}' for i in range(20)]}


@pytest.mark.parametrize('value', [[], [0.] * 256, [float('nan')] * 256, [1.] * 192])
def test_bad_vector_rejected(value):
    with pytest.raises(ValueError):
        unit_vector(value)


def test_cosine_and_centroid():
    vector = [1.] + [0.] * 255
    assert similarity(vector, vector) == 1
    assert similarity(vector, [-1.] + [0.] * 255) == -1
    assert np.allclose(centroid([vector, vector]), vector)
    with np.errstate(over='ignore'), pytest.raises(ValueError):
        unit_vector([1e308] * 256)


def test_profile_pins_model_and_requires_evidence(tmp_path):
    path = tmp_path / 'profile.json'
    data = profile()
    path.write_text(json.dumps(data))
    assert not load_profile(path, MODEL_SHA256)['calibrated']
    with pytest.raises(ValueError):
        load_profile(path, 'different-model')
    data['calibrated'] = True
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError, match='evidence'):
        load_profile(path, MODEL_SHA256)


def test_calibration_fails_closed_when_scores_overlap():
    with pytest.raises(ValueError, match='overlap'):
        choose_threshold([.6] * 10, [.8] * 10)
    with pytest.raises(ValueError, match='at least'):
        choose_threshold([.9] * 9, [.5] * 10)
    result = choose_threshold([.9] * 10, [.5] * 10)
    assert .5 < result['threshold'] < .9
    assert result['development_far'] == result['development_frr'] == 0


@pytest.mark.parametrize('values', [
    {'speaker.mode': True}, {'speaker.mode': 'invalid'},
    {'speaker.wait_seconds': 0}, {'speaker.wait_seconds': float('nan')},
    {'speaker.min_speech_seconds': 16}, {'speaker.profile_path': ''},
])
def test_invalid_configuration(values):
    with pytest.raises(ValueError):
        build_pipeline_config(values)


def test_quality_rejects_silence_clipping_and_oversized_audio():
    # These branches must reject before embedding inference.
    encoder = object.__new__(SpeakerEncoder)
    encoder.min_speech_seconds = .6
    for pcm in (b'\0\0' * 16000, b'\xff\x7f' * 16000, b'\1\0' * 240001):
        with pytest.raises(ValueError):
            encoder.encode(pcm)


def install_decision(monkeypatch, decision, pending=False):
    tickets = []

    def factory(*args):
        def submit(pcm):
            ticket = Future()
            if not pending:
                ticket.set_result(decision)
            tickets.append(ticket)
            return ticket

        def finish(ticket, deadline):
            assert not pending, 'stop must never wait for verification'
            return ticket.result()
        return SimpleNamespace(submit=submit, finish=finish, close=lambda: None)
    monkeypatch.setattr(pipeline, 'SpeakerRuntime', factory)
    return tickets


@pytest.mark.parametrize('reason', ['speaker_rejected', 'speaker_error', 'speaker_preparing',
                                    'speaker_timeout', 'speaker_unavailable'])
def test_failed_auth_never_calls_llm_or_publishes_fetch(monkeypatch, tmp_path, reason):
    install_decision(monkeypatch, SpeakerDecision(False, reason))
    instance, published, _ = make_pipeline(
        monkeypatch, tmp_path, speaker=SpeakerConfig(mode='enforce'))
    monkeypatch.setattr(pipeline, 'classify_text', lambda *args: pytest.fail('LLM called'))
    result = instance.process_utterance(make_utterance())
    assert result['dropped_reason'] == reason
    assert not published


@pytest.mark.parametrize('text', ['스톱', '멈춰', '정지', '콜라 가져다 줘 아니 멈춰'])
def test_stop_does_not_wait_for_speaker_or_call_llm(monkeypatch, tmp_path, text):
    tickets = install_decision(monkeypatch, None, pending=True)
    instance, published, _ = make_pipeline(
        monkeypatch, tmp_path, speaker=SpeakerConfig(mode='enforce'))
    monkeypatch.setattr(pipeline, 'transcribe_audio', lambda *args: (text, 0.0))
    monkeypatch.setattr(pipeline, 'classify_text', lambda *args: pytest.fail('LLM called'))
    result = instance.process_utterance(make_utterance())
    assert published == ['stop']
    assert result['speaker_reason'] == 'stop_bypass'
    assert tickets[0].cancelled()


@pytest.mark.parametrize('mode,allowed', [('enforce', True), ('shadow', False)])
def test_verified_or_observational_fetch_reaches_existing_gates(
    monkeypatch, tmp_path, mode, allowed,
):
    install_decision(monkeypatch, SpeakerDecision(allowed, 'test'))
    instance, published, _ = make_pipeline(
        monkeypatch, tmp_path, speaker=SpeakerConfig(mode=mode))
    instance.process_utterance(make_utterance())
    assert published == ['fetch']
    instance.fetch_gate = lambda start: 'robot_busy'
    result = instance.process_utterance(make_utterance(2))
    assert result['dropped_reason'] == 'robot_busy'
    assert published == ['fetch']


def test_approval_is_not_reused_across_utterances(monkeypatch, tmp_path):
    install_decision(monkeypatch, SpeakerDecision(True, 'speaker_verified'))
    instance, published, _ = make_pipeline(
        monkeypatch, tmp_path, speaker=SpeakerConfig(mode='enforce'))
    instance.process_utterance(make_utterance())
    instance.speaker.finish = lambda *args: SpeakerDecision(False, 'speaker_rejected')
    instance.process_utterance(make_utterance(2))
    assert published == ['fetch']


def test_missing_profile_and_uncalibrated_profile_fail_closed(tmp_path):
    path = tmp_path / 'missing.json'
    for exists in (False, True):
        if exists:
            path.write_text(json.dumps(profile()))
        runtime = SpeakerRuntime(SpeakerConfig(mode='enforce', profile_path=str(path)),
                                 lambda text: None, threading.Event())
        runtime.worker.join(timeout=2)
        assert not runtime.worker.is_alive()
        assert runtime.submit(b'').result().reason == 'speaker_unavailable'
        runtime.close()


def test_busy_worker_is_bounded_and_close_does_not_wait(monkeypatch):
    entered, release, ready = threading.Event(), threading.Event(), threading.Event()
    monkeypatch.setattr(speaker_runtime, 'load_profile', lambda *args: profile())

    class Encoder:
        def __init__(self, *args):
            pass

        def encode(self, pcm):
            entered.set()
            assert release.wait(2)
            return profile()['embedding']

    monkeypatch.setattr(speaker_runtime, 'SpeakerEncoder', Encoder)
    runtime = SpeakerRuntime(SpeakerConfig(mode='shadow'),
                             lambda message: ready.set(), threading.Event())
    try:
        assert ready.wait(1)
        ticket = runtime.submit(b'audio')
        assert entered.wait(1)
        assert runtime.submit(b'next').result().reason == 'speaker_busy'
        assert runtime.finish(ticket, time.perf_counter() + 10).reason == 'speaker_timeout'
        runtime.close()
        assert not release.is_set()
        assert runtime.submit(b'after close').result().reason == 'speaker_shutdown'
    finally:
        release.set()
        runtime.worker.join(timeout=2)
    assert not runtime.worker.is_alive()


def development_profile():
    data = profile()
    data['development'] = {
        'genuine_count': 10, 'other_count': 10, 'threshold': .75,
        'genuine_scores': [.9] * 10, 'other_scores': [.5] * 10,
        'hashes': [f'{i:064x}' for i in range(20, 40)],
    }
    return data


def test_development_override_needs_valid_evidence(tmp_path):
    path = tmp_path / 'profile.json'
    path.write_text(json.dumps(profile()))
    with pytest.raises(ValueError, match='evidence'):
        load_profile(path, MODEL_SHA256, require_development=True)
    data = development_profile()
    path.write_text(json.dumps(data))
    assert not load_profile(path, MODEL_SHA256, True)['calibrated']
    data['development']['other_scores'][0] = .8
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError, match='criteria'):
        load_profile(path, MODEL_SHA256, True)


@pytest.mark.parametrize('allow', [False, True])
def test_explicit_development_override_preserves_calibration_status(monkeypatch, tmp_path, allow):
    path = tmp_path / 'profile.json'
    path.write_text(json.dumps(development_profile()))
    monkeypatch.setattr(speaker_runtime, 'SpeakerEncoder', lambda *args: SimpleNamespace(
        encode=lambda pcm: profile()['embedding']))
    ready, logs = threading.Event(), []

    def log(message):
        logs.append(message)
        ready.set()

    config = SpeakerConfig(mode='enforce', profile_path=str(path), allow_development_profile=allow)
    runtime = SpeakerRuntime(config, log, threading.Event())
    try:
        assert ready.wait(1)
        assert runtime.ready == allow
        if allow:
            assert 'calibrated=False development_override=True' in logs[0]
            assert runtime.submit(b'audio').result(timeout=1).allowed
        else:
            assert runtime.submit(b'audio').result().reason == 'speaker_unavailable'
        assert not json.loads(path.read_text())['calibrated']
    finally:
        runtime.close()
        runtime.worker.join(timeout=2)


def test_development_option_requires_bool_and_overlay_enforces():
    from pathlib import Path
    import yaml

    with pytest.raises(ValueError, match='boolean'):
        SpeakerConfig(allow_development_profile='true')
    path = Path(__file__).resolve().parents[1] / 'config' / 'speaker_development.yaml'
    values = yaml.safe_load(path.read_text())['voice_command_node']['ros__parameters']['speaker']
    config = build_pipeline_config({'speaker.' + key: value for key, value in values.items()})
    assert config.speaker.mode == 'enforce'
    assert config.speaker.allow_development_profile
    assert config.speaker.profile_path.endswith('/owner_development.json')
    assert not build_pipeline_config({}).speaker.allow_development_profile
