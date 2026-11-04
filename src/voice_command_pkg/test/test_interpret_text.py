import pytest

from voice_command_pkg import pipeline
from voice_command_pkg.command_classifier import ClassifierConfig


@pytest.fixture
def llm_calls(monkeypatch):
    calls = []

    def fake_classify_text(text, config):
        calls.append(text)
        return {'action': 'fetch', 'object': 'coke'}, 1.5

    monkeypatch.setattr(pipeline, 'classify_text', fake_classify_text)
    return calls


def test_stop_is_decided_by_rule_without_llm(llm_calls):
    command, llm_seconds, decided_by = pipeline.interpret_text('로봇 멈춰!', ClassifierConfig())
    assert command == {'action': 'stop', 'object': ''}
    assert llm_seconds == 0.0
    assert decided_by == 'rule:멈춰'
    assert llm_calls == []


def test_other_text_goes_to_llm(llm_calls):
    command, llm_seconds, decided_by = pipeline.interpret_text('목말라', ClassifierConfig())
    assert command == {'action': 'fetch', 'object': 'coke'}
    assert llm_seconds == 1.5
    assert decided_by == 'llm'
    assert llm_calls == ['목말라']


def test_stop_still_works_when_llm_is_down(monkeypatch):
    def broken(text, config):
        raise pipeline.PipelineError('LLM', 'Ollama down')

    monkeypatch.setattr(pipeline, 'classify_text', broken)
    command, _, _ = pipeline.interpret_text('정지', ClassifierConfig())
    assert command['action'] == 'stop'
    with pytest.raises(pipeline.PipelineError):
        pipeline.interpret_text('콜라 줘', ClassifierConfig())
