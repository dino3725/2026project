"""ROS parameter names/defaults and their mapping to PipelineConfig (no ROS imports)."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from voice_command_pkg.audio_capture import VadConfig
from voice_command_pkg.command_classifier import ClassifierConfig
from voice_command_pkg.pipeline import PipelineConfig


# Defaults reproduce the 2026-11-04 baseline behaviour. config/voice_command.yaml
# must list the same keys; test_parameters.py checks both.
DEFAULT_PARAMETERS: dict[str, Any] = {
    'publish_commands': True,
    'recordings_dir': '~/2026project/recordings',
    'whisper.model': 'small',
    'whisper.device': 'cpu',
    'whisper.compute_type': 'int8',
    'whisper.language': 'ko',
    'vad.mode': 3,
    'vad.start_min_rms': 200,
    'vad.stream_warmup_ms': 1200,
    'vad.start_window_ms': 300,
    'vad.start_speech_ratio': 0.6,
    'vad.end_silence_ms': 900,
    'vad.pre_roll_ms': 450,
    'vad.max_utterance_seconds': 15.0,
    'llm.ollama_url': 'http://127.0.0.1:11434',
    'llm.model': 'qwen3:4b-instruct',
    'llm.timeout_seconds': 120.0,
    'llm.keep_alive': '10m',
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
        recordings_dir=Path(str(merged['recordings_dir'])).expanduser(),
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
            keep_alive=str(merged['llm.keep_alive']),
        ),
    )
