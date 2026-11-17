"""Replay saved RGB-D pairs without ROS or hardware; compare surface selectors."""

import argparse
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import sys

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'src/vision_pkg'))

from vision_pkg.surface_localization import estimate_surface  # noqa: E402
from vision_pkg.vision_utils import deproject_pixel  # noqa: E402
from vision_pkg.vision_utils import Detection  # noqa: E402
from vision_pkg.vision_utils import get_detection_depth  # noqa: E402
from vision_pkg.vision_utils import get_median_depth  # noqa: E402
from vision_pkg.vision_utils import select_target_detection  # noqa: E402


def sha(path):
    """Identify exact replay inputs."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def stamp_ns(meta):
    """Keep timestamp subtraction in integer nanoseconds."""
    sec, nano = meta['stamp']
    return int(sec) * 1_000_000_000 + int(nano)


def main():
    """Write per-frame metrics and overlays; do not claim ground-truth error."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--sessions', nargs='+', type=Path, required=True)
    parser.add_argument('--model', type=Path, default=ROOT / 'best.pt')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if not args.model.is_file():
        parser.error('Local model required; downloads are not permitted here')
    args.output.mkdir(parents=True, exist_ok=False)
    from ultralytics import YOLO
    model = YOLO(str(args.model))
    report = {
        'model_sha256': sha(args.model), 'device': 'cpu', 'imgsz': 640,
        'scope': 're-inferred saved frames; surface-to-surface comparison only',
        'limitations': [
            'No ground truth object centers or grasp contacts.',
            'Saved snapshots are not the original action inference frames.',
            'Nonzero RGB-depth time offsets are reported, not silently paired.',
            'Camera-frame comparisons avoid reusing latest TF as image-time TF.',
            'Dominant depth does not guarantee foreground segmentation.',
        ],
        'source_sha256': {
            str(p.relative_to(ROOT)): sha(p) for p in [
                Path(__file__),
                ROOT / 'src/vision_pkg/vision_pkg/surface_localization.py',
                ROOT / 'src/vision_pkg/vision_pkg/vision_utils.py']},
        'rows': [],
    }
    for session_path in args.sessions:
        path = session_path / 'session.json'
        session = json.loads(path.read_text())
        info = session['camera_info']
        if any(abs(v) > 1e-12 for v in info['d']):
            raise ValueError('Replay needs explicit distortion handling for this session')
        k = info['k']
        intrinsics = (k[0], k[4], k[2], k[5])
        for trial in session['trials']:
            meta = trial.get('images', {})
            if not all(key in meta for key in ('rgb', 'depth')):
                continue
            rgb_path = session_path / meta['rgb']['file']
            depth_path = session_path / meta['depth']['file']
            rgb = cv2.imread(str(rgb_path))
            depth = cv2.imread(str(depth_path), cv2.IMREAD_UNCHANGED)
            if (rgb is None or depth is None or depth.shape != rgb.shape[:2]
                    or depth.shape != (info['height'], info['width'])):
                raise ValueError('Invalid saved image shape')
            row = {
                'session': str(session_path), 'trial': trial['trial'],
                'input_sha256': {str(p): sha(p) for p in [path, rgb_path, depth_path]},
                'rgb_depth_offset_ms': (stamp_ns(meta['rgb'])
                                        - stamp_ns(meta['depth'])) / 1e6,
            }
            detections = []
            for prediction in model.predict(rgb, device='cpu', imgsz=640, verbose=False):
                for box in prediction.boxes:
                    detections.append(Detection(
                        model.names[int(box.cls.item())], float(box.conf.item()),
                        *[float(x) for x in box.xyxy[0].tolist()]))
            detected = select_target_detection(detections, session['target'], .5)
            row['detection'] = asdict(detected) if detected else None
            if detected is not None:
                encoding = meta['depth']['encoding']
                central = get_median_depth(depth, *detected.center, encoding, .001, 7)
                old_depth = get_detection_depth(depth, detected, encoding, .001, 7)
                old_point = (deproject_pixel(*detected.center, old_depth, *intrinsics)
                             if old_depth is not None else None)
                new = estimate_surface(depth, detected, encoding, .001, intrinsics)
                row.update(legacy_source='center' if central else 'inner',
                           legacy_point=old_point, candidate=asdict(new))
                if old_point is not None and new.point is not None:
                    delta = 1000 * (np.array(new.point) - old_point)
                    row['camera_delta_mm'] = delta.tolist()
                    row['distance_between_methods_mm'] = float(np.linalg.norm(delta))
                cv2.rectangle(rgb, (round(detected.x1), round(detected.y1)),
                              (round(detected.x2), round(detected.y2)), (255, 160, 0), 2)
                cv2.drawMarker(rgb, detected.center, (0, 0, 255),
                               cv2.MARKER_CROSS, 20, 2)
                if new.pixel is not None:
                    cv2.circle(rgb, new.pixel, 7, (0, 255, 0), 2)
                labels = [
                    'RED: legacy center / GREEN: candidate observed surface',
                    f'candidate={new.source}; {new.reason or "accepted"}',
                    f'valid={new.valid_fraction:.2f}, support={new.support_fraction:.2f}',
                    f'RGB-depth offset={row["rgb_depth_offset_ms"]:.2f} ms',
                    'NOT object center / NOT absolute accuracy validation',
                ]
                for i, label in enumerate(labels):
                    cv2.putText(rgb, label, (12, 25 + 23 * i),
                                cv2.FONT_HERSHEY_SIMPLEX, .55, (255, 255, 255), 2)
            name = f'{len(report["rows"]):03d}_comparison.png'
            if not cv2.imwrite(str(args.output / name), rgb):
                raise OSError('Cannot save overlay')
            row['overlay'] = name
            report['rows'].append(row)
    good = [r for r in report['rows'] if r.get('detection')]
    report['summary'] = {
        'frames': len(report['rows']), 'detections': len(good),
        'legacy_valid': sum(r['legacy_point'] is not None for r in good),
        'candidate_valid': sum(r['candidate']['point'] is not None for r in good),
        'legacy_fallbacks': sum(r['legacy_source'] == 'inner' for r in good),
        'exact_timestamp_pairs': sum(r['rgb_depth_offset_ms'] == 0 for r in report['rows']),
    }
    (args.output / 'comparison.json').write_text(
        json.dumps(report, indent=2, allow_nan=False) + '\n')
    print(json.dumps(report['summary'], indent=2))


if __name__ == '__main__':
    main()
