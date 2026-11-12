"""Exercise enrollment boundaries and frozen-threshold validation without recording people."""

import json
import hashlib
from pathlib import Path
import sys
from types import SimpleNamespace
import wave

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from calibrate_speaker import evaluate  # noqa: E402
from speaker_audio import dataset, private_json, read_pcm  # noqa: E402
from voice_command_pkg.speaker_backend import MODEL_SHA256  # noqa: E402
from voice_command_pkg.speaker_profile import load_profile  # noqa: E402


def manifest(tmp_path, purpose, start=100, count=10, consent=True):
    directory = tmp_path / purpose
    directory.mkdir(exist_ok=True)
    rows = []
    for index in range(count):
        pcm = (start + index).to_bytes(2, 'little') * 16000
        path = directory / f'{index}.wav'
        with wave.open(str(path), 'wb') as wav:
            wav.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
            wav.writeframes(pcm)
        rows.append({'file': path.name, 'pcm_sha256': hashlib.sha256(pcm).hexdigest()})
    result = directory / 'manifest.json'
    result.write_text(json.dumps({'purpose': purpose, 'complete': True,
                                 'consent': consent, 'clips': rows}))
    return result


def template():
    return {'schema': 1, 'backend': 'resemblyzer-0.1.4', 'model_sha256': MODEL_SHA256,
            'sample_rate': 16000, 'min_speech_seconds': .6,
            'embedding': [1.] + [0.] * 255, 'threshold': .75,
            'calibrated': False, 'enrollment_hashes': [f'{i:064x}' for i in range(20)]}


def test_private_json_does_not_overwrite(tmp_path):
    path = tmp_path / 'owner.json'
    private_json(path, template())
    assert path.stat().st_mode & 0o777 == 0o600
    with pytest.raises(FileExistsError):
        private_json(path, {'bad': True})
    assert load_profile(path, MODEL_SHA256)['calibrated'] is False


def test_changed_reused_audio_and_wrong_roles_are_rejected(tmp_path):
    path = manifest(tmp_path, 'genuine-dev')
    rows = dataset(path, 'genuine-dev', 10)
    with pytest.raises(ValueError, match='reused'):
        dataset(path, 'genuine-dev', 10, [rows[0][0]])
    with pytest.raises(ValueError, match='purpose'):
        dataset(path, 'genuine-test', 10)
    data = json.loads(path.read_text())
    data['clips'][0]['pcm_sha256'] = 'wrong'
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError, match='changed'):
        dataset(path, 'genuine-dev', 10)


def test_other_recordings_require_consent(tmp_path):
    path = manifest(tmp_path, 'other-dev', consent=False)
    with pytest.raises(ValueError, match='consent'):
        dataset(path, 'other-dev', 10)


def test_duplicate_across_genuine_and_other_fails(tmp_path):
    genuine = manifest(tmp_path, 'genuine-dev', start=100)
    other = manifest(tmp_path, 'other-dev', start=100)
    with pytest.raises(ValueError, match='reused'):
        evaluate(template(), genuine, other, 'calibrate', None)


def test_disjoint_validation_enables_only_passing_profile(tmp_path):
    genuine = manifest(tmp_path, 'genuine-dev', start=100)
    other = manifest(tmp_path, 'other-dev', start=200)
    genuine_test = manifest(tmp_path, 'genuine-test', start=110)
    other_test = manifest(tmp_path, 'other-test', start=210)

    def encode(pcm):
        value = int.from_bytes(pcm[:2], 'little')
        return ([1., 0.] if value < 200 else [0., 1.]) + [0.] * 254
    encoder = SimpleNamespace(encode=encode)
    dev = evaluate(template(), genuine, other, 'calibrate', encoder)
    assert not dev['calibrated']
    assert dev['development']['far'] == dev['development']['frr'] == 0
    result = evaluate(dev, genuine_test, other_test, 'validate', encoder)
    assert result['calibrated']
    assert result['threshold'] == dev['threshold']
    path = tmp_path / 'validated.json'
    private_json(path, result)
    assert load_profile(path, MODEL_SHA256)['calibrated']
    # Independent evaluation failures must count, not disappear from the denominator.
    broken = SimpleNamespace(encode=lambda pcm: (_ for _ in ()).throw(ValueError('quality')))
    rejected = evaluate(dev, genuine_test, other_test, 'validate', broken)
    assert not rejected['calibrated']
    assert rejected['validation']['frr'] == 1


def test_validation_refuses_development_pcm_rewrapped_as_test(tmp_path):
    genuine = manifest(tmp_path, 'genuine-dev', start=100)
    other = manifest(tmp_path, 'other-dev', start=200)
    encoder = SimpleNamespace(encode=lambda pcm: (
        ([1., 0.] if int.from_bytes(pcm[:2], 'little') < 200 else [0., 1.]) + [0.] * 254))
    dev = evaluate(template(), genuine, other, 'calibrate', encoder)
    genuine_test = manifest(tmp_path, 'genuine-test', start=100)
    other_test = manifest(tmp_path, 'other-test', start=210)
    with pytest.raises(ValueError, match='reused'):
        evaluate(dev, genuine_test, other_test, 'validate', encoder)


def test_wrong_sample_rate_fails(tmp_path):
    path = tmp_path / 'wrong.wav'
    with wave.open(str(path), 'wb') as wav:
        wav.setparams((1, 2, 8000, 0, 'NONE', 'not compressed'))
        wav.writeframes(b'\0\0' * 8000)
    with pytest.raises(ValueError, match='16 kHz'):
        read_pcm(path)


def test_prompted_enrollment_creates_twenty_clips_without_ros(monkeypatch, tmp_path):
    import io
    import record_speaker

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr('builtins.input', lambda prompt: '')
    monkeypatch.setattr(record_speaker, 'SpeakerEncoder', lambda: SimpleNamespace(
        encode=lambda pcm: [1.] + [0.] * 255))
    calls = []

    class Recorder:
        def __init__(self, command, **kwargs):
            calls.append(command)
            pcm = len(calls).to_bytes(2, 'little') * 16000 * 6
            self.stdout = io.BytesIO(bytes(32000) + pcm)
            self.returncode = 0

        def communicate(self, timeout):
            return self.stdout.read(), None

        def poll(self):
            return 0

    monkeypatch.setattr(record_speaker.subprocess, 'Popen', Recorder)
    args = SimpleNamespace(purpose='enrollment', device='pulse',
                           profile=tmp_path / 'speaker_profiles' / 'owner.json')
    record_speaker.record(args)
    assert len(calls) == 20
    assert all(command[0] == 'arecord' for command in calls)
    assert not load_profile(args.profile, MODEL_SHA256)['calibrated']
    path = next((tmp_path / 'recordings').glob('*/manifest.json'))
    assert len(dataset(path, 'enrollment', 20)) == 20
    with pytest.raises(ValueError, match='이미'):
        record_speaker.record(args)
    assert len(calls) == 20
