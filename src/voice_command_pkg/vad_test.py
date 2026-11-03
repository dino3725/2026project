#!/usr/bin/env python3
"""Continuously report speech start/end events from the default microphone."""

from __future__ import annotations

import subprocess
import sys
import audioop
from collections import deque
from enum import Enum, auto
from typing import BinaryIO

import webrtcvad


SAMPLE_RATE = 16_000
FRAME_MS = 30
SAMPLE_WIDTH = 2
CHANNELS = 1
FRAME_SAMPLES = SAMPLE_RATE * FRAME_MS // 1_000
FRAME_BYTES = FRAME_SAMPLES * SAMPLE_WIDTH * CHANNELS
VAD_MODE = 3
START_MIN_RMS = 200
STREAM_WARMUP_MS = 1_200
STREAM_WARMUP_FRAMES = STREAM_WARMUP_MS // FRAME_MS

# Start-of-speech uses a rolling window so isolated noisy frames cannot trigger
# a new utterance. End-of-speech remains unchanged for this stage.
START_WINDOW_MS = 300
START_SPEECH_RATIO = 0.8
END_SILENCE_MS = 900
START_WINDOW_FRAMES = START_WINDOW_MS // FRAME_MS
START_REQUIRED_SPEECH_FRAMES = round(START_WINDOW_FRAMES * START_SPEECH_RATIO)
END_SILENCE_FRAMES = (END_SILENCE_MS + FRAME_MS - 1) // FRAME_MS


class VadTestError(RuntimeError):
    """An expected capture or VAD test failure."""


class VadState(Enum):
    """Current standalone detector state."""

    LISTENING = auto()
    SPEAKING = auto()


class VadStateMachine:
    """Convert per-frame VAD decisions into debounced state transitions."""

    def __init__(self) -> None:
        self.state = VadState.LISTENING
        self.frame_index = 0
        self.start_window: deque[bool] = deque(maxlen=START_WINDOW_FRAMES)
        self.silence_frames = 0

    @property
    def audio_time(self) -> float:
        """Processed audio duration, independent of wall-clock buffering."""
        return self.frame_index * FRAME_MS / 1_000.0

    def reset_to_listening(self) -> None:
        """Reset utterance state while preserving the capture timeline."""
        self.state = VadState.LISTENING
        self.start_window.clear()
        self.silence_frames = 0

    def process_frame(self, is_speech: bool) -> tuple[str, dict[str, float | int]] | None:
        """Process one 30 ms decision and return a transition when one occurs."""
        self.frame_index += 1

        if self.state is VadState.LISTENING:
            self.start_window.append(is_speech)
            if len(self.start_window) < START_WINDOW_FRAMES:
                return None

            speech_frames = sum(self.start_window)
            if speech_frames < START_REQUIRED_SPEECH_FRAMES:
                return None

            speech_ratio = speech_frames / START_WINDOW_FRAMES
            self.state = VadState.SPEAKING
            self.start_window.clear()
            self.silence_frames = 0
            return (
                "start",
                {
                    "audio_time": self.audio_time,
                    "speech_frames": speech_frames,
                    "window_frames": START_WINDOW_FRAMES,
                    "speech_ratio": speech_ratio,
                },
            )

        # The start window is not updated while SPEAKING. Only consecutive
        # non-speech frames participate in the unchanged end condition.
        if is_speech:
            self.silence_frames = 0
            return None

        self.silence_frames += 1
        if self.silence_frames < END_SILENCE_FRAMES:
            return None

        end_silence_frames = self.silence_frames
        self.reset_to_listening()
        return (
            "end",
            {
                "audio_time": self.audio_time,
                "silence_frames": end_silence_frames,
                "silence_seconds": end_silence_frames * FRAME_MS / 1_000.0,
            },
        )


def qualify_speech_for_state(state: VadState, vad_speech: bool, frame: bytes) -> bool:
    """Apply a low-level noise gate only while waiting for speech to start."""
    if state is VadState.SPEAKING:
        return vad_speech
    return vad_speech and audioop.rms(frame, SAMPLE_WIDTH) >= START_MIN_RMS


def get_default_source() -> str:
    """Return the current PulseAudio-compatible default input source."""
    try:
        completed = subprocess.run(
            ["pactl", "get-default-source"],
            capture_output=True,
            text=True,
            check=True,
        )
    except FileNotFoundError as exc:
        raise VadTestError("pactl 실행 파일을 찾을 수 없습니다.") from exc
    except subprocess.CalledProcessError as exc:
        detail = exc.stderr.strip() or f"exit={exc.returncode}"
        raise VadTestError(f"기본 마이크 source 조회 실패: {detail}") from exc

    source = completed.stdout.strip()
    if not source:
        raise VadTestError("기본 마이크 source가 비어 있습니다.")
    return source


def start_capture(source: str) -> subprocess.Popen[bytes]:
    """Start one continuous raw PCM stream from the selected source."""
    command = [
        "parec",
        "--device",
        source,
        "--raw",
        "--format=s16le",
        f"--rate={SAMPLE_RATE}",
        f"--channels={CHANNELS}",
    ]
    try:
        return subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
        )
    except FileNotFoundError as exc:
        raise VadTestError("parec 실행 파일을 찾을 수 없습니다.") from exc


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
    frame = b"".join(chunks)
    assert len(frame) == FRAME_BYTES
    return frame


def stop_capture(process: subprocess.Popen[bytes]) -> str:
    """Stop parec and return its diagnostic output without leaving a zombie."""
    if process.poll() is None:
        process.terminate()
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=2)

    if process.stderr is None:
        return ""
    return process.stderr.read().decode("utf-8", errors="replace").strip()


def print_listening() -> None:
    print("\n[LISTENING]", flush=True)
    print("음성을 기다리는 중...", flush=True)


def run_vad_test() -> None:
    """Continuously emit debounced speech state transitions until Ctrl+C."""
    vad = webrtcvad.Vad(VAD_MODE)
    detector = VadStateMachine()
    source = get_default_source()
    process = start_capture(source)

    assert process.stdout is not None
    for frame_index in range(STREAM_WARMUP_FRAMES):
        if read_frame(process.stdout) is None:
            stop_capture(process)
            raise VadTestError(
                "PulseAudio stream 안정화 중 PCM 입력이 종료됐습니다: "
                f"frame={frame_index}/{STREAM_WARMUP_FRAMES}"
            )

    print(f"[SOURCE]\n{source}", flush=True)
    print_listening()

    try:
        while True:
            frame = read_frame(process.stdout)
            if frame is None:
                return_code = process.poll()
                stderr = stop_capture(process)
                detail = stderr or f"parec exit={return_code}"
                raise VadTestError(f"PulseAudio PCM stream이 종료되었습니다: {detail}")

            try:
                is_speech = vad.is_speech(frame, SAMPLE_RATE)
            except Exception as exc:
                raise VadTestError(
                    f"VAD frame 처리 실패: {type(exc).__name__}: {exc}"
                ) from exc

            qualified_speech = qualify_speech_for_state(
                detector.state, is_speech, frame
            )
            transition = detector.process_frame(qualified_speech)
            if transition is None:
                continue

            event, details = transition
            if event == "start":
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
                print_listening()
    finally:
        stop_capture(process)


def main() -> int:
    try:
        run_vad_test()
    except KeyboardInterrupt:
        print("\nVAD test 종료", flush=True)
        return 0
    except VadTestError as exc:
        print(f"\n[ERROR]\n{exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
