"""ROS parameter names/defaults and their mapping to PipelineConfig (no ROS imports)."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from voice_command_pkg.audio_capture import VadConfig
from voice_command_pkg.command_classifier import ClassifierConfig
from voice_command_pkg.pipeline import PipelineConfig
from voice_command_pkg.robot_feedback import DEFAULT_MESSAGES
from voice_command_pkg.stt_config import DEFAULT_RUNTIME_STT, SttConfig


# Model stays at baseline; STT and VAD start ratio use validated development settings.
# config/voice_command.yaml must list the same keys; tests check both.
DEFAULT_PARAMETERS: dict[str, Any] = {
    'publish_commands': True,
    'recordings_dir': '~/2026project/recordings',
    'save_recordings': False,
    'capture.queue_capacity': 2,
    'capture.max_fetch_age_seconds': 10.0,
    'whisper.model': 'small',
    'whisper.device': 'cpu',
    'whisper.compute_type': 'int8',
    'whisper.language': 'ko',
    'whisper.beam_size': 5,
    'whisper.initial_prompt': '',
    'whisper.hotwords': '',
    'whisper.temperatures': list(DEFAULT_RUNTIME_STT.temperatures),
    'vad.mode': 3,
    'vad.start_min_rms': 200,
    'vad.stream_warmup_ms': 1200,
    'vad.start_window_ms': 300,
    'vad.start_speech_ratio': 0.4,
    'vad.end_silence_ms': 900,
    'vad.pre_roll_ms': 450,
    'vad.max_utterance_seconds': 15.0,
    'llm.ollama_url': 'http://127.0.0.1:11434',
    'llm.model': 'qwen3:4b-instruct',
    'llm.timeout_seconds': 10.0,
    'llm.keep_alive': -1,
    'llm.warmup_timeout_seconds': 120.0,
    'llm.retry_seconds': 5.0,
    'messages.llm_preparing': '명령 처리 준비 중이에요. 준비 완료 후 다시 말씀해 주세요.',
    'messages.llm_ready': '명령 처리 준비가 완료됐어요. 말씀해 주세요.',
    'messages.llm_failure': '명령을 처리하지 못했어요. 준비 완료 후 다시 말씀해 주세요.',
    'robot.response_timeout_seconds': 5.0,
    'robot.connection_poll_seconds': 0.5,
    **{f'messages.{key}': value for key, value in DEFAULT_MESSAGES.items()},
}


def build_pipeline_config(values: Mapping[str, Any]) -> PipelineConfig:
    """Build PipelineConfig from a flat mapping of parameter name -> value."""
    unknown = set(values) - set(DEFAULT_PARAMETERS)
    if unknown:
        raise ValueError(f'unknown voice parameters: {sorted(unknown)}')
    merged = {**DEFAULT_PARAMETERS, **values}
    return PipelineConfig(
        whisper_model=str(merged['whisper.model']),
        whisper_device=str(merged['whisper.device']),
        whisper_compute_type=str(merged['whisper.compute_type']),
        language=str(merged['whisper.language']),
        stt=SttConfig(
            beam_size=merged['whisper.beam_size'],
            initial_prompt=merged['whisper.initial_prompt'],
            hotwords=merged['whisper.hotwords'],
            temperatures=merged['whisper.temperatures'],
        ),
        recordings_dir=Path(str(merged['recordings_dir'])).expanduser(),
        save_recordings=merged['save_recordings'],
        queue_capacity=int(merged['capture.queue_capacity']),
        max_fetch_age_seconds=float(merged['capture.max_fetch_age_seconds']),
        llm_warmup_timeout_seconds=float(merged['llm.warmup_timeout_seconds']),
        llm_retry_seconds=float(merged['llm.retry_seconds']),
        llm_preparing_message=str(merged['messages.llm_preparing']),
        llm_ready_message=str(merged['messages.llm_ready']),
        llm_failure_message=str(merged['messages.llm_failure']),
        vad=VadConfig(
            vad_mode=int(merged['vad.mode']),
            start_min_rms=int(merged['vad.start_min_rms']),
            stream_warmup_ms=int(merged['vad.stream_warmup_ms']),
            start_window_ms=int(merged['vad.start_window_ms']),
            start_speech_ratio=float(merged['vad.start_speech_ratio']),
            end_silence_ms=int(merged['vad.end_silence_ms']),
            pre_roll_ms=int(merged['vad.pre_roll_ms']),
            max_utterance_seconds=float(merged['vad.max_utterance_seconds']),
        ),
        classifier=ClassifierConfig(
            ollama_url=str(merged['llm.ollama_url']),
            model=str(merged['llm.model']),
            timeout_seconds=float(merged['llm.timeout_seconds']),
            keep_alive=merged['llm.keep_alive'],
        ),
    )
