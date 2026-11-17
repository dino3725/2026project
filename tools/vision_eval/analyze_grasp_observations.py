"""Analyze exact inference captures; never send ROS or actuator commands."""

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
sys.path.insert(0, str(ROOT / 'src/control_pkg'))

from control_pkg.grasp_reference import upright_contact  # noqa: E402
from vision_pkg.object_geometry import fit_upright_axis  # noqa: E402
from vision_pkg.surface_localization import estimate_surface  # noqa: E402
from vision_pkg.vision_utils import Detection  # noqa: E402


def rotation_xyzw(values):
    """Construct a rotation only from a normalized finite quaternion."""
    q = np.asarray(values, dtype=float)
    if q.shape != (4,) or not np.isfinite(q).all() or abs(np.linalg.norm(q) - 1) > 1e-4:
        raise ValueError('Invalid captured rotation')
    x, y, z, w = q / np.linalg.norm(q)
    return np.array([[1 - 2 * (y*y + z*z), 2*(x*y-z*w), 2*(x*z+y*w)],
                     [2*(x*y+z*w), 1-2*(x*x+z*z), 2*(y*z-x*w)],
                     [2*(x*z-y*w), 2*(y*z+x*w), 1-2*(x*x+y*y)]])


def analyze(meta, depth, support_z, height, radius, max_residual_p90_m=.005):
    """Separate observed surfaces and conditional object-axis hypotheses."""
    result = {'surface': None, 'axis': None}
    if meta['detection'] is None:
        return dict(result, reason='no detection')
    detection = Detection(**meta['detection'])
    k = meta['intrinsics_fx_fy_cx_cy']
    result['surface'] = asdict(estimate_surface(
        depth, detection, meta['depth_encoding'], meta['depth_scale'], k))
    if meta['camera_to_base'] is None:
        return dict(result, reason='image-time TF unavailable')
    if abs(meta['rgb_stamp_ns'] - meta['depth_stamp_ns']) > 50_000_000:
        return dict(result, reason='RGB-depth mismatch exceeds live sync slop')
    if detection.class_name != 'coke':
        return dict(result, reason='upright cylinder hypothesis only enabled for coke')
    tf = meta['camera_to_base']
    r = rotation_xyzw(tf['quaternion_xyzw'])
    t = np.array(tf['translation'])
    fx, fy, cx, cy = k
    # Trim image edges, but do not assume the resulting ROI is a segmentation.
    dx = (detection.x2 - detection.x1) * .1
    dy = (detection.y2 - detection.y1) * .1
    x0, x1 = max(0, int(detection.x1 + dx)), min(depth.shape[1], int(detection.x2 - dx))
    y0, y1 = max(0, int(detection.y1 + dy)), min(depth.shape[0], int(detection.y2 - dy))
    v, u = np.mgrid[y0:y1:2, x0:x1:2]
    z = depth[v, u].astype(float).ravel()
    if meta['depth_encoding'].upper() == '16UC1':
        z *= meta['depth_scale']
    valid = np.isfinite(z) & (z > 0)
    z, u, v = z[valid], u.ravel()[valid], v.ravel()[valid]
    points = np.column_stack(((u-cx)*z/fx, (v-cy)*z/fy, z)) @ r.T + t
    result['axis'] = fit_upright_axis(
        points, t, support_z, height, radius, max_residual_p90_m)
    if result['axis']['accepted']:
        xy = result['axis']['axis_xy_m']
        result['contact_example_base_m'] = upright_contact(xy, support_z, height, .03)
        # A contact example is NOT an end-effector goal; TCP is not validated.
        center_camera = (np.array(result['contact_example_base_m']) - t) @ r
        if center_camera[2] > 0:
            result['contact_example_pixel'] = [
                int(round(fx * center_camera[0] / center_camera[2] + cx)),
                int(round(fy * center_camera[1] / center_camera[2] + cy))]
    if result['surface']['point'] is not None:
        result['candidate_surface_base_m'] = (
            r @ result['surface']['point'] + t).tolist()
    return result


def main():
    """Write hypotheses, rejection reasons and overlays for every captured frame."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--captures', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--support-z-base', type=float, required=True)
    parser.add_argument('--after-stamp-ns', type=int, default=0)
    parser.add_argument('--until-stamp-ns', type=int, default=2**63-1)
    parser.add_argument('--max-residual-p90-m', type=float, default=.005)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    report = {'assumptions': {'support_z_base_m': args.support_z_base,
                              'upright_coke_height_m': .134, 'radius_m': .0265,
                              'max_residual_p90_m': args.max_residual_p90_m},
              'limitations': ['Axis hypotheses not independently measured.',
                              'Contact example is not an executable tool goal.',
                              'ROI is not a segmentation mask.'], 'frames': []}
    for path in sorted(args.captures.glob('*.json')):
        meta = json.loads(path.read_text())
        if not args.after_stamp_ns < meta['rgb_stamp_ns'] <= args.until_stamp_ns:
            continue
        depth = np.load(args.captures / meta['depth_file'], allow_pickle=False)
        row = analyze(meta, depth, args.support_z_base, .134, .0265,
                      args.max_residual_p90_m)
        row['capture'] = str(path)
        row['rgb_stamp_ns'] = meta['rgb_stamp_ns']
        row['input_sha256'] = {str(p): hashlib.sha256(p.read_bytes()).hexdigest()
                               for p in (path, args.captures / meta['rgb_file'],
                                         args.captures / meta['depth_file'])}
        row['legacy_surface_base_m'] = meta['base_surface']
        rgb = cv2.imread(str(args.captures / meta['rgb_file']))
        if meta['detection']:
            box = Detection(**meta['detection'])
            cv2.drawMarker(rgb, box.center, (0, 0, 255), cv2.MARKER_CROSS, 20, 2)
        if row.get('contact_example_pixel'):
            cv2.drawMarker(rgb, tuple(row['contact_example_pixel']),
                           (0, 255, 255), cv2.MARKER_DIAMOND, 20, 2)
        for i, label in enumerate([
            'RED: observed surface / YELLOW: model contact example',
            'Known upright cylinder + support height assumption',
            'NOT executable tool goal / absolute accuracy NOT verified',
        ]):
            cv2.putText(rgb, label, (10, 25 + 25*i), cv2.FONT_HERSHEY_SIMPLEX,
                        .55, (255, 255, 255), 2)
        overlay = f'{path.stem}_geometry.png'
        if not cv2.imwrite(str(args.output / overlay), rgb):
            raise OSError('Cannot write geometry overlay')
        row['overlay'] = overlay
        report['frames'].append(row)
    accepted = [r['axis']['axis_xy_m'] for r in report['frames']
                if r['axis'] and r['axis']['accepted']]
    report['source_sha256'] = {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
                               for p in [Path(__file__), ROOT / 'src/vision_pkg/vision_pkg/object_geometry.py']}
    report['summary'] = {'frames': len(report['frames']), 'axis_accepted': len(accepted)}
    if accepted:
        report['summary']['axis_mean_m'] = np.mean(accepted, axis=0).tolist()
        report['summary']['axis_peak_to_peak_mm'] = (1000*np.ptp(accepted, axis=0)).tolist()
    (args.output / 'geometry.json').write_text(json.dumps(report, indent=2, allow_nan=False)+'\n')
    print(json.dumps(report['summary'], indent=2))


if __name__ == '__main__':
    main()
