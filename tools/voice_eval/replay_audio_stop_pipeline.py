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
from voice_command_pkg.audio_capture import FRAME_BYTES, SAMPLE_RATE
from voice_command_pkg.audio_stop_model import AudioStopConfig


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--wav', type=Path, required=True,
                        help='Include trailing silence for STT to finish before EOF shuts down.')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--mode', choices=['off', 'shadow', 'active'], default='active')
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
    instance, end_error = None, ''
    try:
        for key, value in replacements.items():
            setattr(pipeline, key, value)
        instance = pipeline.VoiceCommandPipeline(
            pipeline.PipelineConfig(audio_stop=AudioStopConfig(mode=args.mode)), record_command, log
        )
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
        for producer in producers:
            producer.join(timeout=1)
        for key, value in originals.items():
            setattr(pipeline, key, value)
    result = {
        'no_ros_no_microphone': True, 'llm_disabled': True, 'mode': args.mode,
        'wav_sha256': hashlib.sha256(args.wav.read_bytes()).hexdigest(),
        'callback_commands': commands, 'logs': logs, 'expected_eof_error': end_error,
        'producer_errors': producer_errors,
        'dropped_frames': instance.audio_stop_worker.dropped_frames
        if instance.audio_stop_worker else None,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(f'local callbacks={len(commands)}; no ROS publications; results={args.output}')


if __name__ == '__main__':
    main()
