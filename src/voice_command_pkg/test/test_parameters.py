from pathlib import Path

import pytest
import yaml

from voice_command_pkg.audio_capture import VadConfig
from voice_command_pkg.command_classifier import ClassifierConfig
from voice_command_pkg.parameters import build_pipeline_config, DEFAULT_PARAMETERS


CONFIG_PATH = Path(__file__).resolve().parents[1] / 'config' / 'voice_command.yaml'


def flatten(tree, prefix=''):
    flat = {}
    for key, value in tree.items():
        name = f'{prefix}{key}'
        if isinstance(value, dict):
            flat.update(flatten(value, name + '.'))
        else:
            flat[name] = value
    return flat


def test_yaml_matches_code_defaults():
    with CONFIG_PATH.open(encoding='utf-8') as handle:
        params = yaml.safe_load(handle)['voice_command_node']['ros__parameters']
    assert flatten(params) == DEFAULT_PARAMETERS


def test_defaults_reproduce_baseline_pipeline():
    config = build_pipeline_config({})
    assert config.vad == VadConfig()
    assert config.classifier == ClassifierConfig()
    assert config.whisper_model == 'small'
    assert config.language == 'ko'
    assert config.recordings_dir == Path('~/2026project/recordings').expanduser()


def test_override_reaches_nested_config():
    config = build_pipeline_config({'vad.end_silence_ms': 600, 'llm.timeout_seconds': 10})
    assert config.vad.end_silence_ms == 600
    assert config.classifier.timeout_seconds == 10.0


def test_capture_parameters_reach_pipeline():
    config = build_pipeline_config({
        'capture.queue_capacity': 3, 'capture.max_fetch_age_seconds': 8.0,
    })
    assert config.queue_capacity == 3
    assert config.max_fetch_age_seconds == 8.0


def test_unknown_parameter_is_rejected():
    with pytest.raises(ValueError):
        build_pipeline_config({'vad.end_silence': 600})


def test_audio_precision_default_and_override_preserve_disabled_mode():
    assert build_pipeline_config({}).audio_stop.precision == 'fp32'
    config = build_pipeline_config({'audio_stop.precision': 'int8'})
    assert config.audio_stop.precision == 'int8'
    assert config.audio_stop.mode == 'off'


def test_phase_recheck_requires_explicit_selection():
    assert not build_pipeline_config({}).audio_stop.recheck_phase_shifts
    config = build_pipeline_config({'audio_stop.recheck_phase_shifts': True})
    assert config.audio_stop.recheck_phase_shifts
    assert config.audio_stop.mode == 'off'


def test_recheck_gain_default_and_override():
    assert build_pipeline_config({}).audio_stop.recheck_gain == 1.0
    assert build_pipeline_config({'audio_stop.recheck_gain': 2.0}).audio_stop.recheck_gain == 2.0


def test_diagnostic_overlay_is_nonpublishing_and_bounded():
    path = CONFIG_PATH.with_name('audio_stop_diagnostic.yaml')
    values = flatten(yaml.safe_load(path.read_text())['voice_command_node']['ros__parameters'])
    assert values['publish_commands'] is False
    config = build_pipeline_config(values)
    assert config.continuous_recording_seconds == 300.0
    assert config.audio_stop.mode == 'shadow'
    assert config.audio_stop.recheck_gain == 2.0
