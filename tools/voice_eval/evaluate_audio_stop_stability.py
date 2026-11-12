#!/usr/bin/env python3
"""Compare model precision and input phase on development audio, without ROS."""

import argparse
from dataclasses import asdict
import hashlib
import json
from pathlib import Path

import numpy as np

from evaluate_audio_stop import read_pcm, replay, score_events
from voice_command_pkg.audio_stop_model import AudioStopConfig, KoreanStopSpotter, MODEL_REVISION


def phase_replay(backend, pcm, prefix_ms, trim_ms=0):
    """Keep original sample coordinates after explicit startup trim and silence prefix."""
    if type(prefix_ms) is not int or type(trim_ms) is not int or min(prefix_ms, trim_ms) < 0:
        raise ValueError('prefix_ms and trim_ms must be nonnegative integers')
    if trim_ms * 32 >= len(pcm):
        raise ValueError('startup trim would remove the whole sample')
    prefix = bytes(prefix_ms * 32)
    result = replay(backend, prefix + pcm[trim_ms * 32:])
    for hit in result['hits']:
        end = hit['audio_end']
        hit['in_synthetic_prefix'] = end <= prefix_ms / 1000
        hit['input_audio_end'] = end
        hit['audio_end'] = end - prefix_ms / 1000 + trim_ms / 1000
    return {**result, 'prefix_ms': prefix_ms, 'trim_ms': trim_ms,
            'original_duration_seconds': len(pcm) / 32000}


def classify_trial(row):
    """Separate misses, extra detections and out-of-vocabulary stop phrases."""
    if row['category'] == 'continuous_stop':
        scored = score_events(row['hits'], row['events'])
        found = sum(index is not None for index in scored['matched_events'])
        return {**scored, 'expected_stops': len(row['events']), 'detected_stops': found,
                'extra_hits': len(scored['extra_hit_indices'])}
    if row['category'] == 'target_stop':
        real = [h for h in row['hits'] if not h['in_synthetic_prefix']]
        return {'expected_stops': 1, 'detected_stops': int(bool(real)),
                'extra_hits': len(row['hits']) - int(bool(real))}
    if row['category'] == 'negative':
        return {'expected_stops': 0, 'detected_stops': 0, 'extra_hits': len(row['hits'])}
    if row['category'] == 'fallback_stop':
        return {'expected_stops': 0, 'detected_stops': 0, 'extra_hits': 0}
    raise ValueError(f"unknown development category: {row['category']}")


def summarize(rows):
    """Count correlated replay trials separately from distinct recordings."""
    target = [r for r in rows if r['category'] == 'target_stop']
    negative = [r for r in rows if r['category'] == 'negative']
    continuous = [r for r in rows if r['category'] == 'continuous_stop']
    ids = sorted({r['id'] for r in target})
    stable = [key for key in ids if all(r['detected_stops'] == 1 for r in target if r['id'] == key)]
    return {
        'target_files': len(ids), 'target_files_detected_at_every_phase': len(stable),
        'target_trials': len(target), 'target_trials_detected': sum(r['detected_stops'] for r in target),
        'negative_files': len({r['id'] for r in negative}), 'negative_trials': len(negative),
        'negative_trials_with_hits': sum(bool(r['hits']) for r in negative),
        'extra_hits_in_target_trials': sum(r['extra_hits'] for r in target),
        'continuous': [{key: r[key] for key in (
            'id', 'prefix_ms', 'trim_ms', 'expected_stops', 'detected_stops', 'extra_hits'
        )} for r in continuous],
        'compute_seconds': sum(r['compute_seconds'] for r in rows),
        'max_step_seconds': max((r['max_step_seconds'] for r in rows), default=0),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--precisions', choices=['int8', 'fp32'], nargs='+', default=['int8', 'fp32'])
    parser.add_argument('--prefix-ms', type=int, nargs='+', default=[0, 60, 120, 180, 240, 300])
    parser.add_argument('--continuous-trim-ms', type=int, default=1200)
    args = parser.parse_args()
    if len(set(args.prefix_ms)) != len(args.prefix_ms) or any(p < 0 for p in args.prefix_ms):
        parser.error('prefix-ms must contain distinct nonnegative offsets')
    if len(set(args.precisions)) != len(args.precisions):
        parser.error('precisions must be distinct')
    if args.continuous_trim_ms < 0:
        parser.error('continuous-trim-ms must be nonnegative')
    manifest = json.loads(args.manifest.read_text(encoding='utf-8'))
    samples = []
    seen = set()
    for sample in manifest['samples']:
        if sample['id'] in seen:
            raise ValueError(f"duplicate sample ID: {sample['id']}")
        seen.add(sample['id'])
        path = args.manifest.parent / sample['file']
        if hashlib.sha256(path.read_bytes()).hexdigest() != sample['sha256']:
            raise ValueError(f'WAV SHA256 mismatch: {path}')
        samples.append((sample, read_pcm(path)))
    output = {'development_only': True, 'correlated_replays_not_independent_samples': True,
              'model_revision': MODEL_REVISION,
              'manifest_sha256': hashlib.sha256(args.manifest.read_bytes()).hexdigest(),
              'variants': []}
    for precision in args.precisions:
        config = AudioStopConfig(precision=precision)
        backend = KoreanStopSpotter(config)
        rows = []
        for prefix_ms in args.prefix_ms:
            for sample, pcm in samples:
                trim = args.continuous_trim_ms if sample['category'] == 'continuous_stop' else 0
                row = {**sample, **phase_replay(backend, pcm, prefix_ms, trim)}
                row.update(classify_trial(row))
                rows.append(row)
            print(f'completed precision={precision} prefix_ms={prefix_ms}', flush=True)
        # Seeded synthetic controls do not represent real household noise.
        random = np.random.default_rng(20261112)
        controls = []
        for rms in [0, 100, 500, 2000]:
            pcm = np.clip(random.normal(0, rms, 160000), -32768, 32767).astype('<i2').tobytes()
            controls.append({'kind': 'synthetic_gaussian', 'rms': rms, **replay(backend, pcm)})
        summary = summarize(rows)
        output['variants'].append({'config': asdict(config), 'summary': summary,
                                   'trials': rows, 'synthetic_controls': controls})
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(output, ensure_ascii=False, indent=2) + '\n',
                               encoding='utf-8')
        print(json.dumps({'precision': precision, **summary}, ensure_ascii=False), flush=True)
    print(f'results: {args.output}')


if __name__ == '__main__':
    main()
