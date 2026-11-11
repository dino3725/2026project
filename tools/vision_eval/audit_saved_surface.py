"""Audit a frozen candidate on earlier box/table captures without fitting it."""

import argparse
import json
from pathlib import Path

import cv2
import numpy as np
from scipy.spatial.transform import Rotation

from calibration_plan import fk
from diagnose_pose_residuals import sha
from handeye import transform
from refine_saved_rgbd import write_json


def region_heights(depth, box, k, camera):
    """Measure median support-relative z in a previously reviewed image region."""
    x0, y0, x1, y1 = box
    vv, uu = np.mgrid[y0:y1:4, x0:x1:4]
    z = depth[vv, uu].ravel().astype(float) * .001
    valid = (z > .2) & (z < 1.2)
    if valid.mean() < .8:
        raise ValueError('Insufficient depth in previously reviewed surface region')
    xyz = np.c_[(uu.ravel() - k[0, 2]) * z / k[0, 0],
                (vv.ravel() - k[1, 2]) * z / k[1, 1], z][valid]
    world = xyz @ camera[:3, :3].T + camera[:3, 3]
    return float(np.median(world[:, 2]) * 1000 + 10)


def main():
    """Replay known regions only after candidate selection; request no new capture."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--capture', type=Path, required=True)
    parser.add_argument('--profile', type=Path, required=True)
    parser.add_argument('--model', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    capture_path = args.capture / 'capture.json'
    regions_path = args.capture / 'plane_geometry.json'
    capture = json.loads(capture_path.read_text())
    regions = json.loads(regions_path.read_text())['held_out_regions_xyxy']
    profile = json.loads(args.profile.read_text())
    if sha(args.model) != profile['model_sha256']:
        raise ValueError('Candidate and audit robot model differ')
    mount = np.array(profile['T_ee_optical'])
    optical = np.linalg.inv(profile['T_ee_camera']) @ mount
    nominal = transform(np.eye(3), [-.065, .0195, .052]) @ optical
    xml = args.model.read_text()
    rows = []
    hashes = {str(p): sha(p) for p in [capture_path, regions_path, args.profile, args.model]}
    for frame in capture['frames']:
        ee = fk(xml, frame['joints_latest_radians'])
        old = frame['transforms_to_base_link_at_depth_stamp'][frame['optical_frame']]
        old = transform(Rotation.from_quat(old['quaternion']).as_matrix(), old['translation'])
        # This historical capture preceded the reviewed +25 mm arm support model.
        old[2, 3] += .025
        mismatch_mm = np.linalg.norm((ee @ nominal)[:3, 3] - old[:3, 3]) * 1000
        mismatch_deg = Rotation.from_matrix(old[:3, :3].T @ (ee @ nominal)[:3, :3]).magnitude()
        if mismatch_mm > .5 or np.rad2deg(mismatch_deg) > .2:
            raise ValueError('Historical TF does not match the reviewed model-height change')
        k = np.array(frame['intrinsics']['k']).reshape(3, 3)
        if (not np.array_equal(k, profile['intrinsics']['k'])
                or np.any(np.array(frame['intrinsics']['d']) != 0)):
            raise ValueError('Audit intrinsics differ from candidate depth path')
        path = args.capture / f'{frame["index"]:03d}_depth.png'
        hashes[str(path)] = sha(path)
        depth = cv2.imread(str(path), cv2.IMREAD_UNCHANGED)
        info = frame['intrinsics']
        if (depth is None or depth.dtype != np.uint16
                or depth.shape != (info['height'], info['width'])):
            raise ValueError('Invalid historical depth')
        row = {'index': frame['index'], 'fk_tf_difference_mm': float(mismatch_mm)}
        for label, m in [('nominal', nominal), ('selected_candidate', mount)]:
            row[label] = {name: region_heights(depth, box, k, ee @ m)
                          for name, box in regions.items()}
        rows.append(row)
    summary = {label: {region: {
        'mean_height_mm': float(np.mean([r[label][region] for r in rows])),
        'min_height_mm': float(min(r[label][region] for r in rows)),
        'max_height_mm': float(max(r[label][region] for r in rows))}
        for region in regions} for label in ['nominal', 'selected_candidate']}
    write_json(args.output, {
        'status': 'offline_cross_dataset_audit_no_refit', 'rows': rows, 'summary': summary,
        'expected_approximate_heights_mm': {'table_left': 0, 'table_right': 0, 'lid_front': 80},
        'input_sha256': hashes, 'implementation_sha256': sha(Path(__file__)),
        'caveat': 'Earlier inspected 10-frame single-pose capture, not 10 independent poses. '
        'Model height correction verified against historical TF. Mount stability between '
        'sessions is assumed. No parameters, masks, or candidate choice fitted to this audit.'})
    print(json.dumps(summary, indent=2))


if __name__ == '__main__':
    main()
