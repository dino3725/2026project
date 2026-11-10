"""Offline diagnostic using hand-selected visible box-lid patches; no ROS."""

import hashlib
import json
from pathlib import Path

import cv2
import numpy as np
from scipy.spatial.transform import Rotation


root = Path(__file__).resolve().parent
session = json.loads((root / 'session.json').read_text())
k = session['camera_info']['k']
tf = session['camera_tf_latest']
rotation = Rotation.from_quat(tf['quaternion'])
translation = np.asarray(tf['translation'])
patches = [(500, 550), (650, 600), (900, 550), (500, 400), (900, 400)]
rows = []
for trial in (1, 2, 3):
    image = cv2.imread(str(root / f'{trial:03d}_depth.png'), cv2.IMREAD_UNCHANGED)
    for u, v in patches:
        values = image[v - 3:v + 4, u - 3:u + 4]
        depth = float(np.median(values[values > 0])) / 1000
        optical = [(u - k[2]) * depth / k[0], (v - k[5]) * depth / k[4], depth]
        base = (rotation.apply(optical) + translation) * 1000
        rows.append({
            'trial': trial, 'pixel_uv': [u, v], 'depth_mm': depth * 1000,
            'base_xyz_mm': base.tolist(), 'z_plus25_mm': float(base[2] + 25),
            'expected_base_z_mm': 70, 'z_plus25_minus_expected_mm': float(base[2] + 25 - 70),
        })
result = {
    'method': 'Hand-selected five visible lid pixels per saved depth image; 7x7 valid-depth median; pinhole deprojection; saved pre-trial TF.',
    'limitations': [
        'Snapshot depth/RGB/debug stamps differ; robot and box were held static.',
        'Saved camera TF is latest at session initialization, not at each image stamp.',
        'Patch selection is manual; lid curvature, depth noise and user height uncertainty remain.',
        'No plane fit or full extrinsic calibration; these differences are diagnostic, not correction parameters.',
    ],
    'inputs_sha256': {name: hashlib.sha256((root / name).read_bytes()).hexdigest()
                      for name in ['session.json', '001_depth.png', '002_depth.png', '003_depth.png']},
    'box_top_above_support_plane_mm': 80,
    'assumed_base_link_above_support_plane_mm': 10,
    'user_camera_lens_height_mm_approximate': 426,
    'live_model_optical_frame_height_above_support_plane_mm': float(translation[2] * 1000 + 10),
    'model_with_inferred_25mm_shift_height_mm': float(translation[2] * 1000 + 35),
    'rows': rows,
}
(root / 'box_plane_diagnostic.json').write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
print(json.dumps({
    'patch_count': len(rows),
    'z_raw_range_mm': [min(r['base_xyz_mm'][2] for r in rows), max(r['base_xyz_mm'][2] for r in rows)],
    'z_plus25_range_mm': [min(r['z_plus25_mm'] for r in rows), max(r['z_plus25_mm'] for r in rows)],
    'expected_base_z_mm': 70,
    'camera_height_model_mm': result['live_model_optical_frame_height_above_support_plane_mm'],
    'camera_height_model_plus25_mm': result['model_with_inferred_25mm_shift_height_mm'],
    'camera_height_user_mm_approximate': 426,
}, indent=2))
