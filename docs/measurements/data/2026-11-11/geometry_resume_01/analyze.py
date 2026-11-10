"""Compare visible tabletop and lid in matched RGB-D snapshots; no ROS."""
import hashlib
import json
from pathlib import Path

import cv2
import numpy as np
from scipy.spatial.transform import Rotation


def parallel_plane_fit(points, labels):
    """z = a*x + b*y + c + label*d, distances in mm."""
    design = np.column_stack([points[:, :2], np.ones(len(points)), labels])
    coef = np.linalg.lstsq(design, points[:, 2], rcond=None)[0]
    residual = points[:, 2] - design @ coef
    return {
        'coefficients_a_b_c_d': coef.tolist(),
        'normal_separation_mm': float(abs(coef[3]) / np.sqrt(1 + sum(coef[:2] ** 2))),
        'residual_rms_mm': float(np.sqrt(np.mean(residual ** 2))),
    }


def validate_fit():
    # Known 80mm normal separation, tilted planes, arbitrary vertical origin.
    x, y = np.meshgrid(np.arange(5) * 30., np.arange(4) * 25.)
    x, y = np.tile(x.ravel(), 2), np.tile(y.ravel(), 2)
    labels = np.repeat([0., 1.], len(x) // 2)
    z = .1 * x - .2 * y + 13 + labels * 80 * np.sqrt(1.05)
    points = np.column_stack([x, y, z])
    for shift in (0, 25):
        shifted = points + [0, 0, shift]
        assert abs(parallel_plane_fit(shifted, labels)['normal_separation_mm'] - 80) < 1e-9


root = Path(__file__).resolve().parent
validate_fit()
capture = json.loads((root / 'capture.json').read_text())
# Manually selected after inspecting fresh 001_rgb.png. Exclude can, rim,
# scissors and phone. Rectangle end coordinates are exclusive; sample stride 4.
regions = {'table_front_left': [80, 550, 260, 680],
           'table_middle_left': [245, 350, 320, 500],
           'table_front_right': [1130, 610, 1230, 680],
           'lid_left': [450, 360, 600, 580],
           'lid_right': [820, 350, 940, 560]}
rows = []
for frame in capture['frames']:
    index = frame['index']
    depth = cv2.imread(str(root / f'{index:03d}_depth.png'), cv2.IMREAD_UNCHANGED)
    k = frame['intrinsics']['k']
    tf = frame['transforms_to_base_link_at_depth_stamp'][frame['optical_frame']]
    rotation = Rotation.from_quat(tf['quaternion'])
    points, labels, statistics = [], [], {}
    for name, (x0, y0, x1, y1) in regions.items():
        v, u = np.mgrid[y0:y1:4, x0:x1:4]
        z = depth[v, u].astype(float).ravel()
        optical = np.column_stack([(u.ravel() - k[2]) * z / k[0],
                                   (v.ravel() - k[5]) * z / k[4], z])
        valid = z > 0
        base = rotation.apply(optical[valid]) + np.array(tf['translation']) * 1000
        points.append(base)
        labels.extend([0 if name.startswith('table') else 1] * len(base))
        statistics[name] = {
            'valid_fraction': float(np.mean(valid)),
            'base_z_median_mm': float(np.median(base[:, 2])),
            'base_z_p05_p95_mm': np.percentile(base[:, 2], [5, 95]).tolist(),
        }
    points, labels = np.concatenate(points), np.asarray(labels)
    fit = parallel_plane_fit(points, labels)
    shifted = parallel_plane_fit(points + [0, 0, 25], labels)
    assert abs(shifted['normal_separation_mm'] - fit['normal_separation_mm']) < 1e-8
    individual = {}
    for label, name in [(0, 'table'), (1, 'lid')]:
        subset = points[labels == label]
        design = np.column_stack([subset[:, :2], np.ones(len(subset))])
        coef = np.linalg.lstsq(design, subset[:, 2], rcond=None)[0]
        individual[name] = {'coefficients_a_b_c': coef.tolist(),
                            'residual_rms_mm': float(np.sqrt(np.mean((subset[:, 2] - design @ coef) ** 2)))}
    lid_median = float(np.median(points[labels == 1, 2]))
    table_median = float(np.median(points[labels == 0, 2]))
    ground_offset = -1000 * frame['transforms_to_base_link_at_depth_stamp']['base_footprint']['translation'][2]
    rows.append({
        'index': index, 'regions': statistics, 'parallel_plane_fit': fit,
        'separate_plane_fits': individual,
        'table_ground_z_original_mm': table_median + ground_offset,
        'lid_ground_z_original_mm': lid_median + ground_offset,
        'table_ground_z_plus25_mm': table_median + ground_offset + 25,
        'lid_ground_z_plus25_mm': lid_median + ground_offset + 25,
    })
values = np.array([r['parallel_plane_fit']['normal_separation_mm'] for r in rows])
summary = {'frames': len(rows), 'separation_mean_mm': float(values.mean()),
           'separation_min_max_mm': [float(values.min()), float(values.max())],
           'separation_sample_std_mm': float(values.std(ddof=1)),
           'difference_from_user_80mm': float(values.mean() - 80),
           'max_rgb_depth_delta_ms': max(f['pair_delta_ms'] for f in capture['frames'])}
for key in ['table_ground_z_original_mm', 'lid_ground_z_original_mm',
            'table_ground_z_plus25_mm', 'lid_ground_z_plus25_mm']:
    summary[key + '_median_over_frames'] = float(np.median([r[key] for r in rows]))
result = {'method': 'Common-normal least-squares fit to tabletop and lid; frame-time TF; no change to robot.',
          'assumptions': ['Selected tabletop and lid regions approximate parallel planes.',
                          'Visible tabletop shares the robot wheel support plane as reported by user.'],
          'limitations': ['Manual ROI choice, surface shape and depth bias remain.',
                          'Small repeated-frame spread does not imply absolute accuracy.',
                          'One pair of planes does not determine full camera extrinsics.'],
          'regions_xyxy': regions, 'sample_stride_pixels': 4,
          'capture_sha256': hashlib.sha256((root / 'capture.json').read_bytes()).hexdigest(),
          'summary': summary, 'frames': rows}
(root / 'analysis.json').write_text(json.dumps(result, indent=2) + '\n')
print(json.dumps(summary, indent=2))
