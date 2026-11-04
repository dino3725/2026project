"""VAD -> STT -> LLM pipeline for one-utterance-at-a-time voice commands."""

from __future__ import annotations

import audioop
from collections import deque
from dataclasses import dataclass, field
import json
import math
from pathlib import Path
import threading
import time
from typing import Any, Callable
import urllib.request
import wave

from voice_command_pkg.audio_capture import (
    CHANNELS,
    FRAME_BYTES,
    FRAME_MS,
    get_default_source,
    qualify_speech_for_state,
    read_frame,
    SAMPLE_RATE,
    SAMPLE_WIDTH,
    start_capture,
    stop_capture,
    VadConfig,
    VadState,
    VadStateMachine,
)
from voice_command_pkg.command_classifier import classify_command, ClassifierConfig


# (command, transcript) -> published?
PublishFn = Callable[[dict[str, str], str], bool]
LogFn = Callable[[str], None]

# Failures in these stages mean the microphone itself is unusable.
FATAL_STAGES = frozenset({'녹음 준비', '녹음', '녹음 저장', 'VAD'})


@dataclass(frozen=True)
class PipelineConfig:
    """All tunable values of the voice pipeline."""

    whisper_model: str = 'small'
    whisper_device: str = 'cpu'
    whisper_compute_type: str = 'int8'
    language: str = 'ko'
    recordings_dir: Path = Path('recordings')
    vad: VadConfig = field(default_factory=VadConfig)
    classifier: ClassifierConfig = field(default_factory=ClassifierConfig)


class PipelineError(RuntimeError):
    """An expected integration failure with stage and debug context."""

    def __init__(self, stage: str, message: str, debug: str = '') -> None:
        super().__init__(message)
        self.stage = stage
        self.debug = debug

    def describe(self) -> str:
        text = f'{self.stage}: {self}'
        if self.debug:
            text += f' ({self.debug})'
        return text


class PipelineStopped(Exception):
    """Raised inside the capture loop when shutdown was requested."""


def load_whisper_model(config: PipelineConfig) -> tuple[Any, float]:
    from faster_whisper import WhisperModel

    started = time.perf_counter()
    try:
        model = WhisperModel(
            config.whisper_model,
            device=config.whisper_device,
            compute_type=config.whisper_compute_type,
        )
    except Exception as exc:
        raise PipelineError(
            'STT 모델 로드',
            'Whisper 모델을 로드할 수 없습니다.',
            f'{type(exc).__name__}: {exc}',
        ) from exc
    return model, time.perf_counter() - started


def ensure_ollama_ready(config: ClassifierConfig) -> None:
    try:
        with urllib.request.urlopen(config.tags_url, timeout=5) as response:
            payload = json.load(response)
    except Exception as exc:
        raise PipelineError(
            'LLM 준비',
            'Ollama Local API에 연결할 수 없습니다.',
            f'{type(exc).__name__}: {exc}',
        ) from exc

    installed = {m.get('name') for m in payload.get('models', []) if m.get('name')}
    if config.model not in installed:
        raise PipelineError(
            'LLM 준비',
            f'필요한 Ollama 모델이 등록되어 있지 않습니다: {config.model}',
            f'installed_models={sorted(installed)}',
        )


def capture_utterance(
    config: VadConfig,
    log: LogFn,
    stop_event: threading.Event,
) -> tuple[bytes, dict[str, Any]]:
    """Capture one VAD-delimited utterance from a fresh PulseAudio stream."""
    import webrtcvad

    try:
        source = get_default_source()
        process = start_capture(source)
    except Exception as exc:
        raise PipelineError(
            '녹음 준비',
            'PulseAudio 기본 마이크 stream을 시작할 수 없습니다.',
            f'{type(exc).__name__}: {exc}',
        ) from exc

    assert process.stdout is not None
    try:
        # Consume the measured PulseAudio startup transient before listening.
        for frame_index in range(config.stream_warmup_frames):
            if stop_event.is_set():
                raise PipelineStopped()
            if read_frame(process.stdout) is None:
                raise PipelineError(
                    '녹음 준비',
                    'PulseAudio stream 안정화 중 PCM 입력이 종료됐습니다.',
                    f'warmup_frame={frame_index}/{config.stream_warmup_frames}',
                )

        vad = webrtcvad.Vad(config.vad_mode)
        detector = VadStateMachine(config)
        pre_roll: deque[bytes] = deque(maxlen=config.pre_roll_frames)
        utterance_frames: list[bytes] = []
        listening_started = time.perf_counter()
        voice_started_wall: float | None = None
        voice_start_audio = 0.0

        log('[LISTENING] 말씀해주세요.')
        while True:
            if stop_event.is_set():
                raise PipelineStopped()
            frame = read_frame(process.stdout)
            if frame is None:
                raise PipelineError(
                    '녹음',
                    'PulseAudio PCM stream이 예기치 않게 종료됐습니다.',
                    f'parec exit={process.poll()}',
                )

            if detector.state is VadState.LISTENING:
                pre_roll.append(frame)
            else:
                utterance_frames.append(frame)

            try:
                vad_speech = vad.is_speech(frame, SAMPLE_RATE)
            except Exception as exc:
                raise PipelineError(
                    'VAD',
                    'WebRTC VAD frame 판정에 실패했습니다.',
                    f'{type(exc).__name__}: {exc}',
                ) from exc
            qualified = qualify_speech_for_state(
                detector.state, vad_speech, frame, config.start_min_rms
            )
            transition = detector.process_frame(qualified)

            end_details: dict[str, float] | None = None
            if transition is not None:
                event, details = transition
                if event == 'start':
                    utterance_frames = list(pre_roll)
                    pre_roll.clear()
                    voice_started_wall = time.perf_counter()
                    voice_start_audio = float(details['audio_time'])
                    log(
                        f'[VOICE START] audio={voice_start_audio:.3f}s '
                        f"speech={details['speech_frames']}/{details['window_frames']}"
                    )
                else:
                    end_details = {
                        'audio_time': float(details['audio_time']),
                        'silence_seconds': float(details['silence_seconds']),
                        'max_duration_reached': False,
                    }
                    log(
                        f"[VOICE END] audio={end_details['audio_time']:.3f}s "
                        f"silence={end_details['silence_seconds']:.3f}s"
                    )
            elif (
                detector.state is VadState.SPEAKING
                and len(utterance_frames) >= config.max_utterance_frames
            ):
                end_details = {
                    'audio_time': detector.audio_time,
                    'silence_seconds': 0.0,
                    'max_duration_reached': True,
                }
                detector.reset_to_listening()
                log(
                    f"[VOICE END] audio={end_details['audio_time']:.3f}s "
                    f'limit={config.max_utterance_seconds}s'
                )

            if end_details is not None:
                voice_end_wall = time.perf_counter()
                assert voice_started_wall is not None
                return b''.join(utterance_frames), {
                    'source': source,
                    'listening_seconds': voice_started_wall - listening_started,
                    'voice_start_audio': voice_start_audio,
                    'voice_end_audio': end_details['audio_time'],
                    'vad_end_silence_seconds': end_details['silence_seconds'],
                    'utterance_seconds': len(utterance_frames) * FRAME_MS / 1_000.0,
                    'capture_wall_seconds': voice_end_wall - voice_started_wall,
                    'voice_end_wall': voice_end_wall,
                    'max_duration_reached': end_details['max_duration_reached'],
                }
    finally:
        stop_capture(process)


def next_recording_path(output_dir: Path, command_index: int) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime('%Y%m%d_%H%M%S')
    candidate = output_dir / f'vad_command_{stamp}_{command_index:03d}.wav'
    suffix = 1
    while candidate.exists():
        candidate = output_dir / f'vad_command_{stamp}_{command_index:03d}_{suffix:02d}.wav'
        suffix += 1
    return candidate.resolve()


def save_wav(audio_path: Path, audio_data: bytes) -> None:
    if not audio_data:
        raise PipelineError('녹음 저장', '저장할 발화 PCM 데이터가 없습니다.')
    if len(audio_data) % FRAME_BYTES:
        raise PipelineError(
            '녹음 저장',
            '발화 PCM 데이터가 완전한 VAD frame으로 구성되지 않았습니다.',
            f'bytes={len(audio_data)} frame_bytes={FRAME_BYTES}',
        )
    if audio_path.exists():
        raise PipelineError('녹음 저장', f'기존 파일을 덮어쓰지 않습니다: {audio_path}')

    try:
        with wave.open(str(audio_path), 'wb') as wav_file:
            wav_file.setnchannels(CHANNELS)
            wav_file.setsampwidth(SAMPLE_WIDTH)
            wav_file.setframerate(SAMPLE_RATE)
            wav_file.writeframes(audio_data)
    except (OSError, wave.Error) as exc:
        raise PipelineError(
            '녹음 저장',
            'VAD 발화 WAV를 저장할 수 없습니다.',
            f'{type(exc).__name__}: {exc}',
        ) from exc


def audio_levels(audio_data: bytes) -> dict[str, float | bool]:
    """Return RMS/peak levels of 16-bit PCM in raw units and dBFS."""
    rms = audioop.rms(audio_data, SAMPLE_WIDTH)
    peak = audioop.max(audio_data, SAMPLE_WIDTH)
    full_scale = float(1 << (SAMPLE_WIDTH * 8 - 1))
    return {
        'duration_seconds': len(audio_data) / (SAMPLE_RATE * SAMPLE_WIDTH * CHANNELS),
        'rms': rms,
        'rms_dbfs': 20 * math.log10(rms / full_scale) if rms else float('-inf'),
        'peak': peak,
        'peak_dbfs': 20 * math.log10(peak / full_scale) if peak else float('-inf'),
        'silent': peak == 0,
    }


def transcribe_audio(model: Any, audio_path: Path, language: str) -> tuple[str, float]:
    started = time.perf_counter()
    try:
        segment_iter, _ = model.transcribe(str(audio_path), language=language)
        text = ''.join(segment.text for segment in segment_iter).strip()
    except Exception as exc:
        raise PipelineError(
            'STT',
            'Whisper 전사에 실패했습니다.',
            f'{type(exc).__name__}: {exc}',
        ) from exc
    stt_seconds = time.perf_counter() - started
    if not text:
        raise PipelineError('STT', 'Whisper 전사 결과가 비어 있습니다.')
    return text, stt_seconds


def classify_text(text: str, config: ClassifierConfig) -> tuple[dict[str, str], float]:
    started = time.perf_counter()
    try:
        command = classify_command(text, config)
    except Exception as exc:
        raise PipelineError(
            'LLM',
            'Ollama 연결 또는 LLM JSON 응답 검증에 실패했습니다.',
            f'{type(exc).__name__}: {exc}',
        ) from exc
    return command, time.perf_counter() - started


class VoiceCommandPipeline:
    """Reuse one Whisper model while processing repeated VAD utterances."""

    def __init__(
        self,
        config: PipelineConfig,
        publish: PublishFn | None = None,
        log: LogFn = print,
        stop_event: threading.Event | None = None,
    ) -> None:
        self.config = config
        self.publish = publish
        self.log = log
        self.stop_event = stop_event or threading.Event()
        self.recordings_dir = Path(config.recordings_dir).expanduser().resolve()
        self.recordings_dir.mkdir(parents=True, exist_ok=True)
        self.whisper_model, self.model_load_seconds = load_whisper_model(config)
        ensure_ollama_ready(config.classifier)
        self.log(
            f'[READY] whisper={config.whisper_model} '
            f'load={self.model_load_seconds:.3f}s ollama={config.classifier.model}'
        )

    def run_once(self, command_index: int) -> dict[str, Any]:
        audio_data, capture = capture_utterance(self.config.vad, self.log, self.stop_event)
        audio_path = next_recording_path(self.recordings_dir, command_index)
        save_wav(audio_path, audio_data)
        levels = audio_levels(audio_data)
        self.log(
            f"[AUDIO] {audio_path.name} {levels['duration_seconds']:.3f}s "
            f"rms={levels['rms_dbfs']:.2f}dBFS peak={levels['peak_dbfs']:.2f}dBFS"
        )

        text, stt_seconds = transcribe_audio(
            self.whisper_model, audio_path, self.config.language
        )
        self.log(f'[STT] {text}')

        command, llm_seconds = classify_text(text, self.config.classifier)
        command_done = time.perf_counter()
        self.log('[COMMAND] ' + json.dumps(command, ensure_ascii=False, separators=(',', ':')))

        published = False
        if self.publish is not None:
            published = self.publish(command, text)
            self.log('[ROS] ' + ('published' if published else 'skipped'))

        voice_end_to_command = command_done - float(capture['voice_end_wall'])
        self.log(
            f"[TIME] utterance={capture['utterance_seconds']:.3f}s "
            f"end_silence={capture['vad_end_silence_seconds']:.3f}s "
            f'stt={stt_seconds:.3f}s llm={llm_seconds:.3f}s '
            f'voice_end_to_command={voice_end_to_command:.3f}s'
        )
        return {
            'audio_path': str(audio_path),
            'audio_levels': levels,
            'capture': capture,
            'transcript': text,
            'command': command,
            'published': published,
            'stt_seconds': stt_seconds,
            'llm_seconds': llm_seconds,
            'voice_end_to_command_seconds': voice_end_to_command,
        }

    def run_forever(self) -> None:
        """Process utterances until stop_event is set or the microphone fails."""
        command_index = 1
        while not self.stop_event.is_set():
            try:
                self.run_once(command_index)
            except PipelineStopped:
                return
            except PipelineError as exc:
                if exc.stage in FATAL_STAGES:
                    raise
                self.log('[ERROR] ' + exc.describe())
                continue
            command_index += 1
