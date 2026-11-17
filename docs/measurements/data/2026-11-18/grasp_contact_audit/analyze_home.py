"""Read-only, session-specific audit of reviewed box ROIs and saved YOLO frames."""
from pathlib import Path
import hashlib
import sys
import json
import cv2
import numpy as np
from ultralytics import YOLO
sys.path.insert(0, 'tools/vision_eval')
from analyze_grasp_observations import analyze, rotation_xyzw
from rgbd_refinement import fit_plane
root = Path('log/grasp_contact_audit_20261118')
capture_dir = root / 'home_capture_v2'
source = capture_dir / 'capture.json'
capture = json.loads(source.read_text())
model = YOLO('best.pt')
regions = {'box_right': [700, 490, 900, 610], 'box_front': [430, 640, 820, 690]}
rows = []
hashes = {str(p): hashlib.sha256(p.read_bytes()).hexdigest()
          for p in [source, Path('best.pt'), Path(__file__)]}
seen = set()
for frame in capture['frames']:
    index = frame['index']
    stamp = frame['depth_stamp_ns']
    if stamp in seen:
        raise ValueError('Duplicate depth observation')
    seen.add(stamp)
    rgb_path = capture_dir / f'{index:03d}_rgb.png'
    depth_path = capture_dir / f'{index:03d}_depth.png'
    for path in (rgb_path, depth_path):
        hashes[str(path)] = hashlib.sha256(path.read_bytes()).hexdigest()
    rgb, depth = cv2.imread(str(rgb_path)), cv2.imread(str(depth_path), -1)
    info = frame['intrinsics']
    if np.any(info['d']) or info['model'] != 'plumb_bob':
        raise ValueError('Expected recorded rectified depth intrinsics')
    k = np.array(info['k']).reshape(3, 3)
    tf = frame['transforms_to_base_link_at_depth_stamp'][frame['optical_frame']]
    rotation = rotation_xyzw(tf['quaternion'])
    translation = np.array(tf['translation'])
    row = {'index': index, 'depth_stamp_ns': stamp, 'regions': {}}
    for name, rect in regions.items():
        x0, y0, x1, y1 = rect
        v, u = np.mgrid[y0:y1:4, x0:x1:4]
        z = depth[v, u].ravel() * .001
        keep = (z > .15) & (z < 1.2)
        if keep.mean() < .8:
            raise ValueError('Insufficient depth in reviewed box region')
        z, u, v = z[keep], u.ravel()[keep], v.ravel()[keep]
        camera = np.c_[(u-k[0, 2])*z/k[0, 0], (v-k[1, 2])*z/k[1, 1], z]
        base = camera @ rotation.T + translation
        plane = fit_plane(base)
        row['regions'][name] = {
            'valid_fraction': float(keep.mean()),
            'median_z_base_m': float(np.median(base[:, 2])),
            'plane': {key: value.tolist() if isinstance(value, np.ndarray) else value
                      for key, value in plane.items()},
        }
        cv2.rectangle(rgb, (x0, y0), (x1, y1), (0, 255, 255), 2)
    result = model.predict(str(rgb_path), device='cpu', verbose=False)[0]
    detections = []
    for box in result.boxes:
        name, confidence = result.names[int(box.cls[0])], float(box.conf[0])
        if name == 'coke' and confidence >= .5:
            detections.append(dict(class_name=name, confidence=confidence,
                                   **dict(zip(['x1', 'y1', 'x2', 'y2'], box.xyxy[0].tolist()))))
    detection = max(detections, key=lambda d: d['confidence']) if detections else None
    meta = dict(detection=detection, intrinsics_fx_fy_cx_cy=[k[0, 0], k[1, 1], k[0, 2], k[1, 2]],
                depth_encoding='16UC1', depth_scale=.001,
                camera_to_base=dict(translation=tf['translation'], quaternion_xyzw=tf['quaternion']),
                rgb_stamp_ns=frame['rgb_stamp_ns'], depth_stamp_ns=stamp)
    row['inference_metadata'] = meta
    row['geometry'] = analyze(meta, depth, .07, .134, .0265)
    rows.append(row)
    if index == 1:
        cv2.imwrite(str(root / 'reviewed_home_regions.png'), rgb)
summary = {}
for name in regions:
    z = np.array([r['regions'][name]['median_z_base_m'] for r in rows]) * 1000
    summary[name] = dict(median_z_base_mean_mm=float(z.mean()),
                         median_z_base_range_mm=[float(z.min()), float(z.max())],
                         expected_z_base_mm=70., mean_difference_mm=float(z.mean()-70.))
axes = np.array([r['geometry']['axis']['axis_xy_m'] for r in rows
                 if r['geometry']['axis'] and r['geometry']['axis']['accepted']])
summary['axis_accepted'] = len(axes)
if len(axes):
    summary['axis_mean_mm'] = (1000*axes.mean(axis=0)).tolist()
    summary['axis_peak_to_peak_mm'] = (1000*np.ptp(axes, axis=0)).tolist()
report = dict(summary=summary, rows=rows, regions_xyxy=regions, input_sha256=hashes,
              limitations=['Box ROIs visually reviewed at HOME only, not segmentation.',
                           'Ten unique stationary frames are not ten independent poses.',
                           'No independent object XY measurement in this HOME capture.',
                           'Earlier contact could have displaced the can; cross-pose differences '
                           'cannot be assigned solely to calibration.',
                           'No runtime offsets fitted or applied.'])
output = Path('docs/measurements/data/2026-11-18/grasp_contact_audit/home_observation.json')
with output.open('x') as stream:
    json.dump(report, stream, indent=2, allow_nan=False)
    stream.write('\n')
print(json.dumps(summary, indent=2))
