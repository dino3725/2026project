"""Check diagnostic coverage, resource bounds and isolation from disk failures."""

import hashlib
import json
import threading
import wave

import pytest

from voice_command_pkg import continuous_recording
from voice_command_pkg.audio_capture import FRAME_BYTES
from voice_command_pkg.continuous_recording import ContinuousRecording
from voice_command_pkg.parameters import build_pipeline_config


@pytest.mark.parametrize('value', [True, -1, .01, 301, float('nan'), float('inf'), '10'])
def test_recording_duration_rejects_unbounded_or_invalid_input(value):
    with pytest.raises(ValueError):
        build_pipeline_config({'continuous_recording_seconds': value})


def test_recording_defaults_off():
    assert build_pipeline_config({}).continuous_recording_seconds == 0
    assert build_pipeline_config({'continuous_recording_seconds': 180.0})


def read_result(recorder):
    recorder.worker.join(timeout=2)
    assert not recorder.worker.is_alive()
    assert not recorder.error
    metadata = json.loads(recorder.result_path.read_text())
    with wave.open(str(recorder.result_path.parent / metadata['wav']), 'rb') as handle:
        actual = (handle.getnchannels(), handle.getsampwidth(), handle.getframerate())
        assert actual == (1, 2, 16000)
        pcm = handle.readframes(handle.getnframes())
    assert hashlib.sha256(pcm).hexdigest() == metadata['pcm_sha256']
    return metadata, pcm


def test_limit_saves_exact_prefix_and_preserves_silent_frames(tmp_path):
    logs = []
    recorder = ContinuousRecording(tmp_path, .06, {'vad': {}}, logs.append)
    first, second = bytes(FRAME_BYTES), b'\1\0' * (FRAME_BYTES // 2)
    recorder.submit(first, .03, 10.0)
    recorder.submit(second, .06, 10.03)
    recorder.submit(bytes(FRAME_BYTES), .09, 10.06)
    recorder.close()
    metadata, pcm = read_result(recorder)
    assert pcm == first + second
    assert metadata['end_reason'] == 'duration_limit'
    assert metadata['audio_end'] == .06
    assert metadata['first_frame_read_monotonic'] == 10.0
    assert metadata['last_frame_read_monotonic'] == 10.03
    assert metadata['replay_warmup_ms'] == 0


def test_shutdown_flushes_prefix_without_overwriting_prior_runs(tmp_path):
    paths = []
    for _ in range(2):
        recorder = ContinuousRecording(tmp_path, 10, {}, lambda line: None)
        recorder.submit(bytes(FRAME_BYTES), .03, 10.0)
        recorder.close('input_error')
        recorder.close()
        metadata, pcm = read_result(recorder)
        assert metadata['frames'] == 1
        assert metadata['end_reason'] == 'input_error'
        paths.append(recorder.result_path)
    assert paths[0] != paths[1]
    assert all(path.exists() for path in paths)


def test_timeline_gap_is_not_silently_concatenated(tmp_path):
    recorder = ContinuousRecording(tmp_path, 10, {}, lambda line: None)
    recorder.submit(bytes(FRAME_BYTES), .03, 10.0)
    recorder.submit(bytes(FRAME_BYTES), .09, 10.06)
    metadata, pcm = read_result(recorder)
    assert metadata['end_reason'] == 'input_discontinuity'
    assert len(pcm) == FRAME_BYTES


def test_disk_failure_does_not_escape_to_capture_thread(tmp_path):
    blocked = tmp_path / 'file'
    blocked.write_text('not a directory')
    logs = []
    recorder = ContinuousRecording(blocked, .03, {}, logs.append)
    recorder.submit(bytes(FRAME_BYTES), .03, 10.0)
    recorder.worker.join(timeout=2)
    assert recorder.error
    assert recorder.result_path is None
    recorder.submit(bytes(FRAME_BYTES), .06, 10.03)
    recorder.close()
    assert any('[CONTINUOUS AUDIO ERROR]' in line for line in logs)


def test_stalled_disk_does_not_hold_capture_or_close_lock(monkeypatch, tmp_path):
    entered, release = threading.Event(), threading.Event()
    original = continuous_recording.wave.open

    def stalled(*args, **kwargs):
        entered.set()
        assert release.wait(2)
        return original(*args, **kwargs)

    monkeypatch.setattr(continuous_recording.wave, 'open', stalled)
    recorder = ContinuousRecording(tmp_path, .03, {}, lambda line: None)
    recorder.submit(bytes(FRAME_BYTES), .03, 10.0)
    try:
        assert entered.wait(1)
        # These return while the writer is still blocked; no disk work on this thread.
        recorder.submit(bytes(FRAME_BYTES), .06, 10.03)
        recorder.close()
        assert recorder.worker.is_alive()
    finally:
        release.set()
        recorder.worker.join(timeout=2)
    assert not recorder.error
