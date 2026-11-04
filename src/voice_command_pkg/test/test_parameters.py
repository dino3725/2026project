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


def test_unknown_parameter_is_rejected():
    with pytest.raises(ValueError):
        build_pipeline_config({'vad.end_silence': 600})
