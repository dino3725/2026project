"""Continuous VAD capture with bounded, sequential STT/LLM command processing."""

from __future__ import annotations

import audioop
from collections import deque
from dataclasses import dataclass, field
import json
import math
from pathlib import Path
import threading
import time
from typing import Any, Callable, Iterator
import urllib.request
import wave

from voice_command_pkg.audio_capture import (
    AudioCaptureError,
    CHANNELS,
    FRAME_BYTES,
    FRAME_MS,
    get_default_source,
    qualify_speech_for_state,
    read_frame_interruptible,
    SAMPLE_RATE,
    SAMPLE_WIDTH,
    start_capture,
    stop_capture,
    VadConfig,
    VadState,
    VadStateMachine,
)
from voice_command_pkg.audio_samples import pcm_to_float32
from voice_command_pkg.command_classifier import classify_command, ClassifierConfig
from voice_command_pkg.llm_runtime import LlmRuntime, LlmStopped, LlmUnavailable
from voice_command_pkg.transcript_rules import find_stop_keyword, stop_command
from voice_command_pkg.utterance_buffer import Utterance, UtteranceBuffer


# (command, transcript, capture timing) -> published?
PublishFn = Callable[[dict[str, str], str, dict[str, Any]], bool]
LogFn = Callable[[str], None]

# Failures in these stages mean the microphone itself is unusable.
FATAL_STAGES = frozenset({'녹음 준비', '녹음', 'VAD'})


@dataclass(frozen=True)
class PipelineConfig:
    """All tunable values of the voice pipeline."""

    whisper_model: str = 'small'
    whisper_device: str = 'cpu'
    whisper_compute_type: str = 'int8'
    language: str = 'ko'
    recordings_dir: Path = Path('recordings')
    save_recordings: bool = False
    vad: VadConfig = field(default_factory=VadConfig)
    classifier: ClassifierConfig = field(default_factory=ClassifierConfig)
    queue_capacity: int = 2
    max_fetch_age_seconds: float = 10.0
    llm_warmup_timeout_seconds: float = 120.0
    llm_retry_seconds: float = 5.0
    llm_preparing_message: str = '명령 처리 준비 중이에요. 준비 완료 후 다시 말씀해 주세요.'
    llm_ready_message: str = '명령 처리 준비가 완료됐어요. 말씀해 주세요.'
    llm_failure_message: str = '명령을 처리하지 못했어요. 준비 완료 후 다시 말씀해 주세요.'

    def __post_init__(self) -> None:
        if not isinstance(self.save_recordings, bool):
            raise ValueError('save_recordings must be a boolean')
        if self.queue_capacity < 1:
            raise ValueError('queue_capacity must be positive')
        if not math.isfinite(self.max_fetch_age_seconds) or self.max_fetch_age_seconds <= 0:
            raise ValueError('max_fetch_age_seconds must be finite and positive')
        for name in ('llm_warmup_timeout_seconds', 'llm_retry_seconds'):
            value = getattr(self, name)
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f'{name} must be finite and positive')


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


def capture_utterances(
    config: VadConfig,
    log: LogFn,
    stop_event: threading.Event,
) -> Iterator[tuple[bytes, dict[str, Any]]]:
    """Yield utterances from one stream; warm up only once per session."""
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
            if read_frame_interruptible(process.stdout, stop_event) is None:
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
        utterance_start_wall = 0.0
        voice_start_audio = 0.0

        log('[LISTENING] 말씀해주세요.')
        while True:
            if stop_event.is_set():
                raise PipelineStopped()
            frame = read_frame_interruptible(process.stdout, stop_event)
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
                    utterance_start_wall = (
                        voice_started_wall - len(utterance_frames) * FRAME_MS / 1_000.0
                    )
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
                yield b''.join(utterance_frames), {
                    'source': source,
                    'listening_seconds': voice_started_wall - listening_started,
                    'voice_start_audio': voice_start_audio,
                    'voice_start_wall': voice_started_wall,
                    'utterance_start_wall': utterance_start_wall,
                    'voice_end_audio': end_details['audio_time'],
                    'vad_end_silence_seconds': end_details['silence_seconds'],
                    'utterance_seconds': len(utterance_frames) * FRAME_MS / 1_000.0,
                    'capture_wall_seconds': voice_end_wall - voice_started_wall,
                    'voice_end_wall': voice_end_wall,
                    'max_duration_reached': end_details['max_duration_reached'],
                }
                utterance_frames.clear()
                pre_roll.clear()
                listening_started = time.perf_counter()
                log('[LISTENING] 말씀해주세요.')
    except InterruptedError as exc:
        raise PipelineStopped() from exc
    except (AudioCaptureError, OSError) as exc:
        raise PipelineError('녹음', str(exc)) from exc
    finally:
        stop_capture(process)


def capture_utterance(
    config: VadConfig,
    log: LogFn,
    stop_event: threading.Event,
) -> tuple[bytes, dict[str, Any]]:
    """Capture a single trial for evaluation tools, closing its stream."""
    stream = capture_utterances(config, log, stop_event)
    try:
        return next(stream)
    finally:
        stream.close()


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


def transcribe_audio(model: Any, audio: Path | bytes, language: str) -> tuple[str, float]:
    """Transcribe raw capture PCM, or a file path for existing evaluation tools."""
    started = time.perf_counter()
    try:
        source = pcm_to_float32(audio) if isinstance(audio, bytes) else str(audio)
        segment_iter, _ = model.transcribe(source, language=language)
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


def interpret_text(
    text: str,
    config: ClassifierConfig,
) -> tuple[dict[str, str], float, str]:
    """
    Decide the command for a transcript: stop rule first, LLM only otherwise.

    Returns (command, llm_seconds, decided_by) where decided_by is
    'rule:<keyword>' or 'llm'. Stop never waits for or depends on the LLM.
    """
    keyword = find_stop_keyword(text)
    if keyword is not None:
        return stop_command(), 0.0, f'rule:{keyword}'
    command, llm_seconds = classify_text(text, config)
    return command, llm_seconds, 'llm'


class VoiceCommandPipeline:
    """Record continuously while one consumer reuses the Whisper model."""

    def __init__(
        self,
        config: PipelineConfig,
        publish: PublishFn | None = None,
        log: LogFn = print,
        stop_event: threading.Event | None = None,
        fetch_gate: Callable[[float], str] | None = None,
    ) -> None:
        self.config = config
        self.publish = publish
        self.fetch_gate = fetch_gate
        self.log = log
        self.stop_event = stop_event or threading.Event()
        self._publish_lock = threading.RLock()
        self._fetch_not_before = float('-inf')
        self._capture_error: Exception | None = None
        self.buffer = UtteranceBuffer(config.queue_capacity)
        self.capture_worker: threading.Thread | None = None
        self._ensure_running()
        # Saving is optional: do not touch the filesystem during initialization.
        self.recordings_dir = Path(config.recordings_dir).expanduser()
        self.whisper_model, self.model_load_seconds = load_whisper_model(config)
        self._ensure_running()
        self.log(
            f'[STT READY] whisper={config.whisper_model} '
            f'load={self.model_load_seconds:.3f}s'
        )
        self.llm = LlmRuntime(config, log, self.stop_event)

    def run_once(self, command_index: int) -> dict[str, Any]:
        audio_data, capture = capture_utterance(self.config.vad, self.log, self.stop_event)
        return self.process_utterance(Utterance(command_index, audio_data, capture))

    def _ensure_running(self) -> None:
        if self.stop_event.is_set():
            raise PipelineStopped()

    def request_stop(self) -> None:
        """Stop capture/processing and serialize shutdown with publication."""
        with self._publish_lock:
            self.stop_event.set()
        self.buffer.close()
        self.llm.close()

    def _fetch_drop_reason(self, capture: dict[str, Any]) -> str:
        start = float(capture.get('utterance_start_wall', capture['voice_start_wall']))
        if start <= self._fetch_not_before:
            return 'before_stop'
        age = time.perf_counter() - float(capture['voice_end_wall'])
        if age > self.config.max_fetch_age_seconds:
            return 'expired'
        return ''

    def process_utterance(self, utterance: Utterance) -> dict[str, Any]:
        """Process one buffered utterance; only fresh, active commands may publish."""
        self._ensure_running()
        audio_data, capture = utterance.audio, utterance.capture
        queue_wait_seconds = time.perf_counter() - float(capture['voice_end_wall'])
        levels = audio_levels(audio_data)
        audio_path = None
        recording_error = ''
        if self.config.save_recordings:
            try:
                candidate = next_recording_path(self.recordings_dir, utterance.sequence)
                save_wav(candidate, audio_data)
                audio_path = candidate
            except (PipelineError, OSError, ValueError) as exc:
                recording_error = exc.describe() if isinstance(exc, PipelineError) else str(exc)
                self.log(f'[RECORDING ERROR] sequence={utterance.sequence} {recording_error}')
        recording = str(audio_path) if audio_path is not None else (
            'failed' if recording_error else 'disabled'
        )
        self.log(
            f'[AUDIO] sequence={utterance.sequence} recording={recording} '
            f"{levels['duration_seconds']:.3f}s "
            f"rms={levels['rms_dbfs']:.2f}dBFS peak={levels['peak_dbfs']:.2f}dBFS"
        )

        self._ensure_running()
        text, stt_seconds = transcribe_audio(
            self.whisper_model, audio_data, self.config.language
        )
        self._ensure_running()
        self.log(f'[STT] {text}')

        keyword = find_stop_keyword(text)
        dropped_reason = ''
        llm_metrics = {}
        if keyword is not None:
            command, llm_seconds, decided_by = stop_command(), 0.0, f'rule:{keyword}'
            # Invalidate fetch utterances that began before stop was recognized,
            # including an utterance still being recorded. Keep checking their
            # transcripts for stop; age/invalidation never filters known stop.
            self._fetch_not_before = time.perf_counter()
        else:
            utterance_start = float(capture.get(
                'utterance_start_wall', capture['voice_start_wall']
            ))
            dropped_reason = (
                self._fetch_drop_reason(capture)
                or (self.fetch_gate(utterance_start) if self.fetch_gate is not None else '')
                or self.llm.drop_reason(utterance_start)
            )
            command, llm_seconds, decided_by = (
                {'action': 'unknown', 'object': ''}, 0.0, 'discard'
            )
            if not dropped_reason:
                started = time.perf_counter()
                try:
                    command, llm_metrics = self.llm.classify(
                        text, utterance_start,
                        float(capture['voice_end_wall']) + self.config.max_fetch_age_seconds,
                    )
                    decided_by = 'llm'
                except LlmUnavailable as exc:
                    dropped_reason = exc.reason
                except LlmStopped as exc:
                    raise PipelineStopped() from exc
                llm_seconds = time.perf_counter() - started
            if dropped_reason.startswith('llm_'):
                message = self.config.llm_failure_message if dropped_reason in (
                    'llm_timeout', 'llm_error'
                ) else self.config.llm_preparing_message
                self.log('[GUIDANCE] ' + message)
        self._ensure_running()
        command_done = time.perf_counter()
        self.log(
            '[COMMAND] ' + json.dumps(command, ensure_ascii=False, separators=(',', ':'))
            + f' by={decided_by}'
        )

        published = False
        with self._publish_lock:
            self._ensure_running()
            if command['action'] == 'fetch':
                dropped_reason = self._fetch_drop_reason(capture)
                if not dropped_reason and self.fetch_gate is not None:
                    dropped_reason = self.fetch_gate(utterance_start)
            if self.publish is not None and not dropped_reason:
                published = self.publish(command, text, capture)
        if dropped_reason:
            self.log(f'[DROPPED] sequence={utterance.sequence} reason={dropped_reason}')
        self.log('[ROS] ' + ('published' if published else 'skipped'))
        if llm_metrics:
            self.log('[LLM TIME] ' + ' '.join(
                f'{key}={value:.3f}s' for key, value in llm_metrics.items()
            ))

        voice_end_to_command = command_done - float(capture['voice_end_wall'])
        self.log(
            f"[TIME] utterance={capture['utterance_seconds']:.3f}s "
            f"end_silence={capture['vad_end_silence_seconds']:.3f}s "
            f'stt={stt_seconds:.3f}s llm={llm_seconds:.3f}s '
            f'queue_wait={queue_wait_seconds:.3f}s '
            f'voice_end_to_command={voice_end_to_command:.3f}s'
        )
        return {
            'sequence': utterance.sequence,
            'queue_wait_seconds': queue_wait_seconds,
            'dropped_reason': dropped_reason,
            'audio_path': str(audio_path) if audio_path is not None else None,
            'recording_saved': audio_path is not None,
            'recording_error': recording_error,
            'audio_levels': levels,
            'capture': capture,
            'transcript': text,
            'command': command,
            'decided_by': decided_by,
            'published': published,
            'stt_seconds': stt_seconds,
            'llm_seconds': llm_seconds,
            'llm_metrics': llm_metrics,
            'voice_end_to_command_seconds': voice_end_to_command,
        }

    def _capture_loop(self) -> None:
        try:
            for sequence, (audio, capture) in enumerate(
                capture_utterances(self.config.vad, self.log, self.stop_event), start=1
            ):
                if self.stop_event.is_set():
                    break
                dropped = self.buffer.put(Utterance(sequence, audio, capture))
                if dropped is not None:
                    self.log(f'[DROPPED] sequence={dropped.sequence} reason=queue_full')
        except PipelineStopped:
            pass
        except Exception as exc:
            self._capture_error = exc
        finally:
            self.request_stop()

    def run_forever(self) -> None:
        """Run the capture producer and sequential consumer until shutdown."""
        self._ensure_running()
        self.capture_worker = threading.Thread(
            target=self._capture_loop, name='voice_capture', daemon=True
        )
        self.capture_worker.start()
        try:
            while not self.stop_event.is_set():
                utterance = self.buffer.get()
                if utterance is None:
                    continue
                try:
                    self.process_utterance(utterance)
                except PipelineStopped:
                    break
                except PipelineError as exc:
                    if exc.stage in FATAL_STAGES:
                        raise
                    self.log('[ERROR] ' + exc.describe())
        finally:
            self.request_stop()
            self.capture_worker.join()
        if self._capture_error is not None:
            raise self._capture_error
