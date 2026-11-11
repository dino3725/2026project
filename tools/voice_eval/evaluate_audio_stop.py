#!/usr/bin/env python3
"""Replay development WAVs through the runtime KWS backend without ROS or a mic."""

import argparse
import hashlib
import json
from pathlib import Path
import time
import wave

from voice_command_pkg.audio_capture import FRAME_BYTES, SAMPLE_RATE
from voice_command_pkg.audio_stop_model import AudioStopConfig, KoreanStopSpotter, MODEL_REVISION


def read_pcm(path):
    """Reject formats that would otherwise silently distort streaming timing."""
    with wave.open(str(path), 'rb') as wav:
        if (wav.getframerate(), wav.getnchannels(), wav.getsampwidth(), wav.getcomptype()) != (
            SAMPLE_RATE, 1, 2, 'NONE'
        ):
            raise ValueError(f'expected 16 kHz mono PCM16 WAV: {path}')
        return wav.readframes(wav.getnframes())


def replay(backend, pcm, tail_seconds=0.6):
    """Measure audio offsets and CPU time, explicitly labeling synthetic tail hits."""
    backend.reset()
    hits, steps = [], []
    padded = pcm + bytes(round(tail_seconds * SAMPLE_RATE) * 2)
    started = time.perf_counter()
    for offset in range(0, len(padded), FRAME_BYTES):
        chunk = padded[offset:offset + FRAME_BYTES]
        before = time.perf_counter()
        words = backend.accept(chunk)
        steps.append(time.perf_counter() - before)
        end = (offset + len(chunk)) / (SAMPLE_RATE * 2)
        for word in words:
            hits.append({'keyword': word, 'audio_end': end, 'in_synthetic_tail': end > (
                len(pcm) / (SAMPLE_RATE * 2)
            )})
    elapsed = time.perf_counter() - started
    return {
        'duration_seconds': len(pcm) / (SAMPLE_RATE * 2), 'tail_seconds': tail_seconds,
        'hits': hits, 'compute_seconds': elapsed, 'max_step_seconds': max(steps, default=0),
    }


def score_events(hits, events):
    """Match one detection per annotated stop window; report extras separately."""
    unmatched = list(range(len(hits)))
    matches = []
    for event in events:
        found = next((i for i in unmatched if (
            event['start'] <= hits[i]['audio_end'] <= event['end']
        )), None)
        matches.append(found)
        if found is not None:
            unmatched.remove(found)
    return {'matched_events': matches, 'extra_hit_indices': unmatched}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument('--manifest', type=Path)
    source.add_argument('--wav', type=Path)
    parser.add_argument('--threshold', type=float, default=AudioStopConfig().threshold)
    parser.add_argument('--score', type=float, default=AudioStopConfig().score)
    parser.add_argument('--model-dir', default=AudioStopConfig().model_dir)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    config = AudioStopConfig(threshold=args.threshold, score=args.score, model_dir=args.model_dir)
    backend = KoreanStopSpotter(config)
    if args.manifest:
        manifest = json.loads(args.manifest.read_text(encoding='utf-8'))
        samples, root = manifest['samples'], args.manifest.parent
    else:
        samples, root = [{'id': args.wav.name, 'file': str(args.wav.resolve())}], Path('.')
    rows = []
    for sample in samples:
        path = root / sample['file']
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if sample.get('sha256', digest) != digest:
            raise ValueError(f'WAV SHA256 mismatch: {path}')
        row = {**sample, 'sha256': digest, **replay(backend, read_pcm(path))}
        if 'events' in sample:
            row.update(score_events(row['hits'], sample['events']))
        rows.append(row)
        print(json.dumps({k: row[k] for k in ('id', 'hits')}, ensure_ascii=False), flush=True)
    # Synthetic silence is a sanity check, never a substitute for room noise.
    silence = replay(backend, bytes(SAMPLE_RATE * 2 * 10))
    result = {
        'development_only': True, 'model_revision': MODEL_REVISION,
        'threshold': config.threshold, 'score': config.score,
        'manifest_sha256': hashlib.sha256(args.manifest.read_bytes()).hexdigest()
        if args.manifest else None,
        'samples': rows, 'synthetic_silence_10s': silence,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(f'results: {args.output}')


if __name__ == '__main__':
    main()
