#!/usr/bin/env python3
"""Compare file and raw PCM STT on explicit development WAVs; never publish ROS."""

import argparse
import hashlib
import importlib.metadata
import json
from pathlib import Path
import statistics
import time
import wave

from faster_whisper import WhisperModel
from faster_whisper.audio import decode_audio
import numpy as np

from voice_command_pkg.audio_samples import pcm_to_float32
from voice_command_pkg.pipeline import transcribe_audio


def read_pcm(path):
    """Read explicit development audio without changing its sample format."""
    with wave.open(str(path), 'rb') as wav:
        if (wav.getframerate(), wav.getnchannels(), wav.getsampwidth(), wav.getcomptype()) != (
            16000, 1, 2, 'NONE'
        ):
            raise ValueError(f'{path}: expected 16 kHz mono PCM16 WAV')
        return wav.readframes(wav.getnframes())


def main():
    """Run paired offline transcriptions and write an auditable local report."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('files', type=Path, nargs='+', help='Development audio, not held-out data')
    parser.add_argument(
        '--output', type=Path, required=True, help='Local JSON report, e.g. log/...'
    )
    parser.add_argument('--rounds', type=int, default=2)
    args = parser.parse_args()
    if args.rounds < 1:
        parser.error('--rounds must be positive')
    if args.output.exists():
        parser.error('--output already exists; choose a new path')
    audio = [(path, read_pcm(path)) for path in args.files]
    started = time.perf_counter()
    model = WhisperModel('small', device='cpu', compute_type='int8', local_files_only=True)
    load_seconds = time.perf_counter() - started
    # Warm once outside the paired measurements. Alternate ordering per pair.
    transcribe_audio(model, audio[0][1], 'ko')
    results = []
    for round_index in range(args.rounds):
        for index, (path, pcm) in enumerate(audio):
            decoded = decode_audio(str(path))
            direct = pcm_to_float32(pcm)
            arrays_equal = bool(np.array_equal(decoded, direct))
            if not arrays_equal:
                raise ValueError(f'File decoder and PCM conversion differ: {path}')
            order = ('file', 'memory') if (index + round_index) % 2 == 0 else ('memory', 'file')
            row = {
                'filename': path.name, 'sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
                'round': round_index + 1, 'order': list(order),
                'duration_seconds': len(pcm) / 32000, 'samples_equal': arrays_equal,
            }
            for mode in order:
                text, seconds = transcribe_audio(model, path if mode == 'file' else pcm, 'ko')
                row[mode] = {'transcript': text, 'seconds': seconds}
            row['transcripts_equal'] = row['file']['transcript'] == row['memory']['transcript']
            results.append(row)
            print(json.dumps(row, ensure_ascii=False), flush=True)
    report = {
        'model': 'small', 'device': 'cpu', 'compute_type': 'int8', 'language': 'ko',
        'faster_whisper': importlib.metadata.version('faster-whisper'),
        'numpy': np.__version__, 'load_seconds': load_seconds,
        'file_count': len(audio), 'paired_runs': len(results),
        'all_samples_equal': all(row['samples_equal'] for row in results),
        'all_transcripts_equal': all(row['transcripts_equal'] for row in results),
        'median_file_seconds': statistics.median(row['file']['seconds'] for row in results),
        'median_memory_seconds': statistics.median(row['memory']['seconds'] for row in results),
        'results': results,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open('x', encoding='utf-8') as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2)
        handle.write('\n')
    print(json.dumps({key: value for key, value in report.items() if key != 'results'}, indent=2))


if __name__ == '__main__':
    main()
