#!/usr/bin/env python3
"""Compare original/phase rechecks and the shared deadline on development WAVs."""

import argparse
import hashlib
import json
from pathlib import Path
import statistics
import time

from evaluate_audio_stop import read_pcm
from voice_command_pkg.audio_stop_model import AudioStopConfig, KoreanStopSpotter
from voice_command_pkg.audio_stop_recheck import recheck_audio, RECHECK_PREFIXES_MS


def trial(backend, pcm, phases, deadline_seconds=None):
    """Distinguish absent detections from results discarded by the runtime deadline."""
    start = time.perf_counter()
    deadline = float('inf') if deadline_seconds is None else start + deadline_seconds
    hit = recheck_audio(backend, pcm, lambda: time.perf_counter() > deadline,
                        phase_shifts=phases)
    elapsed = time.perf_counter() - start
    expired = elapsed > (deadline_seconds if deadline_seconds is not None else float('inf'))
    return {'hit': None if expired else hit, 'expired': expired, 'compute_seconds': elapsed}


def summarize(rows):
    """Count each source recording once per method; unconfirmed inputs stay separate."""
    summary = {}
    for category in sorted({row['category'] for row in rows}):
        selected = [row for row in rows if row['category'] == category]
        summary[category] = {
            'files': len({row.get('pcm_sha256', i) for i, row in enumerate(selected)}),
            'trials': len(selected),
        }
        for method in ('original', 'phases', 'bounded_phases'):
            trials = [row[method] for row in selected]
            summary[category][method] = {
                'hits': sum(bool(t['hit']) for t in trials),
                'expired': sum(t['expired'] for t in trials),
                'median_seconds': statistics.median(t['compute_seconds'] for t in trials),
                'max_seconds': max(t['compute_seconds'] for t in trials),
            }
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', type=Path, nargs='+', required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--prefix-ms', type=int, nargs='+', default=[0],
                        help='Input offsets for sensitivity testing, not new recordings.')
    parser.add_argument('--targets-only', action='store_true')
    args = parser.parse_args()
    if (len(set(args.prefix_ms)) != len(args.prefix_ms)
            or any(p < 0 or p > 1000 for p in args.prefix_ms)):
        parser.error('prefix-ms must be distinct offsets between 0 and 1000')
    backend = KoreanStopSpotter(AudioStopConfig())
    rows, seen, skipped = [], set(), []
    for manifest in args.manifest:
        for sample in json.loads(manifest.read_text())['samples']:
            if sample['category'] == 'continuous_stop':
                skipped.append(sample['id'])
                continue
            if args.targets_only and sample['category'] != 'target_stop':
                continue
            path = manifest.parent / sample['file']
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            if digest != sample['sha256']:
                raise ValueError(f'WAV SHA256 mismatch: {path}')
            pcm = read_pcm(path)
            pcm_digest = hashlib.sha256(pcm).hexdigest()
            if pcm_digest in seen:
                raise ValueError(f'duplicate PCM recording: {path}')
            seen.add(pcm_digest)
            for prefix_ms in args.prefix_ms:
                row = {**sample, 'pcm_sha256': pcm_digest, 'input_prefix_ms': prefix_ms}
                for method, phases, deadline in (
                    ('original', False, None), ('phases', True, None),
                    ('bounded_phases', True, AudioStopConfig().max_lag_seconds),
                ):
                    result = trial(backend, bytes(prefix_ms * 32) + pcm, phases, deadline)
                    hit = result['hit']
                    result['synthetic_prefix_hit'] = bool(hit and hit[1] <= prefix_ms / 1000)
                    if hit:
                        result['hit'] = (None if result['synthetic_prefix_hit'] else
                                         (hit[0], hit[1] - prefix_ms / 1000))
                    row[method] = result
                rows.append(row)
    # Synthetic silence is not a household-noise recording or an accuracy sample.
    silence = trial(backend, bytes(32000 * 10), True)
    result = {
        'development_only': True, 'no_ros_no_microphone': True,
        'prefixes_ms': list(RECHECK_PREFIXES_MS), 'synthetic_tail_seconds': 0,
        'input_prefixes_ms': args.prefix_ms, 'targets_only': args.targets_only,
        'correlated_replays_not_independent_samples': True,
        'deadline_seconds': AudioStopConfig().max_lag_seconds,
        'summary': summarize(rows), 'samples': rows, 'synthetic_silence_10s': silence,
        'skipped_continuous_requires_pipeline_replay': skipped,
        'manifest_hashes': {str(p): hashlib.sha256(p.read_bytes()).hexdigest()
                            for p in args.manifest},
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps(result['summary'], ensure_ascii=False, indent=2))
    print(f'results: {args.output}')


if __name__ == '__main__':
    main()
