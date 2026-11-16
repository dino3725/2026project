#!/usr/bin/env python3
"""Rebuild a local speaker template from explicit existing audio and compare development scores."""

import argparse
import hashlib
import json
from pathlib import Path

from calibrate_speaker import evaluate
from speaker_audio import dataset, private_json, read_pcm

from voice_command_pkg.speaker_backend import SpeakerEncoder
from voice_command_pkg.speaker_profile import centroid, load_profile, MODEL_SPECS, similarity


def score(encoder, pcm, reference):
    """Count quality rejections instead of removing hard cases from evaluation."""
    try:
        return similarity(encoder.encode(pcm), reference)
    except ValueError:
        return None


def accepted(value, threshold):
    """Keep invalid audio rejected at every threshold."""
    return value is not None and value >= threshold


def rebuild(args):
    """Keep enrollment, development and historical checks disjoint; do not enable the node."""
    baseline = load_profile(args.baseline)
    enrollment = dataset(args.enrollment, 'enrollment', 20)
    encoder = SpeakerEncoder(.6, args.backend)
    spec = MODEL_SPECS[args.backend]
    vectors = [encoder.encode(pcm) for _, pcm in enrollment]
    profile = {
        'schema': 1, 'backend': args.backend, 'model_sha256': spec['sha256'],
        'preprocessing': spec['preprocessing'], 'sample_rate': 16000, 'min_speech_seconds': .6,
        'embedding': centroid(vectors, spec['dimension']).tolist(),
        'threshold': .75, 'calibrated': False,
        'enrollment_hashes': [digest for digest, _ in enrollment],
    }
    profile = evaluate(profile, args.genuine, args.other, 'calibrate', encoder)
    # Fixed before final pipeline replay; leave a small margin above observed negative scores.
    threshold = profile['threshold'] + .01
    evidence = profile['development']
    evidence['threshold'] = profile['threshold'] = threshold
    evidence['frr'] = (sum(not accepted(x, threshold) for x in evidence['genuine_scores'])
                       / len(evidence['genuine_scores']))
    evidence['far'] = (sum(accepted(x, threshold) for x in evidence['other_scores'])
                       / len(evidence['other_scores']))
    if evidence['frr'] > .1 or evidence['far'] != 0:
        raise ValueError('development margin fails acceptance criteria; profile not saved')
    profile['threshold_method'] = 'max_other_development_plus_0.01'
    seen = set(profile['enrollment_hashes'] + evidence['hashes'])
    checks = []
    for row in json.loads(args.checks.read_text())['samples']:
        if row['speaker'] != 'owner':
            raise ValueError('historical checks require explicit owner labels')
        pcm = read_pcm(Path(row['file']))
        digest = hashlib.sha256(pcm).hexdigest()
        if digest != row['pcm_sha256'] or digest in seen:
            raise ValueError('historical audio changed or reused across splits')
        seen.add(digest)
        checks.append((row['file'], pcm, digest))
    if not checks:
        raise ValueError('no historical checks')
    old_encoder = SpeakerEncoder(.6, baseline['backend'])
    results = []
    groups = {
        'genuine_development': [(str(i + 1), pcm, h) for i, (h, pcm) in enumerate(
            dataset(args.genuine, 'genuine-dev', 10))],
        'other_development': [(str(i + 1), pcm, h) for i, (h, pcm) in enumerate(
            dataset(args.other, 'other-dev', 10))],
        'historical_owner': checks,
    }
    summary = {}
    for group, clips in groups.items():
        old_pass, new_pass = 0, 0
        for name, pcm, digest in clips:
            old_score = score(old_encoder, pcm, baseline['embedding'])
            new_score = score(encoder, pcm, profile['embedding'])
            old_ok = accepted(old_score, baseline['threshold'])
            new_ok = accepted(new_score, threshold)
            old_pass += old_ok
            new_pass += new_ok
            results.append({'group': group, 'file': name, 'pcm_sha256': digest,
                            'old_score': old_score, 'new_score': new_score,
                            'old_allowed': old_ok, 'new_allowed': new_ok})
        summary[group] = {'count': len(clips), 'old_allowed': old_pass, 'new_allowed': new_pass}
        if (group == 'other_development' and new_pass > old_pass
                or group != 'other_development' and new_pass < old_pass):
            raise ValueError('candidate regressed on explicit development checks; not saved')
    private_json(args.output, profile)
    # Re-read with the same strict rules used by enforcement.
    load_profile(args.output, require_development=True)
    private_json(args.report, {
        'note': 'Development comparison used for model selection, not independent validation',
        'baseline_profile_sha256': hashlib.sha256(args.baseline.read_bytes()).hexdigest(),
        'candidate_profile_sha256': hashlib.sha256(args.output.read_bytes()).hexdigest(),
        'backend': args.backend, 'threshold': threshold, 'margin': .01,
        'enrollment_count': len(enrollment), 'summary': summary, 'rows': results,
    })
    print(json.dumps({'profile': str(args.output), 'threshold': threshold,
                      'summary': summary}, ensure_ascii=False, indent=2))


def main():
    """Consume existing recordings only; never capture audio or publish robot commands."""
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('baseline', 'enrollment', 'genuine', 'other', 'checks', 'output', 'report'):
        parser.add_argument('--' + name, type=Path, required=True)
    parser.add_argument('--backend', choices=MODEL_SPECS, default='speechbrain-ecapa-voxceleb-v1')
    args = parser.parse_args()
    if args.output.exists() or args.report.exists():
        parser.error('choose fresh output and report paths')
    rebuild(args)


if __name__ == '__main__':
    main()
