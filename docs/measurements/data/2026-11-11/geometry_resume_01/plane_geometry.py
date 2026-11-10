"""Assess camera/table geometry and held-out regions, without applying TF."""
import json
from pathlib import Path

import cv2
import numpy as np
from scipy.spatial.transform import Rotation


root = Path(__file__).resolve().parent
capture = json.loads((root / 'capture.json').read_text())
analysis = json.loads((root / 'analysis.json').read_text())
# Regions not used to fit either plane. Inspected in the current RGB image.
held_out = {'table_left': [140, 500, 260, 540],
            'table_right': [1140, 550, 1220, 595],
            'lid_front': [460, 585, 900, 615]}
rows = []
for frame, measured in zip(capture['frames'], analysis['frames']):
    coeff = np.array(measured['separate_plane_fits']['table']['coefficients_a_b_c'])
    normal = np.array([-coeff[0], -coeff[1], 1.])
    length = np.linalg.norm(normal)
    normal /= length
    plane_offset = coeff[2] / length
    tf = frame['transforms_to_base_link_at_depth_stamp'][frame['optical_frame']]
    camera = np.array(tf['translation']) * 1000
    height = float(normal @ camera - plane_offset)
    row = {'index': frame['index'],
           'table_normal_in_live_base': normal.tolist(),
           'table_tilt_from_live_base_degrees': float(np.degrees(np.arccos(normal[2]))),
           'camera_to_table_normal_distance_mm': height,
           'camera_model_support_height_plus25_mm': float(camera[2] + 35),
           'table_fit_rms_mm': measured['separate_plane_fits']['table']['residual_rms_mm'],
           'held_out': {}}
    depth = cv2.imread(str(root / f"{frame['index']:03d}_depth.png"), -1)
    k = frame['intrinsics']['k']
    rotation = Rotation.from_quat(tf['quaternion'])
    for name, (x0, y0, x1, y1) in held_out.items():
        v, u = np.mgrid[y0:y1:4, x0:x1:4]
        z = depth[v, u].ravel().astype(float)
        optical = np.column_stack([(u.ravel() - k[2]) * z / k[0],
                                   (v.ravel() - k[5]) * z / k[4], z])
        points = rotation.apply(optical[z > 0]) + camera
        heights = points @ normal - plane_offset
        row['held_out'][name] = {
            'valid_fraction': float(np.mean(z > 0)),
            'table_relative_height_median_mm': float(np.median(heights)),
            'table_relative_height_p05_p95_mm': np.percentile(heights, [5, 95]).tolist(),
        }
    rows.append(row)

summary = {}
for field in ['table_tilt_from_live_base_degrees', 'camera_to_table_normal_distance_mm',
              'camera_model_support_height_plus25_mm', 'table_fit_rms_mm']:
    values = np.array([r[field] for r in rows])
    summary[field] = {'mean': float(values.mean()), 'min': float(values.min()),
                      'max': float(values.max())}
for name in held_out:
    values = np.array([r['held_out'][name]['table_relative_height_median_mm'] for r in rows])
    summary[name] = {'median_height_mean_mm': float(values.mean()),
                     'median_height_range_mm': [float(values.min()), float(values.max())]}
result = {'applied_to_robot': False,
          'interpretation': 'Plane-relative height diagnostic; not a complete camera extrinsic solution.',
          'limitations': ['One fixed pose cannot separate arm kinematic errors from mount errors.',
                          'Plane alone leaves in-plane translation and rotation unobserved.',
                          'Camera-to-plane distance is independent of the chosen rigid base transform.',
                          'Height and tilt use manually selected approximately planar tabletop regions.'],
          'held_out_regions_xyxy': held_out, 'summary': summary, 'frames': rows}
(root / 'plane_geometry.json').write_text(json.dumps(result, indent=2) + '\n')
print(json.dumps(summary, indent=2))
