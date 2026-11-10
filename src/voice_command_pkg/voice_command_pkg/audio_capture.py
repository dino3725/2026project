#!/usr/bin/env python3
"""PulseAudio capture and WebRTC VAD utterance detection (no ROS imports)."""

from __future__ import annotations

import audioop
from collections import deque
from dataclasses import dataclass
from enum import auto, Enum
import os
import select
import subprocess
import sys
import threading
import time
from typing import BinaryIO


# Fixed by WebRTC VAD and the Whisper input format; not tunable.
SAMPLE_RATE = 16_000
FRAME_MS = 30
SAMPLE_WIDTH = 2
CHANNELS = 1
FRAME_SAMPLES = SAMPLE_RATE * FRAME_MS // 1_000
FRAME_BYTES = FRAME_SAMPLES * SAMPLE_WIDTH * CHANNELS


@dataclass(frozen=True)
class VadConfig:
    """
    Tunable VAD values. Defaults are the 2026-11-04 baseline.

    Start-of-speech uses a rolling window so isolated noisy frames cannot
    trigger a new utterance. End-of-speech needs consecutive silent frames.
    """

    vad_mode: int = 3
    start_min_rms: int = 200
    stream_warmup_ms: int = 1_200
    start_window_ms: int = 300
    start_speech_ratio: float = 0.6
    end_silence_ms: int = 900
    pre_roll_ms: int = 450
    max_utterance_seconds: float = 15.0

    def __post_init__(self) -> None:
        if self.vad_mode not in (0, 1, 2, 3):
            raise ValueError(f'vad_mode must be 0-3: {self.vad_mode}')
        if not 0.0 < self.start_speech_ratio <= 1.0:
            raise ValueError(f'start_speech_ratio must be in (0, 1]: {self.start_speech_ratio}')
        frame_lengths_ms = {
            'start_window_ms': self.start_window_ms,
            'end_silence_ms': self.end_silence_ms,
            'max_utterance_seconds': self.max_utterance_seconds * 1_000,
        }
        for name, length_ms in frame_lengths_ms.items():
            if length_ms < FRAME_MS:
                raise ValueError(f'{name} must cover at least one {FRAME_MS} ms frame')
        for name in ('start_min_rms', 'stream_warmup_ms', 'pre_roll_ms'):
            if getattr(self, name) < 0:
                raise ValueError(f'{name} must not be negative')

    @property
    def stream_warmup_frames(self) -> int:
        return self.stream_warmup_ms // FRAME_MS

    @property
    def start_window_frames(self) -> int:
        return self.start_window_ms // FRAME_MS

    @property
    def start_required_speech_frames(self) -> int:
        return round(self.start_window_frames * self.start_speech_ratio)

    @property
    def end_silence_frames(self) -> int:
        return (self.end_silence_ms + FRAME_MS - 1) // FRAME_MS

    @property
    def pre_roll_frames(self) -> int:
        return self.pre_roll_ms // FRAME_MS

    @property
    def max_utterance_frames(self) -> int:
        return int(self.max_utterance_seconds * 1_000) // FRAME_MS


class AudioCaptureError(RuntimeError):
    """An expected capture or VAD failure."""


class VadState(Enum):
    """Current detector state."""

    LISTENING = auto()
    SPEAKING = auto()


class VadStateMachine:
    """Convert per-frame VAD decisions into debounced state transitions."""

    def __init__(self, config: VadConfig | None = None) -> None:
        self.config = config or VadConfig()
        self.state = VadState.LISTENING
        self.frame_index = 0
        self.start_window: deque[bool] = deque(maxlen=self.config.start_window_frames)
        self.silence_frames = 0

    @property
    def audio_time(self) -> float:
        """Return processed audio duration, independent of wall-clock buffering."""
        return self.frame_index * FRAME_MS / 1_000.0

    def reset_to_listening(self) -> None:
        """Reset utterance state while preserving the capture timeline."""
        self.state = VadState.LISTENING
        self.start_window.clear()
        self.silence_frames = 0

    def process_frame(self, is_speech: bool) -> tuple[str, dict[str, float | int]] | None:
        """Process one 30 ms decision and return a transition when one occurs."""
        self.frame_index += 1
        config = self.config

        if self.state is VadState.LISTENING:
            self.start_window.append(is_speech)
            if len(self.start_window) < config.start_window_frames:
                return None

            speech_frames = sum(self.start_window)
            if speech_frames < config.start_required_speech_frames:
                return None

            speech_ratio = speech_frames / config.start_window_frames
            self.state = VadState.SPEAKING
            self.start_window.clear()
            self.silence_frames = 0
            return (
                'start',
                {
                    'audio_time': self.audio_time,
                    'speech_frames': speech_frames,
                    'window_frames': config.start_window_frames,
                    'speech_ratio': speech_ratio,
                },
            )

        # The start window is not updated while SPEAKING. Only consecutive
        # non-speech frames participate in the end condition.
        if is_speech:
            self.silence_frames = 0
            return None

        self.silence_frames += 1
        if self.silence_frames < config.end_silence_frames:
            return None

        end_silence_frames = self.silence_frames
        self.reset_to_listening()
        return (
            'end',
            {
                'audio_time': self.audio_time,
                'silence_frames': end_silence_frames,
                'silence_seconds': end_silence_frames * FRAME_MS / 1_000.0,
            },
        )


def qualify_speech_for_state(
    state: VadState,
    vad_speech: bool,
    frame: bytes,
    start_min_rms: int,
) -> bool:
    """Apply a low-level noise gate only while waiting for speech to start."""
    if state is VadState.SPEAKING:
        return vad_speech
    return vad_speech and audioop.rms(frame, SAMPLE_WIDTH) >= start_min_rms


def get_default_source() -> str:
    """Return the current PulseAudio-compatible default input source."""
    try:
        completed = subprocess.run(
            ['pactl', 'get-default-source'],
            capture_output=True,
            text=True,
            check=True,
            timeout=2,
        )
    except FileNotFoundError as exc:
        raise AudioCaptureError('pactl 실행 파일을 찾을 수 없습니다.') from exc
    except subprocess.CalledProcessError as exc:
        detail = exc.stderr.strip() or f'exit={exc.returncode}'
        raise AudioCaptureError(f'기본 마이크 source 조회 실패: {detail}') from exc
    except subprocess.TimeoutExpired as exc:
        raise AudioCaptureError('기본 마이크 source 조회 시간이 초과되었습니다.') from exc

    source = completed.stdout.strip()
    if not source:
        raise AudioCaptureError('기본 마이크 source가 비어 있습니다.')
    return source


def start_capture(source: str) -> subprocess.Popen[bytes]:
    """Start one continuous raw PCM stream from the selected source."""
    command = [
        'parec',
        '--device',
        source,
        '--raw',
        '--format=s16le',
        f'--rate={SAMPLE_RATE}',
        f'--channels={CHANNELS}',
    ]
    try:
        return subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
        )
    except FileNotFoundError as exc:
        raise AudioCaptureError('parec 실행 파일을 찾을 수 없습니다.') from exc


def read_frame(stream: BinaryIO) -> bytes | None:
    """Read exactly one VAD frame; never return a partial frame."""
    chunks: list[bytes] = []
    remaining = FRAME_BYTES
    while remaining:
        chunk = stream.read(remaining)
        if not chunk:
            return None
        chunks.append(chunk)
        remaining -= len(chunk)
    return b''.join(chunks)


def read_frame_interruptible(
    stream: BinaryIO,
    stop_event: threading.Event,
    timeout_seconds: float = 2.0,
) -> bytes | None:
    """Read a pipe frame with bounded waits, even if the producer stalls."""
    chunks: list[bytes] = []
    remaining = FRAME_BYTES
    deadline = time.monotonic() + timeout_seconds
    while remaining:
        if stop_event.is_set():
            raise InterruptedError('audio capture stopped')
        wait = deadline - time.monotonic()
        if wait <= 0:
            raise AudioCaptureError(
                f'마이크 PCM 입력이 {timeout_seconds:g}초 동안 도착하지 않았습니다.'
            )
        ready, _, _ = select.select([stream], [], [], min(wait, 0.1))
        if not ready:
            continue
        chunk = os.read(stream.fileno(), remaining)
        if not chunk:
            return None
        chunks.append(chunk)
        remaining -= len(chunk)
    return b''.join(chunks)


def stop_capture(process: subprocess.Popen[bytes]) -> str:
    """Stop parec and return its diagnostic output without leaving a zombie."""
    if process.poll() is None:
        process.terminate()
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=2)

    detail = ''
    if process.stderr is not None:
        detail = process.stderr.read().decode('utf-8', errors='replace').strip()
        process.stderr.close()
    if process.stdout is not None:
        process.stdout.close()
    return detail


def run_vad_monitor(config: VadConfig) -> None:
    """Print debounced speech start/end events until Ctrl+C (VAD tuning aid)."""
    import webrtcvad

    vad = webrtcvad.Vad(config.vad_mode)
    detector = VadStateMachine(config)
    source = get_default_source()
    process = start_capture(source)

    assert process.stdout is not None
    try:
        for frame_index in range(config.stream_warmup_frames):
            if read_frame(process.stdout) is None:
                raise AudioCaptureError(
                    'PulseAudio stream 안정화 중 PCM 입력이 종료됐습니다: '
                    f'frame={frame_index}/{config.stream_warmup_frames}'
                )

        print(f'[SOURCE]\n{source}', flush=True)
        print('\n[LISTENING]', flush=True)
        while True:
            frame = read_frame(process.stdout)
            if frame is None:
                raise AudioCaptureError(
                    f'PulseAudio PCM stream이 종료되었습니다: parec exit={process.poll()}'
                )
            qualified = qualify_speech_for_state(
                detector.state,
                vad.is_speech(frame, SAMPLE_RATE),
                frame,
                config.start_min_rms,
            )
            transition = detector.process_frame(qualified)
            if transition is None:
                continue
            event, details = transition
            if event == 'start':
                print(
                    f"\n[VOICE START] audio={details['audio_time']:.3f}s "
                    f"speech={details['speech_frames']}/{details['window_frames']}",
                    flush=True,
                )
            else:
                print(
                    f"[VOICE END] audio={details['audio_time']:.3f}s "
                    f"silence={details['silence_seconds']:.3f}s",
                    flush=True,
                )
                print('\n[LISTENING]', flush=True)
    finally:
        stop_capture(process)


def main() -> int:
    try:
        run_vad_monitor(VadConfig())
    except KeyboardInterrupt:
        print('\nVAD monitor 종료', flush=True)
        return 0
    except AudioCaptureError as exc:
        print(f'\n[ERROR]\n{exc}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
