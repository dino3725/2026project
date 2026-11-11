"""Protect development metric denominators and explicit audio/label matching."""

import json
from pathlib import Path
import sys
import wave

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from compare_stt_settings import (  # noqa: E402
    distance, load_samples, normalize, SETTINGS, summarize,
)


@pytest.mark.parametrize('left,right,expected', [
    ('', '', 0), ('', 'abc', 3), ('abc', '', 3), ('바세린', '파세린', 1),
    ('스톱', '스톱', 0), ('abc', 'axc', 1), ('abc', 'abcd', 1),
])
def test_character_distance(left, right, expected):
    assert distance(left, right) == expected


def test_normalization_keeps_real_errors():
    assert normalize('콜라 가져다 줘!') == normalize('콜라 가져다줘')
    assert normalize('바세린') != normalize('파세린')


def make_manifest(tmp_path):
    path = tmp_path / 'audio.wav'
    with wave.open(str(path), 'wb') as wav:
        wav.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
        wav.writeframes(bytes(32000))
    entry = {'file': path.name, 'reference': '스톱', 'action': 'stop', 'object': ''}
    manifest = tmp_path / 'manifest.json'
    manifest.write_text(json.dumps({'purpose': 'development', 'samples': [entry]}))
    return manifest, entry


def test_relative_manifest_and_hash(tmp_path):
    manifest, _ = make_manifest(tmp_path)
    entries = load_samples(manifest)
    assert len(entries) == 1
    assert entries[0][0]['duration_seconds'] == 1
    assert len(entries[0][0]['sha256']) == 64
    assert entries[0][1] == bytes(32000)


@pytest.mark.parametrize('failure', ['duplicate', 'hash', 'label', 'heldout', 'empty'])
def test_bad_manifest_rejected(tmp_path, failure):
    manifest, entry = make_manifest(tmp_path)
    data = json.loads(manifest.read_text())
    if failure == 'duplicate':
        data['samples'].append(entry)
    elif failure == 'hash':
        data['samples'][0]['sha256'] = 'incorrect'
    elif failure == 'label':
        data['samples'][0]['object'] = 'coke'
    elif failure == 'heldout':
        data['purpose'] = 'heldout'
    else:
        data['samples'] = []
    manifest.write_text(json.dumps(data))
    with pytest.raises(ValueError):
        load_samples(manifest)


def test_controls_excluded_from_accuracy_and_latency():
    rows = []
    for setting in SETTINGS:
        for action, text, seconds, control in [
            ('stop', '스톱', 2, False), ('unknown', '안녕', 4, False),
            ('unknown', '', 20, True),
        ]:
            rows.append({'setting': setting, 'action': action, 'transcript': text,
                         'seconds': seconds, 'control': control, 'error': '',
                         'normalized_exact': True, 'edits': 0, 'reference_length': len(text),
                         'stop_keyword': '스톱' if action == 'stop' else None,
                         'command_correct': True, 'command_error': ''})
    for result in summarize(rows, True).values():
        assert result['runs'] == result['normalized_exact'] == result['command_correct'] == 2
        assert result['median_seconds'] == 3
        assert result['stop_detected'] == result['stop_runs'] == 1
        assert result['false_stop'] == result['control_nonempty'] == 0
        assert result['control_runs'] == 1
