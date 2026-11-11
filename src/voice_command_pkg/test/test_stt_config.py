"""Check decoding parameter validation and actual file/PCM forwarding."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from voice_command_pkg.parameters import build_pipeline_config
from voice_command_pkg.pipeline import transcribe_audio
from voice_command_pkg.stt_config import SttConfig


@pytest.mark.parametrize('value', [0, -1, True, 1.5, '1', None])
def test_invalid_beam_is_rejected_without_coercion(value):
    with pytest.raises(ValueError, match='positive integer'):
        build_pipeline_config({'whisper.beam_size': value})


@pytest.mark.parametrize('name', ['initial_prompt', 'hotwords'])
@pytest.mark.parametrize('value', [None, 42, ['콜라']])
def test_hint_must_be_a_string(name, value):
    with pytest.raises(ValueError, match='string'):
        build_pipeline_config({f'whisper.{name}': value})


def test_parameters_reach_stt_config():
    config = build_pipeline_config({
        'whisper.beam_size': 1, 'whisper.initial_prompt': '바세린',
        'whisper.hotwords': '에어팟',
    })
    assert config.stt == SttConfig(1, '바세린', '에어팟', (0.0,))


@pytest.mark.parametrize('audio', [b'\x00\x00' * 480, Path('sample.wav')])
def test_runtime_forwards_options_for_both_input_types(audio):
    received = []

    def transcribe(source, **kwargs):
        received.append(kwargs)
        return iter([SimpleNamespace(text='스톱')]), None

    text, _ = transcribe_audio(
        SimpleNamespace(transcribe=transcribe), audio, 'ko', SttConfig(1, '바세린', '스톱'),
    )
    assert text == '스톱'
    assert received == [{
        'language': 'ko', 'beam_size': 1, 'initial_prompt': '바세린', 'hotwords': '스톱',
        'temperature': (0.0, 0.2, 0.4, 0.6, 0.8, 1.0),
    }]


def test_blank_hints_disable_context():
    assert SttConfig(initial_prompt='  ', hotwords='').options() == {
        'beam_size': 5, 'initial_prompt': None, 'hotwords': None,
        'temperature': (0.0, 0.2, 0.4, 0.6, 0.8, 1.0),
    }


@pytest.mark.parametrize('value', [
    [], (), '', None, [True], [float('nan')], [float('inf')], [-0.1], [1.1], ['0.0'],
])
def test_invalid_temperature_schedule(value):
    with pytest.raises(ValueError, match='temperatures'):
        build_pipeline_config({'whisper.temperatures': value})


def test_single_temperature_reaches_decoder_and_is_immutable():
    values = [0.0]
    config = build_pipeline_config({'whisper.temperatures': values})
    values.append(0.8)
    assert config.stt.temperatures == (0.0,)
    assert config.stt.options()['temperature'] == (0.0,)


def test_ros_and_standalone_pipeline_share_selected_defaults():
    from voice_command_pkg.pipeline import PipelineConfig

    expected = SttConfig(temperatures=(0.0,))
    assert build_pipeline_config({}).stt == PipelineConfig().stt == expected


def test_pipeline_uses_configured_decoder_and_keeps_stop_before_llm(monkeypatch):
    import time

    from voice_command_pkg import pipeline
    from voice_command_pkg.utterance_buffer import Utterance

    received, published = [], []

    def transcribe(source, **kwargs):
        received.append(kwargs)
        return iter([SimpleNamespace(text='스톱')]), None

    monkeypatch.setattr(pipeline, 'load_whisper_model', lambda config: (
        SimpleNamespace(transcribe=transcribe), 0.0,
    ))
    # No classify method: a stop must bypass LLM completely.
    monkeypatch.setattr(pipeline, 'LlmRuntime', lambda *args: SimpleNamespace(close=lambda: None))
    config = pipeline.PipelineConfig(stt=SttConfig(temperatures=(0.0,)))
    instance = pipeline.VoiceCommandPipeline(
        config, publish=lambda cmd, text, capture: published.append(cmd), log=lambda text: None,
    )
    now = time.perf_counter()
    instance.process_utterance(Utterance(1, b'\x00\x40' * 480, {
        'voice_start_wall': now - 0.03, 'voice_end_wall': now,
        'utterance_seconds': 0.03, 'vad_end_silence_seconds': 0.0,
    }))
    assert received[0]['temperature'] == (0.0,)
    assert received[0]['initial_prompt'] is None
    assert published == [{'action': 'stop', 'object': ''}]
