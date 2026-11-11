"""Evaluate frozen RGB candidates on new views without fitting camera parameters."""

import argparse
from datetime import datetime
import hashlib
import json
from pathlib import Path

from solve_intrinsics import evaluate, prepare


def digest(path):
    """Hash exact bytes for auditability."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    """Verify frozen inputs and write held-out scores without changing candidates."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--session', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error('Refusing to overwrite existing held-out results')
    root = args.session
    protocol = json.loads((root / 'validation_protocol.json').read_text())
    candidates_path = root / 'intrinsic_candidates.json'
    if digest(candidates_path) != protocol['candidate_sha256']:
        raise ValueError('Candidate changed after validation protocol was frozen')
    if digest(root / 'training_frozen.json') != protocol['training_frozen_sha256']:
        raise ValueError('Training manifest changed')
    candidates = json.loads(candidates_path.read_text())
    solver = Path(__file__).with_name('solve_intrinsics.py')
    if digest(solver) != candidates['solver_sha256']:
        raise ValueError('Scoring implementation changed since candidate calculation')
    groups = {}
    for kind in ['training', 'validation']:
        manifest = json.loads((root / f'{kind}_frozen.json').read_text())
        for relative, expected in manifest['files'].items():
            if digest(root / relative) != expected:
                raise ValueError('Changed input: ' + relative)
        paths = sorted(p for p in manifest['files'] if p.endswith('/observation.json'))
        records = []
        for path in paths:
            record = json.loads((root / path).read_text())
            parent = root / Path(path).parent
            role = 'train' if kind == 'training' else 'validation'
            review_key = f'retained_for_{kind}'
            if (record['role'] != role
                    or not json.loads((parent / 'review.json').read_text())[review_key]
                    or digest(parent / 'rgb.png') != record['image_sha256']):
                raise ValueError('Invalid role, image or review: ' + path)
            records.append(record)
        groups[kind] = records
    if len(groups['validation']) != protocol['validation_view_count']:
        raise ValueError('Incomplete validation set')
    # Joint metadata validation also rejects a held-out frame reused from training.
    prepare(groups['training'] + groups['validation'])
    result = {
        'created': datetime.now().astimezone().isoformat(),
        'status': 'independent_view_scores_only_requires_physical_interpretation',
        'candidate_sha256': digest(candidates_path),
        'validation_frozen_sha256': digest(root / 'validation_frozen.json'),
        'evaluation_script_sha256': digest(Path(__file__)),
        'primary_candidate': protocol['primary_candidate'],
        'training_count': len(groups['training']), 'validation_count': len(groups['validation']),
        'intrinsics_refitted': False, 'applied': False, 'scores': {},
        'method': 'Fixed K/D. Fit board pose per image; score all corners and split corners.',
    }
    models = {'factory': candidates['factory'], **candidates['models']}
    for label, model in models.items():
        result['scores'][label] = evaluate(groups['validation'], model['k'], model['d'])
    with args.output.open('x') as handle:
        json.dump(result, handle, indent=2, allow_nan=False)
        handle.write('\n')
    print(json.dumps({name: {key: score[key] for key in ['rms_px', 'split_corner_rms_px']}
                      for name, score in result['scores'].items()}, indent=2))


if __name__ == '__main__':
    main()
