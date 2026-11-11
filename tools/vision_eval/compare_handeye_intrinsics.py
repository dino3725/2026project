"""Compare fixed RGB intrinsic models on saved hand-eye data without ROS."""

import argparse
from datetime import datetime
import hashlib
import json
from pathlib import Path

import cv2
import numpy as np
from scipy.spatial.transform import Rotation

from handeye import consistency, solve, transform
from intrinsics_observation import extract, metric_corners
from refine_handeye import refine


def digest(path):
    """Hash original inputs without modifying them."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def pnp(obj, pixels, k, d):
    """Estimate positive-depth board pose, retaining all supplied correspondences."""
    obj, pixels = np.asarray(obj, dtype=float), np.asarray(pixels, dtype=float)
    ok, rv, tv = cv2.solvePnP(obj, pixels, k, d)
    rotation = cv2.Rodrigues(rv)[0]
    if not ok or np.min((rotation @ obj.T + tv)[2]) <= 0:
        raise ValueError('Invalid board pose')
    projected = cv2.projectPoints(obj, rv, tv, k, d)[0].reshape(-1, 2)
    rms = float(np.sqrt(np.mean(np.sum((projected - pixels) ** 2, axis=1))))
    if not np.isfinite(rms):
        raise ValueError('Nonfinite board residual')
    return transform(rotation, tv), rms


def fixed_board_pixel_rms(records, objects, pixels, mount, fixed, k, d):
    """Predict held-out pixels using training-only mount and fixed board pose."""
    scores = []
    for record, obj, observed in zip(records, objects, pixels):
        ob = np.linalg.inv(mount) @ np.linalg.inv(record['T_base_ee']) @ fixed
        if np.min((ob[:3, :3] @ obj.T + ob[:3, 3, None])[2]) <= 0:
            raise ValueError('Predicted board behind camera')
        projected = cv2.projectPoints(
            obj, cv2.Rodrigues(ob[:3, :3])[0], ob[:3, 3], k, d)[0].reshape(-1, 2)
        scores.append(float(np.sqrt(np.mean(np.sum((projected - observed) ** 2, axis=1)))))
    return scores


def calculate(records, objects, pixels, k, d, width_mm, height_mm):
    """Fit mount and board from training only; diagnose the reused held-out views."""
    train_indices = [i for i, r in enumerate(records) if r['role'] == 'train']
    held_indices = [i for i, r in enumerate(records) if r['role'] == 'validation']
    if len(train_indices) < 8 or len(held_indices) < 3:
        raise ValueError('Need complete train and validation observations')
    solutions = [pnp(obj, px, k, d) for obj, px in zip(objects, pixels)]
    ee = [records[i]['T_base_ee'] for i in train_indices]
    held_ee = [records[i]['T_base_ee'] for i in held_indices]
    targets = [solutions[i][0] for i in train_indices]
    held_targets = [solutions[i][0] for i in held_indices]
    mount, singular = solve(ee, targets)
    _, reference = consistency(ee, targets, mount)
    refined, fixed, refinement = refine(
        ee, [objects[i] for i in train_indices], [pixels[i] for i in train_indices],
        k, d, mount, reference)
    results = {}
    for label, x, ref in [('park', mount, reference), ('reprojection', refined, fixed)]:
        train_stats, _ = consistency(ee, targets, x, ref)
        held_stats, _ = consistency(held_ee, held_targets, x, ref)
        camera = x @ np.linalg.inv(records[0]['T_camera_optical'])
        center = ref @ np.array([width_mm / 2000, height_mm / 2000, 0, 1])
        results[label] = {
            'T_ee_optical': x.tolist(), 'T_ee_camera': camera.tolist(),
            'T_base_board': ref.tolist(), 'camera_xyz_mm': (camera[:3, 3] * 1000).tolist(),
            'camera_rpy_deg': Rotation.from_matrix(camera[:3, :3]).as_euler(
                'xyz', degrees=True).tolist(),
            # Existing base_footprint -> base_link z = 10 mm; table supports wheels.
            'board_center_above_support_mm': float((center[2] + 0.01) * 1000),
            'training': train_stats, 'reused_validation': held_stats,
            'held_fixed_board_reprojection_rms_px': fixed_board_pixel_rms(
                [records[i] for i in held_indices], [objects[i] for i in held_indices],
                [pixels[i] for i in held_indices], x, ref, k, d),
        }
    return {'methods': results, 'refinement': refinement,
            'rotation_singular_values': singular,
            'per_view_pnp_rms_px': [s[1] for s in solutions]}


def compare_session(root, candidates, width_mm, height_mm):
    """Compare identical original pixels, then independently re-detected pixels."""
    paths = sorted(root.glob('train_*/observation.json'))
    paths += sorted(root.glob('validation_*/observation.json'))
    records = [json.loads(p.read_text()) for p in paths]
    first = records[0]
    original_objects, original_pixels, neutral_objects, neutral_pixels = [], [], [], []
    hashes, neutral_counts = {}, []
    for path, record in zip(paths, records):
        for key in ['color_info', 'robot_description_sha256', 'session_id',
                    'T_camera_optical', 'T_base_link1', 'board']:
            if record[key] != first[key]:
                raise ValueError('Mixed camera, model or session: ' + key)
        image_path = path.parent / 'rgb.png'
        if digest(image_path) != record['image_sha256']:
            raise ValueError('Original RGB image hash mismatch')
        hashes[str(path)] = digest(path)
        hashes[str(image_path)] = digest(image_path)
        original_objects.append(metric_corners(
            record['corner_ids'], width_mm, height_mm).astype(float))
        original_pixels.append(np.asarray(record['corners_px'], dtype=float))
        image = cv2.imread(str(image_path))
        if image is None or image.shape[:2] != (first['color_info']['height'],
                                                first['color_info']['width']):
            raise ValueError('Image geometry mismatch')
        detected = extract(image, width_mm, height_mm)
        neutral_objects.append(np.asarray(detected['object_points_m'], dtype=float))
        neutral_pixels.append(np.asarray(detected['corners_px'], dtype=float))
        neutral_counts.append(detected['corner_count'])
    info = first['color_info']
    if ([info['width'], info['height']] != candidates['image_size']
            or not np.allclose(info['k'], np.asarray(candidates['factory']['k']).ravel())
            or not np.allclose(info['d'], candidates['factory']['d'])):
        raise ValueError('Intrinsic candidate belongs to a different camera stream')
    result = {'session_id': first['session_id'], 'inputs_sha256': hashes,
              'model_sha256': first['robot_description_sha256'],
              'view_names': [p.parent.name for p in paths],
              'neutral_detection_corner_counts': neutral_counts, 'comparisons': {}}
    for mode, objects, pixels in [
        ('original_pixels', original_objects, original_pixels),
        ('neutral_redetection', neutral_objects, neutral_pixels),
    ]:
        result['comparisons'][mode] = {}
        for name, model in [('factory', candidates['factory']),
                            ('radial2', candidates['models']['radial2'])]:
            k = np.asarray(model['k'], dtype=float).reshape(3, 3)
            d = np.asarray(model['d'], dtype=float)
            scores = calculate(records, objects, pixels, k, d, width_mm, height_mm)
            result['comparisons'][mode][name] = scores
            print(json.dumps({'session': root.name, 'pixels': mode, 'intrinsics': name,
                              'methods': {label: {
                                  'height_mm': m['board_center_above_support_mm'],
                                  'held_max_mm': m['reused_validation']['max_translation_mm']}
                                  for label, m in scores['methods'].items()}}), flush=True)
    return result


def main():
    """Save isolated diagnostics while preserving all original files and settings."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--sessions', nargs='+', type=Path, required=True)
    parser.add_argument('--intrinsics-session', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error('Refusing to overwrite prior comparison')
    cv2.setNumThreads(1)
    candidate_path = args.intrinsics_session / 'intrinsic_candidates.json'
    protocol = json.loads((args.intrinsics_session / 'validation_protocol.json').read_text())
    if digest(candidate_path) != protocol['candidate_sha256']:
        raise ValueError('Fixed intrinsic candidates changed')
    candidates = json.loads(candidate_path.read_text())
    dimensions = candidates['measured_pattern_size_mm']
    result = {
        'created': datetime.now().astimezone().isoformat(), 'applied': False,
        'status': 'offline_diagnostic_reused_handeye_validation_not_new_independent_test',
        'candidate_sha256': digest(candidate_path), 'source_sha256': digest(Path(__file__)),
        'printed_dimensions_mm': dimensions, 'base_link_height_above_support_mm': 10,
        'no_height_constraint': True, 'intrinsics_refitted': False, 'depth_recomputed': False,
        'comparisons': [compare_session(root, candidates, *dimensions) for root in args.sessions],
    }
    with args.output.open('x') as stream:
        json.dump(result, stream, indent=2, allow_nan=False)
        stream.write('\n')


if __name__ == '__main__':
    main()
