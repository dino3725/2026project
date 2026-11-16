#!/usr/bin/env python3
"""Calibrate then validate a frozen speaker threshold using disjoint recordings, offline."""

import argparse
from pathlib import Path

from speaker_audio import dataset, private_json

from voice_command_pkg.speaker_backend import SpeakerEncoder
from voice_command_pkg.speaker_profile import choose_threshold, load_profile, similarity


def evaluate(profile, genuine_manifest, other_manifest, stage, encoder):
    """Never reuse enrollment/development PCM in independent validation."""
    excluded = list(profile['enrollment_hashes'])
    if stage == 'validate':
        if 'development' not in profile or profile['calibrated']:
            raise ValueError('validation requires a frozen development profile')
        excluded += profile['development']['hashes']
    suffix = 'dev' if stage == 'calibrate' else 'test'
    genuine = dataset(genuine_manifest, 'genuine-' + suffix, 10, excluded)
    others = dataset(other_manifest, 'other-' + suffix, 10,
                     excluded + [digest for digest, _ in genuine])
    # Poor-quality clips are rejected by the runtime as well. Count them in the
    # denominator, especially as false rejections for genuine speakers.

    def scores(clips):
        values = []
        for _, pcm in clips:
            try:
                values.append(similarity(encoder.encode(pcm), profile['embedding']))
            except ValueError:
                values.append(None)
        return values
    genuine_scores, other_scores = scores(genuine), scores(others)
    if stage == 'calibrate':
        if any(score is None for score in genuine_scores + other_scores):
            raise ValueError('development audio quality failed; record usable clips before tuning')
        choice = choose_threshold(genuine_scores, other_scores)
        profile = {**profile, 'threshold': choice['threshold'], 'calibrated': False}
    threshold = profile['threshold']
    frr = sum(x is None or x < threshold for x in genuine_scores) / len(genuine_scores)
    far = sum(x is not None and x >= threshold for x in other_scores) / len(other_scores)
    evidence = {
        'hashes': [digest for digest, _ in genuine + others],
        'genuine_scores': genuine_scores, 'other_scores': other_scores,
        'genuine_count': len(genuine), 'other_count': len(others), 'frr': frr, 'far': far,
        'threshold': threshold,
    }
    if stage == 'calibrate':
        return {**profile, 'development': evidence}
    passed = frr <= .1 and far == 0
    return {**profile, 'validation': evidence, 'calibrated': passed}


def main():
    """Save a new profile; a failed validation never enables enforcement."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('stage', choices=('calibrate', 'validate'))
    parser.add_argument('--profile', type=Path, required=True)
    parser.add_argument('--genuine', type=Path, required=True, help='Owner recordings manifest')
    parser.add_argument('--other', type=Path, required=True, help='Consented other voice manifest')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.output.expanduser().exists():
        parser.error('choose a fresh output file')
    try:
        profile = load_profile(args.profile)
        encoder = SpeakerEncoder(profile['min_speech_seconds'], profile['backend'])
        result = evaluate(profile, args.genuine, args.other, args.stage, encoder)
        private_json(args.output, result)
    except (ValueError, KeyError, OSError) as exc:
        parser.exit(1, f'중단: {exc}\n')
    evidence = result['development' if args.stage == 'calibrate' else 'validation']
    print(f'FRR={evidence["frr"]:.1%} FAR={evidence["far"]:.1%} '
          f'threshold={result["threshold"]:.6f} calibrated={result["calibrated"]}')
    print(f'결과: {args.output}')
    if args.stage == 'validate' and not result['calibrated']:
        parser.exit(2, '검증 기준 미달: 인증 강제 모드를 켜지 마세요.\n')


if __name__ == '__main__':
    main()
