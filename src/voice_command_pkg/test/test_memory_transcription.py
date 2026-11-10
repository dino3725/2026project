"""Check PCM conversion, optional recording and file-tool compatibility without ROS."""

import json
from pathlib import Path
from types import SimpleNamespace
import wave

import numpy as np
import pytest

from voice_command_pkg import pipeline
from voice_command_pkg.audio_capture import FRAME_BYTES, SAMPLE_RATE
from voice_command_pkg.audio_samples import pcm_to_float32
from voice_command_pkg.parameters import build_pipeline_config
from voice_command_pkg.utterance_buffer import Utterance


PCM = b'\x00\x40' * (FRAME_BYTES // 2)


def test_pcm_conversion_preserves_sign_endianness_and_scale():
    # -32768, -1, 0, 1, 256, 32767 in signed little-endian form.
    pcm = b'\x00\x80\xff\xff\x00\x00\x01\x00\x00\x01\xff\x7f'
    samples = pcm_to_float32(pcm)
    expected = np.array([-32768, -1, 0, 1, 256, 32767], dtype=np.float32) / 32768
    np.testing.assert_array_equal(samples, expected)
    assert samples.dtype == np.float32
    assert samples.shape == (6,)
    assert samples.flags.c_contiguous


def test_one_second_pcm_keeps_length_and_silence():
    samples = pcm_to_float32(bytes(SAMPLE_RATE * 2))
    assert samples.shape == (SAMPLE_RATE,)
    assert np.count_nonzero(samples) == 0


@pytest.mark.parametrize('pcm', [b'', b'\x00', b'\x00\x01\x02'])
def test_invalid_pcm_is_rejected(pcm):
    with pytest.raises(ValueError):
        pcm_to_float32(pcm)


@pytest.mark.parametrize('source', [PCM, Path('legacy.wav')])
def test_transcriber_consumes_segments_and_preserves_file_input(source):
    received = []

    def transcribe(audio, language):
        received.append((audio, language))

        def segments():
            yield SimpleNamespace(text=' 멈')
            yield SimpleNamespace(text='춰 ')

        return segments(), None

    text, seconds = pipeline.transcribe_audio(SimpleNamespace(transcribe=transcribe), source, 'ko')
    assert text == '멈춰'
    assert seconds >= 0
    assert received[0][1] == 'ko'
    if isinstance(source, bytes):
        np.testing.assert_array_equal(received[0][0], np.full(FRAME_BYTES // 2, 0.5, np.float32))
    else:
        assert received[0][0] == 'legacy.wav'


def test_segment_iteration_error_is_reported_as_stt_error():
    def segments():
        yield SimpleNamespace(text='부분')
        raise RuntimeError('inference failed')

    model = SimpleNamespace(transcribe=lambda *args, **kwargs: (segments(), None))
    with pytest.raises(pipeline.PipelineError, match='전사에 실패'):
        pipeline.transcribe_audio(model, PCM, 'ko')


def test_empty_transcript_is_still_rejected():
    model = SimpleNamespace(transcribe=lambda *args, **kwargs: (iter([]), None))
    with pytest.raises(pipeline.PipelineError, match='비어'):
        pipeline.transcribe_audio(model, PCM, 'ko')


def make_pipeline(monkeypatch, recordings_dir, save_recordings=False):
    inputs, published, logs = [], [], []

    def transcribe(audio, language):
        assert isinstance(audio, np.ndarray), 'pipeline must always transcribe from memory'
        inputs.append(audio.copy())
        return iter([SimpleNamespace(text='멈춰')]), None

    model = SimpleNamespace(transcribe=transcribe)
    monkeypatch.setattr(pipeline, 'load_whisper_model', lambda config: (model, 0.0))
    monkeypatch.setattr(pipeline, 'LlmRuntime', lambda *args: SimpleNamespace(close=lambda: None))

    def publish(command, text):
        published.append(command)
        return True

    instance = pipeline.VoiceCommandPipeline(
        pipeline.PipelineConfig(recordings_dir=recordings_dir, save_recordings=save_recordings),
        publish=publish, log=logs.append,
    )
    return instance, inputs, published, logs


def process(instance, sequence=1):
    now = pipeline.time.perf_counter()
    return instance.process_utterance(Utterance(sequence, PCM, {
        'voice_start_wall': now - 0.03, 'voice_end_wall': now,
        'utterance_seconds': 0.03, 'vad_end_silence_seconds': 0.0,
    }))


def test_saving_disabled_does_not_create_directory_or_file(monkeypatch, tmp_path):
    target = tmp_path / 'must_not_be_created'
    instance, inputs, published, logs = make_pipeline(monkeypatch, target)
    assert not target.exists()
    result = process(instance)
    assert not target.exists()
    assert result['audio_path'] is None
    assert result['recording_saved'] is False
    assert result['recording_error'] == ''
    assert len(inputs) == 1
    assert published == [{'action': 'stop', 'object': ''}]
    assert any('recording=disabled' in line for line in logs)
    json.dumps(result)  # Optional paths remain JSON-safe for measurement consumers.


def test_saving_enabled_writes_original_pcm_but_transcribes_array(monkeypatch, tmp_path):
    instance, inputs, _, logs = make_pipeline(monkeypatch, tmp_path / 'recordings', True)
    result = process(instance)
    assert result['recording_saved'] is True
    assert result['recording_error'] == ''
    with wave.open(result['audio_path'], 'rb') as wav:
        assert wav.getnchannels() == 1
        assert wav.getsampwidth() == 2
        assert wav.getframerate() == SAMPLE_RATE
        assert wav.readframes(wav.getnframes()) == PCM
    np.testing.assert_array_equal(inputs[0], pcm_to_float32(PCM))
    assert any('recording=' + result['audio_path'] in line for line in logs)


@pytest.mark.parametrize('save_recordings', [False, True])
def test_unusable_directory_does_not_block_start_or_stop_detection(
    monkeypatch, tmp_path, save_recordings,
):
    blocker = tmp_path / 'file_not_directory'
    blocker.write_text('keep me')
    instance, inputs, published, logs = make_pipeline(
        monkeypatch, blocker / 'child', save_recordings
    )
    for sequence in (1, 2):
        result = process(instance, sequence)
        assert result['audio_path'] is None
        assert bool(result['recording_error']) is save_recordings
    assert len(inputs) == len(published) == 2
    assert blocker.read_text() == 'keep me'
    assert any('[RECORDING ERROR]' in line for line in logs) is save_recordings


def test_disk_write_failure_continues_transcription(monkeypatch, tmp_path):
    instance, inputs, published, _ = make_pipeline(monkeypatch, tmp_path, True)

    def broken_open(*args, **kwargs):
        raise OSError('disk full')

    monkeypatch.setattr(pipeline.wave, 'open', broken_open)
    result = process(instance)
    assert 'disk full' in result['recording_error']
    assert result['audio_path'] is None
    assert result['recording_saved'] is False
    assert len(inputs) == len(published) == 1


def test_shutdown_during_recording_failure_never_transcribes_or_publishes(monkeypatch, tmp_path):
    instance, inputs, published, _ = make_pipeline(monkeypatch, tmp_path, True)

    def interrupted_save(*args):
        instance.request_stop()
        raise pipeline.PipelineError('녹음 저장', 'interrupted')

    monkeypatch.setattr(pipeline, 'save_wav', interrupted_save)
    with pytest.raises(pipeline.PipelineStopped):
        process(instance)
    assert inputs == published == []


def test_save_recordings_parameter_default_and_override():
    assert build_pipeline_config({}).save_recordings is False
    assert build_pipeline_config({'save_recordings': True}).save_recordings is True
    with pytest.raises(ValueError, match='boolean'):
        build_pipeline_config({'save_recordings': 'false'})
