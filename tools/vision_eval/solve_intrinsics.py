"""Fit offline RGB intrinsic candidates; never change camera or robot settings."""

import argparse
from datetime import datetime
import hashlib
import json
from pathlib import Path

import cv2
import numpy as np

from intrinsics_observation import metric_corners


MODELS = {
    'pinhole': (cv2.CALIB_ZERO_TANGENT_DIST | cv2.CALIB_FIX_K1
                | cv2.CALIB_FIX_K2 | cv2.CALIB_FIX_K3),
    'radial2': cv2.CALIB_ZERO_TANGENT_DIST | cv2.CALIB_FIX_K3,
    'brown5_diagnostic': 0,
}


def prepare(records):
    """Validate a single camera/target session and its observed correspondences."""
    if not records:
        raise ValueError('No observations')
    first = records[0]
    info = first['color_info']
    if info['model'] != 'plumb_bob' or len(info['d']) != 5:
        raise ValueError('Expected plumb_bob with five coefficients')
    k = np.asarray(info['k'], dtype=float).reshape(3, 3)
    d = np.asarray(info['d'], dtype=float)
    size = (info['width'], info['height'])
    if (not np.isfinite(k).all() or not np.isfinite(d).all()
            or min(k[0, 0], k[1, 1], *size) <= 0):
        raise ValueError('Invalid camera metadata')
    objects, pixels = [], []
    stamps = set()
    for record in records:
        for key in ['session_id', 'color_info', 'measured_pattern_size_mm', 'nominal_board']:
            if record[key] != first[key]:
                raise ValueError('Mixed session, camera or target geometry')
        if record['rgb_stamp_ns'] in stamps:
            raise ValueError('Duplicate image stamp')
        stamps.add(record['rgb_stamp_ns'])
        obj = metric_corners(record['corner_ids'], *record['measured_pattern_size_mm'])
        px = np.asarray(record['corners_px'], dtype=np.float32)
        if (len(obj) < 12 or px.shape != (len(obj), 2) or not np.isfinite(px).all()
                or not np.allclose(record['object_points_m'], obj, atol=1e-8)
                or np.linalg.matrix_rank(obj[:, :2] - obj[:, :2].mean(axis=0)) < 2
                or np.any(px < 0) or np.any(px >= np.asarray(size))):
            raise ValueError('Invalid corner correspondences')
        objects.append(obj)
        pixels.append(px)
    return objects, pixels, k, d, size


def evaluate(records, k, d):
    """Score fixed intrinsics with all-point and split-point pose estimation."""
    objects, pixels, _, _, _ = prepare(records)
    k = np.asarray(k, dtype=float).reshape(3, 3)
    d = np.asarray(d, dtype=float).reshape(-1)
    rows = []
    total_square, total_count = 0.0, 0
    split_square, split_count = 0.0, 0
    for record, obj, px in zip(records, objects, pixels):
        ok, rv, tv = cv2.solvePnP(obj, px, k, d)
        if not ok or np.min((cv2.Rodrigues(rv)[0] @ obj.T + tv)[2]) <= 0:
            raise ValueError('Invalid positive-depth board pose')
        projected = cv2.projectPoints(obj, rv, tv, k, d)[0].reshape(-1, 2)
        squares = np.sum((px - projected) ** 2, axis=1)
        total_square += float(squares.sum())
        total_count += len(squares)
        ids = np.asarray(record['corner_ids'])
        # Checker parity distributes both subsets over both grid axes.
        subset = ((ids % 6 + ids // 6) % 2) == 0
        errors = []
        for fit_mask in [subset, ~subset]:
            if min(np.sum(fit_mask), np.sum(~fit_mask)) < 6:
                raise ValueError('Need at least six corners per split subset')
            fit_obj = obj[fit_mask]
            if np.linalg.matrix_rank(fit_obj[:, :2] - fit_obj[:, :2].mean(axis=0)) < 2:
                raise ValueError('Collinear pose-fit subset')
            ok, split_rv, split_tv = cv2.solvePnP(fit_obj, px[fit_mask], k, d)
            if not ok:
                raise ValueError('Split-corner PnP failed')
            prediction = cv2.projectPoints(obj[~fit_mask], split_rv, split_tv, k, d)[0]
            errors.extend(np.sum((px[~fit_mask] - prediction.reshape(-1, 2)) ** 2,
                                 axis=1).tolist())
        split_square += sum(errors)
        split_count += len(errors)
        rows.append({'stamp_ns': record['rgb_stamp_ns'], 'corner_count': len(obj),
                     'rms_px': float(np.sqrt(squares.mean())),
                     'split_corner_rms_px': float(np.sqrt(np.mean(errors))),
                     'normal': cv2.Rodrigues(rv)[0][:, 2].tolist()})
    return {'rms_px': float(np.sqrt(total_square / total_count)),
            'split_corner_rms_px': float(np.sqrt(split_square / split_count)),
            'per_view': rows}


def fit(records, model):
    """Fit training observations only, refusing audit or validation inputs."""
    if len(records) < 10 or any(r['role'] != 'train' for r in records):
        raise ValueError('Need at least ten training-only observations')
    objects, pixels, k, _, size = prepare(records)
    result = cv2.calibrateCameraExtended(
        objects, pixels, size, k.copy(), np.zeros(5),
        flags=cv2.CALIB_USE_INTRINSIC_GUESS | MODELS[model],
        criteria=(cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_COUNT, 100, 1e-9))
    rms, estimated, coefficients, _, _, std_intr, _, per_view = result
    if (not np.isfinite(estimated).all() or not np.isfinite(coefficients).all()
            or not np.isfinite(rms) or min(estimated[0, 0], estimated[1, 1]) <= 0):
        raise ValueError('Nonfinite or invalid fitted intrinsics')
    return {'model': model, 'rms_px': float(rms), 'k': estimated.tolist(),
            'd': coefficients.reshape(-1).tolist(),
            'internal_parameter_std': std_intr.reshape(-1).tolist(),
            'per_view_rms_px': per_view.reshape(-1).tolist(), 'applied': False}


def main():
    """Verify frozen inputs, compare models, and freeze candidates before holdout."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--session', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error('Output exists; frozen candidates must not be overwritten')
    cv2.setNumThreads(1)
    frozen_path = args.session / 'training_frozen.json'
    frozen = json.loads(frozen_path.read_text())
    for relative, digest in frozen['files'].items():
        if hashlib.sha256((args.session / relative).read_bytes()).hexdigest() != digest:
            raise ValueError('Frozen input changed: ' + relative)
    paths = sorted(p for p in frozen['files'] if p.endswith('/observation.json'))
    records = [json.loads((args.session / p).read_text()) for p in paths]
    if len(records) != frozen['training_count']:
        raise ValueError('Frozen observation count mismatch')
    for relative, record in zip(paths, records):
        folder = args.session / Path(relative).parent
        if not json.loads((folder / 'review.json').read_text())['retained_for_training']:
            raise ValueError('Unreviewed or rejected training observation')
        if hashlib.sha256((folder / 'rgb.png').read_bytes()).hexdigest() != record['image_sha256']:
            raise ValueError('Observation image hash mismatch')
    _, _, k, d, size = prepare(records)
    result = {
        'created': datetime.now().astimezone().isoformat(),
        'status': 'candidates_frozen_pending_independent_validation', 'applied': False,
        'session_id': records[0]['session_id'], 'image_size': size,
        'training_files': paths, 'training_frozen_sha256': hashlib.sha256(
            frozen_path.read_bytes()).hexdigest(),
        'solver_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        'measured_pattern_size_mm': records[0]['measured_pattern_size_mm'],
        'factory': {'k': k.tolist(), 'd': d.tolist(), 'training': evaluate(records, k, d)},
        'models': {},
        'limitations': [
            'Printed dimensions assume uniform scaling per axis; ruler uncertainty unknown.',
            'Reprojection accuracy does not establish metric depth or camera mounting accuracy.',
            'Internal standard deviations and leave-one-view-out ranges '
            'are diagnostics, not guarantees.',
            'Raw RGB only; do not replace aligned-depth intrinsics '
            'without alignment consistency review.',
        ],
    }
    for model in MODELS:
        candidate = fit(records, model)
        candidate['training'] = evaluate(records, candidate['k'], candidate['d'])
        if model != 'brown5_diagnostic':
            rows = []
            for index in range(len(records)):
                subset = records[:index] + records[index + 1:]
                partial = fit(subset, model)
                score = evaluate([records[index]], partial['k'], partial['d'])
                rows.append({'omitted_index': index, 'k': partial['k'], 'd': partial['d'],
                             'omitted_view_rms_px': score['rms_px'],
                             'omitted_split_rms_px': score['split_corner_rms_px']})
            candidate['leave_one_view_out'] = rows
        result['models'][model] = candidate
        print(json.dumps({'model': model, 'rms_px': candidate['rms_px'],
                          'k': candidate['k'], 'd': candidate['d']}), flush=True)
    with args.output.open('x') as handle:
        json.dump(result, handle, indent=2, allow_nan=False)
        handle.write('\n')


if __name__ == '__main__':
    main()
