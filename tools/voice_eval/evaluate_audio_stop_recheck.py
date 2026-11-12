#!/usr/bin/env python3
"""Evaluate fresh utterance checks on development clips, without ROS or a mic."""

import argparse
import hashlib
import json
from pathlib import Path
import statistics
import time

from evaluate_audio_stop import read_pcm
from voice_command_pkg.audio_stop_model import AudioStopConfig, KoreanStopSpotter, MODEL_REVISION
from voice_command_pkg.audio_stop_recheck import recheck_audio


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', type=Path, nargs='+', required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    backend = KoreanStopSpotter(AudioStopConfig())
    rows, skipped = [], []
    for manifest in args.manifest:
        for sample in json.loads(manifest.read_text())['samples']:
            if sample['category'] == 'continuous_stop':
                skipped.append(sample['id'])
                continue
            path = manifest.parent / sample['file']
            if hashlib.sha256(path.read_bytes()).hexdigest() != sample['sha256']:
                raise ValueError(f'WAV SHA256 mismatch: {path}')
            pcm = read_pcm(path)
            started = time.perf_counter()
            hit = recheck_audio(backend, pcm)
            elapsed = time.perf_counter() - started
            rows.append({**sample, 'hit': hit, 'compute_seconds': elapsed,
                         'duration_seconds': len(pcm) / 32000})
    targets = [r for r in rows if r['category'] == 'target_stop']
    negatives = [r for r in rows if r['category'] == 'negative']
    summary = {
        'target_files': len(targets), 'target_hits': sum(bool(r['hit']) for r in targets),
        'negative_files': len(negatives), 'negative_hits': sum(bool(r['hit']) for r in negatives),
        'median_compute_seconds': statistics.median(r['compute_seconds'] for r in rows),
        'max_compute_seconds': max(r['compute_seconds'] for r in rows),
        'over_500ms': sum(r['compute_seconds'] > 0.5 for r in rows),
    }
    result = {'development_only': True, 'no_ros_no_microphone': True,
              'model_revision': MODEL_REVISION, 'precision': 'fp32',
              'synthetic_padding_seconds': 0, 'summary': summary, 'samples': rows,
              'skipped_continuous_requires_pipeline_replay': skipped,
              'manifest_hashes': {str(p): hashlib.sha256(p.read_bytes()).hexdigest()
                                  for p in args.manifest}}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps(summary, ensure_ascii=False))
    print(f'results: {args.output}')


if __name__ == '__main__':
    main()
