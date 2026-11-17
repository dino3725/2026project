"""Bounded diagnostic PCM storage, independent of VAD and command processing."""

import hashlib
import json
import math
from pathlib import Path
import tempfile
import threading
import wave

from voice_command_pkg.audio_capture import FRAME_BYTES, FRAME_MS, SAMPLE_RATE


def validate_recording_seconds(seconds):
    """Allow disabled storage or a finite, bounded diagnostic session."""
    if (type(seconds) not in (int, float) or not math.isfinite(seconds)
            or not 0 <= seconds <= 300 or 0 < seconds < FRAME_MS / 1000):
        raise ValueError('continuous_recording_seconds must be 0 or between 0.03 and 300')


class ContinuousRecording:
    """Keep a contiguous PCM prefix in bounded memory; write only on a worker."""

    def __init__(self, directory, seconds, settings, log):
        validate_recording_seconds(seconds)
        if seconds == 0:
            raise ValueError('Do not construct a recorder when disabled')
        self.directory = Path(directory)
        self.max_frames = math.ceil(seconds * 1000 / FRAME_MS)
        self.settings = settings
        self.log = log
        self.frames = []
        self.first_read_wall = None
        self.last_read_wall = None
        self.reason = ''
        self.error = ''
        self.result_path = None
        self.lock = threading.Lock()
        self.finished = threading.Event()
        self.worker = threading.Thread(target=self._write, name='continuous_wav', daemon=True)
        self.worker.start()
        self.log(f'[CONTINUOUS AUDIO ARMED] limit_frames={self.max_frames} '
                 'post_warmup=true; saving on limit or shutdown')

    def submit(self, frame, audio_end, received_at):
        """Append before the VAD gate, without filesystem access or disk waits."""
        with self.lock:
            if self.finished.is_set():
                return
            expected_end = (len(self.frames) + 1) * FRAME_MS / 1000
            if len(frame) != FRAME_BYTES or abs(audio_end - expected_end) > 0.0001:
                self.reason = 'input_discontinuity'
                self.finished.set()
                return
            self.frames.append(frame)
            if self.first_read_wall is None:
                self.first_read_wall = received_at
            self.last_read_wall = received_at
            if len(self.frames) >= self.max_frames:
                self.reason = 'duration_limit'
                self.finished.set()

    def close(self, reason='shutdown'):
        """Stop accepting frames; no disk wait on the capture/shutdown path."""
        with self.lock:
            if not self.finished.is_set():
                self.reason = reason
                self.finished.set()

    def _write(self):
        self.finished.wait()
        with self.lock:
            frames, self.frames = self.frames, []
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            directory = Path(tempfile.mkdtemp(prefix='continuous_', dir=self.directory))
            wav_path = directory / 'continuous.wav'
            digest = hashlib.sha256()
            with wave.open(str(wav_path), 'wb') as handle:
                handle.setparams((1, 2, SAMPLE_RATE, 0, 'NONE', 'not compressed'))
                for frame in frames:
                    handle.writeframesraw(frame)
                    digest.update(frame)
            metadata = {
                'schema_version': 1, 'wav': wav_path.name, 'pcm_sha256': digest.hexdigest(),
                'sample_rate': SAMPLE_RATE, 'frame_ms': FRAME_MS, 'frames': len(frames),
                'duration_seconds': len(frames) * FRAME_MS / 1000,
                'audio_start': 0.0, 'audio_end': len(frames) * FRAME_MS / 1000,
                'post_warmup': True, 'replay_warmup_ms': 0,
                'first_frame_read_monotonic': self.first_read_wall,
                'last_frame_read_monotonic': self.last_read_wall,
                'end_reason': self.reason, 'settings': self.settings,
                'scope': 'Contiguous post-warmup input prefix, including VAD-rejected audio. '
                         'Not microphone hardware timestamps or a full session guarantee.',
            }
            path = directory / 'metadata.json'
            path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + '\n')
            self.result_path = path
            self.log(f'[CONTINUOUS AUDIO SAVED] metadata={path} '
                     f'frames={len(frames)} reason={self.reason}')
        except Exception as exc:
            self.error = str(exc)
            self.log(f'[CONTINUOUS AUDIO ERROR] diagnostic save failed: {exc}')
