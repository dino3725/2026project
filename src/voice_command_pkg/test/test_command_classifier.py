import hashlib

import pytest

from voice_command_pkg.command_classifier import (
    build_request_payload,
    ClassifierConfig,
    OUTPUT_SCHEMA,
    SYSTEM_PROMPT,
    validate_result,
)
from voice_command_pkg.command_protocol import VALID_OBJECTS


# SHA256 of the prompt last checked with tools/voice_eval/test_llm_pick_objects.py
# (2026-11-04, stop removed from the LLM; see results/stop_rules_20261104.md).
# If you change the prompt on purpose, rerun the regression test, record the
# result in tools/voice_eval/results/, then update this value.
FROZEN_PROMPT_SHA256 = 'b184e9bc9b5c26ef822a79d2d18025aaecbde07f014325f2515817b653ff8df6'


def test_prompt_is_unchanged_since_last_regression_run():
    digest = hashlib.sha256(SYSTEM_PROMPT.encode('utf-8')).hexdigest()
    assert digest == FROZEN_PROMPT_SHA256


def test_schema_objects_match_protocol_allowlist():
    objects = set(OUTPUT_SCHEMA['properties']['object']['enum']) - {''}
    assert objects == VALID_OBJECTS


def test_llm_cannot_output_stop():
    assert OUTPUT_SCHEMA['properties']['action']['enum'] == ['fetch', 'unknown']
    assert all(
        option['properties']['action']['const'] != 'stop' for option in OUTPUT_SCHEMA['oneOf']
    )
    with pytest.raises(ValueError):
        validate_result({'action': 'stop', 'object': ''})


@pytest.mark.parametrize('value', [
    {'action': 'fetch', 'object': 'coke'},
    {'action': 'unknown', 'object': ''},
])
def test_validate_result_accepts_allowed_pairs(value):
    assert validate_result(value) == value


@pytest.mark.parametrize('value', [
    {'action': 'stop', 'object': 'coke'},
    {'action': 'fetch', 'object': 'snack'},
    {'action': 'fetch', 'object': 'coke', 'reason': 'x'},
    {'action': 1, 'object': ''},
    [],
])
def test_validate_result_rejects_everything_else(value):
    with pytest.raises(ValueError):
        validate_result(value)


def test_request_payload_uses_config_and_strips_text():
    config = ClassifierConfig(model='m', keep_alive='-1')
    payload = build_request_payload('  콜라 줘 ', config)
    assert payload['model'] == 'm'
    assert payload['keep_alive'] == '-1'
    assert payload['messages'][1] == {'role': 'user', 'content': '콜라 줘'}
    assert payload['options']['temperature'] == 0


def test_request_payload_rejects_empty_text():
    with pytest.raises(ValueError):
        build_request_payload('   ', ClassifierConfig())


def test_urls_are_built_from_base():
    config = ClassifierConfig(ollama_url='http://host:1/')
    assert config.chat_url == 'http://host:1/api/chat'
    assert config.tags_url == 'http://host:1/api/tags'
