#!/usr/bin/env python3
"""Replay a WAV at wall-clock pace through real VAD/STT/KWS, with no ROS or mic."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import threading
import time
from types import SimpleNamespace

from evaluate_audio_stop import read_pcm
from voice_command_pkg import pipeline
from voice_command_pkg.audio_capture import FRAME_BYTES, SAMPLE_RATE, VadConfig
from voice_command_pkg.audio_stop_model import AudioStopConfig
from voice_command_pkg.speaker_profile import SpeakerConfig


def join_replay_workers(instance, producers, timeout=10.0):
    """Wait after offline shutdown so native model cleanup cannot race Python exit."""
    workers = list(producers)
    if instance is not None:
        workers.append(instance.capture_worker)
        for name in ('speaker', 'audio_stop_worker', 'audio_stop_recheck', 'continuous_recording'):
            component = getattr(instance, name, None)
            workers.append(getattr(component, 'worker', None))
    workers = list(dict.fromkeys(worker for worker in workers if worker is not None))
    deadline = time.monotonic() + timeout
    for worker in workers:
        worker.join(timeout=max(0.0, deadline - time.monotonic()))
    return [worker.name for worker in workers if worker.is_alive()]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--wav', type=Path, required=True,
                        help='Include trailing silence for STT to finish before EOF shuts down.')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--mode', choices=['off', 'shadow', 'active'], default='active')
    parser.add_argument('--precision', choices=['int8', 'fp32'],
                        default=AudioStopConfig().precision)
    parser.add_argument('--no-utterance-recheck', action='store_true')
    parser.add_argument('--recheck-phase-shifts', action='store_true')
    parser.add_argument('--recheck-gain', type=float, default=1.0)
    parser.add_argument('--post-warmup', action='store_true',
                        help='Input is diagnostic continuous.wav; do not discard startup again.')
    parser.add_argument('--continuous-recording-seconds', type=float, default=0.0,
                        help='Exercise optional diagnostic storage during offline replay.')
    parser.add_argument('--speaker-profile', type=Path,
                        help='Enable real speaker inference alongside STT; still no ROS/LLM.')
    parser.add_argument('--allow-development-profile', action='store_true')
    args = parser.parse_args()
    pcm = read_pcm(args.wav)
    logs, commands, producers, producer_errors = [], [], [], []

    def log(line):
        logs.append({'wall': time.perf_counter(), 'message': line})
        if any(tag in line for tag in ['[AUDIO STOP', '[STT]', '[COMMAND]', '[DROPPED]']):
            print(line, flush=True)

    def start_capture(source):
        reader, writer = os.pipe()
        stream = os.fdopen(reader, 'rb', buffering=0)

        def feed():
            origin = time.perf_counter()
            try:
                for offset in range(0, len(pcm), FRAME_BYTES):
                    target = origin + (offset + FRAME_BYTES) / (SAMPLE_RATE * 2)
                    time.sleep(max(0, target - time.perf_counter()))
                    frame = pcm[offset:offset + FRAME_BYTES].ljust(FRAME_BYTES, b'\0')
                    os.write(writer, frame)
            except OSError as exc:
                producer_errors.append(str(exc))
            finally:
                os.close(writer)

        producer = threading.Thread(target=feed, daemon=True)
        producers.append(producer)
        producer.start()
        return SimpleNamespace(stdout=stream, poll=lambda: 0)

    def record_command(command, text, capture):
        # A successful local callback exercises deduplication; no ROS publisher
        # is constructed, and this is not evidence of robot receipt or motion.
        commands.append({'command': command, 'text': text, 'capture': capture,
                         'wall': time.perf_counter()})
        return True

    replacements = {
        'get_default_source': lambda: 'recorded PCM pipe (no microphone)',
        'start_capture': start_capture,
        'stop_capture': lambda process: process.stdout.close() or '',
        'LlmRuntime': lambda *args: SimpleNamespace(
            drop_reason=lambda start: 'offline_no_llm', close=lambda: None,
        ),
    }
    originals = {key: getattr(pipeline, key) for key in replacements}
    instance, end_error, pending_workers = None, '', []
    try:
        for key, value in replacements.items():
            setattr(pipeline, key, value)
        instance = pipeline.VoiceCommandPipeline(
            pipeline.PipelineConfig(
                continuous_recording_seconds=args.continuous_recording_seconds,
                recordings_dir=args.output.parent / 'capture',
                vad=VadConfig(stream_warmup_ms=0 if args.post_warmup else 1200),
                audio_stop=AudioStopConfig(
                    mode=args.mode, precision=args.precision,
                    utterance_recheck=not args.no_utterance_recheck,
                    recheck_phase_shifts=args.recheck_phase_shifts,
                    recheck_gain=args.recheck_gain,
                ), speaker=SpeakerConfig(
                    mode='enforce' if args.speaker_profile else 'off',
                    profile_path=str(args.speaker_profile) if args.speaker_profile else (
                        SpeakerConfig().profile_path
                    ),
                    allow_development_profile=args.allow_development_profile,
                ),
            ), record_command, log
        )
        if args.speaker_profile:
            deadline = time.perf_counter() + 60
            while not instance.speaker.ready and time.perf_counter() < deadline:
                if not instance.speaker.worker.is_alive():
                    raise RuntimeError(f'speaker initialization failed: {instance.speaker.error}')
                time.sleep(0.05)
            if not instance.speaker.ready:
                raise RuntimeError('speaker initialization timed out')
        try:
            instance.run_forever()
        except pipeline.PipelineError as exc:
            # EOF deliberately follows the normal live input-failure shutdown.
            if exc.stage != '녹음' or '예기치 않게 종료' not in str(exc):
                raise
            end_error = exc.describe()
    finally:
        if instance is not None:
            instance.request_stop()
        pending_workers = join_replay_workers(instance, producers)
        for key, value in originals.items():
            setattr(pipeline, key, value)
    result = {
        'no_ros_no_microphone': True, 'llm_disabled': True, 'mode': args.mode,
        'precision': args.precision,
        'utterance_recheck': not args.no_utterance_recheck,
        'recheck_phase_shifts': args.recheck_phase_shifts,
        'recheck_gain': args.recheck_gain,
        'post_warmup_input': args.post_warmup,
        'continuous_recording_metadata': str(instance.continuous_recording.result_path)
        if instance.continuous_recording else None,
        'speaker_profile_sha256': hashlib.sha256(args.speaker_profile.read_bytes()).hexdigest()
        if args.speaker_profile else None,
        'wav_sha256': hashlib.sha256(args.wav.read_bytes()).hexdigest(),
        'callback_commands': commands, 'logs': logs, 'expected_eof_error': end_error,
        'producer_errors': producer_errors,
        'shutdown_workers_alive': pending_workers,
        'dropped_frames': instance.audio_stop_worker.dropped_frames
        if instance.audio_stop_worker else None,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n',
                           encoding='utf-8')
    print(f'local callbacks={len(commands)}; no ROS publications; results={args.output}')
    if pending_workers:
        raise SystemExit('Replay shutdown incomplete: ' + ', '.join(pending_workers))


if __name__ == '__main__':
    main()
