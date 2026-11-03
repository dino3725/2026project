#!/usr/bin/env python3
"""Continuously detect, transcribe, and classify local voice commands."""

from __future__ import annotations

import argparse
import audioop
import json
import math
import sys
import time
import urllib.request
import wave
from collections import deque
from pathlib import Path
from typing import Any, Protocol

import webrtcvad
from faster_whisper import WhisperModel

from llm_test import MODEL as LLM_MODEL
from llm_test import classify_command
from vad_test import (
    CHANNELS,
    END_SILENCE_MS,
    FRAME_BYTES,
    FRAME_MS,
    SAMPLE_RATE,
    SAMPLE_WIDTH,
    START_MIN_RMS,
    STREAM_WARMUP_FRAMES,
    STREAM_WARMUP_MS,
    VAD_MODE,
    VadState,
    VadStateMachine,
    get_default_source,
    read_frame,
    qualify_speech_for_state,
    start_capture,
    stop_capture,
)


WHISPER_MODEL = "small"
PRE_ROLL_MS = 450
PRE_ROLL_FRAMES = PRE_ROLL_MS // FRAME_MS
MAX_UTTERANCE_SECONDS = 15
MAX_UTTERANCE_FRAMES = MAX_UTTERANCE_SECONDS * 1_000 // FRAME_MS
OLLAMA_TAGS_URL = "http://127.0.0.1:11434/api/tags"
ROS_TOPIC_NAME = "/voice/robot_command"


class CommandPublisher(Protocol):
    def publish_command(self, command: Any, transcript: str) -> bool: ...

    def close(self) -> None: ...


class PipelineError(RuntimeError):
    """An expected integration failure with stage and debug context."""

    def __init__(self, stage: str, message: str, debug: str = "") -> None:
        super().__init__(message)
        self.stage = stage
        self.debug = debug


def report_error(exc: PipelineError) -> None:
    print(f"\n[ERROR]\n{exc.stage}: {exc}", file=sys.stderr)
    if exc.debug:
        print(f"[DEBUG]\n{exc.debug}", file=sys.stderr)


def load_whisper_model() -> tuple[WhisperModel, float]:
    started = time.perf_counter()
    try:
        model = WhisperModel(WHISPER_MODEL, device="cpu", compute_type="int8")
    except Exception as exc:
        raise PipelineError(
            "STT 모델 로드",
            "Whisper 모델을 로드할 수 없습니다.",
            f"{type(exc).__name__}: {exc}",
        ) from exc
    return model, time.perf_counter() - started


def ensure_ollama_ready() -> None:
    try:
        with urllib.request.urlopen(OLLAMA_TAGS_URL, timeout=5) as response:
            payload = json.load(response)
    except Exception as exc:
        raise PipelineError(
            "LLM 준비",
            "Ollama Local API에 연결할 수 없습니다.",
            f"{type(exc).__name__}: {exc}",
        ) from exc

    installed_models = {
        model.get("name") for model in payload.get("models", []) if model.get("name")
    }
    if LLM_MODEL not in installed_models:
        raise PipelineError(
            "LLM 준비",
            f"필요한 Ollama 모델이 등록되어 있지 않습니다: {LLM_MODEL}",
            f"installed_models={sorted(installed_models)}",
        )


def _is_speech(vad: webrtcvad.Vad, frame: bytes) -> bool:
    if len(frame) != FRAME_BYTES:
        raise PipelineError(
            "VAD",
            "WebRTC VAD에 전달할 PCM frame 크기가 올바르지 않습니다.",
            f"actual={len(frame)} expected={FRAME_BYTES}",
        )
    try:
        return vad.is_speech(frame, SAMPLE_RATE)
    except Exception as exc:
        raise PipelineError(
            "VAD",
            "WebRTC VAD frame 판정에 실패했습니다.",
            f"{type(exc).__name__}: {exc}",
        ) from exc


def discard_stream_warmup(process: Any) -> None:
    """Consume the measured PulseAudio startup transient before listening."""
    assert process.stdout is not None
    for frame_index in range(STREAM_WARMUP_FRAMES):
        frame = read_frame(process.stdout)
        if frame is None:
            raise PipelineError(
                "녹음 준비",
                "PulseAudio stream 안정화 중 PCM 입력이 종료됐습니다.",
                f"warmup_frame={frame_index}/{STREAM_WARMUP_FRAMES}",
            )


def capture_utterance() -> tuple[bytes, dict[str, float | bool | str]]:
    """Capture one VAD-delimited utterance from a fresh PulseAudio stream."""
    try:
        source = get_default_source()
        process = start_capture(source)
    except Exception as exc:
        raise PipelineError(
            "녹음 준비",
            "PulseAudio 기본 마이크 stream을 시작할 수 없습니다.",
            f"{type(exc).__name__}: {exc}",
        ) from exc

    try:
        discard_stream_warmup(process)
    except Exception:
        stop_capture(process)
        raise

    vad = webrtcvad.Vad(VAD_MODE)
    detector = VadStateMachine()
    pre_roll: deque[bytes] = deque(maxlen=PRE_ROLL_FRAMES)
    utterance_frames: list[bytes] = []
    listening_started = time.perf_counter()
    voice_started_wall: float | None = None
    voice_start_audio = 0.0

    assert process.stdout is not None
    print("\n[LISTENING]", flush=True)
    print("말씀해주세요.", flush=True)

    try:
        while True:
            frame = read_frame(process.stdout)
            if frame is None:
                return_code = process.poll()
                raise PipelineError(
                    "녹음",
                    "PulseAudio PCM stream이 예기치 않게 종료됐습니다.",
                    f"parec exit={return_code}",
                )

            state_before = detector.state
            if state_before is VadState.LISTENING:
                pre_roll.append(frame)
            else:
                utterance_frames.append(frame)

            vad_speech = _is_speech(vad, frame)
            qualified_speech = qualify_speech_for_state(
                detector.state, vad_speech, frame
            )
            transition = detector.process_frame(qualified_speech)
            if transition is not None:
                event, details = transition
                if event == "start":
                    utterance_frames = list(pre_roll)
                    pre_roll.clear()
                    voice_started_wall = time.perf_counter()
                    voice_start_audio = float(details["audio_time"])
                    print(
                        f"\n[VOICE START] audio={voice_start_audio:.3f}s "
                        f"speech={details['speech_frames']}/{details['window_frames']}",
                        flush=True,
                    )
                else:
                    voice_end_wall = time.perf_counter()
                    audio_end = float(details["audio_time"])
                    print(
                        f"[VOICE END] audio={audio_end:.3f}s "
                        f"silence={details['silence_seconds']:.3f}s",
                        flush=True,
                    )
                    assert voice_started_wall is not None
                    return b"".join(utterance_frames), {
                        "source": source,
                        "listening_seconds": voice_started_wall - listening_started,
                        "voice_start_audio": voice_start_audio,
                        "voice_end_audio": audio_end,
                        "vad_end_silence_seconds": float(details["silence_seconds"]),
                        "utterance_seconds": len(utterance_frames) * FRAME_MS / 1_000.0,
                        "capture_wall_seconds": voice_end_wall - voice_started_wall,
                        "voice_end_wall": voice_end_wall,
                        "max_duration_reached": False,
                    }

            if (
                detector.state is VadState.SPEAKING
                and len(utterance_frames) >= MAX_UTTERANCE_FRAMES
            ):
                voice_end_wall = time.perf_counter()
                audio_end = detector.audio_time
                detector.reset_to_listening()
                print(
                    f"[VOICE END] audio={audio_end:.3f}s "
                    f"limit={MAX_UTTERANCE_SECONDS}s",
                    flush=True,
                )
                assert voice_started_wall is not None
                return b"".join(utterance_frames), {
                    "source": source,
                    "listening_seconds": voice_started_wall - listening_started,
                    "voice_start_audio": voice_start_audio,
                    "voice_end_audio": audio_end,
                    "vad_end_silence_seconds": 0.0,
                    "utterance_seconds": len(utterance_frames) * FRAME_MS / 1_000.0,
                    "capture_wall_seconds": voice_end_wall - voice_started_wall,
                    "voice_end_wall": voice_end_wall,
                    "max_duration_reached": True,
                }
    finally:
        stop_capture(process)


def next_recording_path(output_dir: Path, command_index: int) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    candidate = output_dir / f"vad_command_{stamp}_{command_index:03d}.wav"
    suffix = 1
    while candidate.exists():
        candidate = output_dir / (
            f"vad_command_{stamp}_{command_index:03d}_{suffix:02d}.wav"
        )
        suffix += 1
    return candidate.resolve()


def save_wav(audio_path: Path, audio_data: bytes) -> None:
    if not audio_data:
        raise PipelineError("녹음 저장", "저장할 발화 PCM 데이터가 없습니다.")
    if len(audio_data) % FRAME_BYTES:
        raise PipelineError(
            "녹음 저장",
            "발화 PCM 데이터가 완전한 VAD frame으로 구성되지 않았습니다.",
            f"bytes={len(audio_data)} frame_bytes={FRAME_BYTES}",
        )
    if audio_path.exists():
        raise PipelineError("녹음 저장", f"기존 파일을 덮어쓰지 않습니다: {audio_path}")

    try:
        with wave.open(str(audio_path), "wb") as wav_file:
            wav_file.setnchannels(CHANNELS)
            wav_file.setsampwidth(SAMPLE_WIDTH)
            wav_file.setframerate(SAMPLE_RATE)
            wav_file.writeframes(audio_data)
    except (OSError, wave.Error) as exc:
        raise PipelineError(
            "녹음 저장",
            "VAD 발화 WAV를 저장할 수 없습니다.",
            f"{type(exc).__name__}: {exc}",
        ) from exc


def inspect_audio(audio_path: Path) -> dict[str, float | int | bool]:
    try:
        with wave.open(str(audio_path), "rb") as wav_file:
            channels = wav_file.getnchannels()
            sample_rate = wav_file.getframerate()
            sample_width = wav_file.getsampwidth()
            frame_count = wav_file.getnframes()
            audio_data = wav_file.readframes(frame_count)
    except (OSError, wave.Error) as exc:
        raise PipelineError(
            "오디오 검사",
            "녹음된 WAV 파일을 검사할 수 없습니다.",
            f"{type(exc).__name__}: {exc}",
        ) from exc

    rms = audioop.rms(audio_data, sample_width)
    peak = audioop.max(audio_data, sample_width)
    full_scale = float(1 << (sample_width * 8 - 1))
    rms_dbfs = 20 * math.log10(rms / full_scale) if rms else float("-inf")
    peak_dbfs = 20 * math.log10(peak / full_scale) if peak else float("-inf")
    result: dict[str, float | int | bool] = {
        "duration_seconds": frame_count / sample_rate,
        "sample_rate": sample_rate,
        "channels": channels,
        "rms": rms,
        "rms_dbfs": rms_dbfs,
        "peak": peak,
        "peak_dbfs": peak_dbfs,
        "silent": peak == 0,
    }
    print("\n[AUDIO]")
    print(f"FILE: {audio_path}")
    print(f"DURATION: {result['duration_seconds']:.3f} sec")
    print(f"SAMPLE_RATE: {sample_rate} Hz")
    print(f"CHANNELS: {channels}")
    print(f"RMS: {rms} ({rms_dbfs:.2f} dBFS)")
    print(f"PEAK: {peak} ({peak_dbfs:.2f} dBFS)")
    print(f"SILENT: {result['silent']}")
    return result


def transcribe_audio(model: WhisperModel, audio_path: Path) -> tuple[str, float]:
    if not audio_path.is_file():
        raise PipelineError("STT", f"WAV 파일을 찾을 수 없습니다: {audio_path}")

    started = time.perf_counter()
    try:
        segment_iter, _ = model.transcribe(str(audio_path), language="ko")
        text = "".join(segment.text for segment in segment_iter).strip()
    except Exception as exc:
        raise PipelineError(
            "STT",
            "Whisper 전사에 실패했습니다.",
            f"{type(exc).__name__}: {exc}",
        ) from exc
    stt_seconds = time.perf_counter() - started
    if not text:
        raise PipelineError("STT", "Whisper 전사 결과가 비어 있습니다.")
    return text, stt_seconds


def classify_text(text: str) -> tuple[dict[str, str], float]:
    started = time.perf_counter()
    try:
        command = classify_command(text)
    except Exception as exc:
        raise PipelineError(
            "LLM",
            "Ollama 연결 또는 LLM JSON 응답 검증에 실패했습니다.",
            f"{type(exc).__name__}: {exc}",
        ) from exc
    return command, time.perf_counter() - started


class VoiceCommandPipeline:
    """Reuse one Whisper model while processing repeated VAD utterances."""

    def __init__(
        self,
        output_dir: Path,
        command_publisher: CommandPublisher | None = None,
    ) -> None:
        self.output_dir = output_dir.resolve()
        self.command_publisher = command_publisher
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.whisper_model, self.model_load_seconds = load_whisper_model()
        ensure_ollama_ready()
        print("[READY]")
        print(f"WHISPER_MODEL_LOAD: {self.model_load_seconds:.3f} sec")
        print(f"OLLAMA_MODEL: {LLM_MODEL}")

    def run_once(self, command_index: int) -> dict[str, Any]:
        audio_data, capture = capture_utterance()
        audio_path = next_recording_path(self.output_dir, command_index)
        save_wav(audio_path, audio_data)
        audio_status = inspect_audio(audio_path)

        text, stt_seconds = transcribe_audio(self.whisper_model, audio_path)
        print("\n[STT]")
        print(text)

        command, llm_seconds = classify_text(text)
        command_done = time.perf_counter()
        print("\n[COMMAND]")
        print(json.dumps(command, ensure_ascii=False, separators=(",", ":")))

        ros_published = False
        if self.command_publisher is not None:
            ros_published = self.command_publisher.publish_command(command, text)
            print("\n[ROS]")
            print(f"published {ROS_TOPIC_NAME}" if ros_published else "skipped")

        voice_end_to_command = command_done - float(capture["voice_end_wall"])
        print("\n[TIME]")
        print(f"UTTERANCE_WAV: {capture['utterance_seconds']:.3f} sec")
        print(f"VAD_END_SILENCE: {capture['vad_end_silence_seconds']:.3f} sec")
        print(f"STT: {stt_seconds:.3f} sec")
        print(f"LLM: {llm_seconds:.3f} sec")
        print(f"VOICE_END_TO_COMMAND: {voice_end_to_command:.3f} sec")

        return {
            "audio_path": str(audio_path),
            "audio_status": audio_status,
            "capture": capture,
            "transcript": text,
            "command": command,
            "ros_published": ros_published,
            "stt_seconds": stt_seconds,
            "llm_seconds": llm_seconds,
            "voice_end_to_command_seconds": voice_end_to_command,
        }

    def run_forever(self, max_commands: int | None = None) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        command_index = 1
        while max_commands is None or command_index <= max_commands:
            try:
                result = self.run_once(command_index)
            except PipelineError as exc:
                if exc.stage in {"녹음 준비", "녹음", "녹음 저장", "VAD"}:
                    raise
                report_error(exc)
                print("\n[LISTENING 재시작]", flush=True)
                continue
            results.append(result)
            command_index += 1
        return results


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("recordings"),
        help="Directory for uniquely named VAD utterance WAV files",
    )
    parser.add_argument(
        "--max-commands",
        type=int,
        help="Stop after this many successful commands; default is Ctrl+C",
    )
    parser.add_argument(
        "--ros",
        action="store_true",
        help="Publish validated fetch/stop commands to ROS2 /voice/robot_command",
    )
    args = parser.parse_args()
    if args.max_commands is not None and args.max_commands < 1:
        parser.error("--max-commands must be at least 1")

    command_publisher: CommandPublisher | None = None
    try:
        if args.ros:
            try:
                from robot_command_publisher import RobotCommandPublisher
            except ImportError as exc:
                raise PipelineError(
                    "ROS2 준비",
                    "ROS2 Python 환경을 불러올 수 없습니다. ROS2 setup을 source했는지 확인하세요.",
                    f"{type(exc).__name__}: {exc}",
                ) from exc
            command_publisher = RobotCommandPublisher("voice_command_publisher")
        pipeline = VoiceCommandPipeline(args.output_dir, command_publisher)
        pipeline.run_forever(args.max_commands)
    except KeyboardInterrupt:
        print("\nVoice command test 종료", flush=True)
        return 0
    except PipelineError as exc:
        report_error(exc)
        return 1
    except Exception as exc:
        print("\n[ERROR]\n예상하지 못한 통합 프로그램 오류가 발생했습니다.", file=sys.stderr)
        print(f"[DEBUG]\n{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    finally:
        if command_publisher is not None:
            command_publisher.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
