"""Compare recorded joint/model geometry with reported physical contact.

No ROS calls, fitted offsets or runtime parameter changes. Model envelopes are
conditional on the installed meshes matching the physical fingers.
"""

import argparse
import hashlib
import itertools
import json
from pathlib import Path

import numpy as np

from calibration_plan import fk


def transformed_bounds(lower, upper, pose):
    """Transform all box corners, retaining rotation-dependent lowest height."""
    lower, upper, pose = [np.asarray(x, dtype=float) for x in (lower, upper, pose)]
    if lower.shape != (3,) or upper.shape != (3,) or pose.shape != (4, 4):
        raise ValueError('Expected 3D bounds and a 4x4 transform')
    if not all(np.isfinite(x).all() for x in (lower, upper, pose)):
        raise ValueError('Nonfinite geometry')
    if np.any(lower > upper):
        raise ValueError('Inverted bounds')
    r = pose[:3, :3]
    if (not np.allclose(r.T @ r, np.eye(3), atol=1e-6)
            or not np.isclose(np.linalg.det(r), 1., atol=1e-6)
            or not np.allclose(pose[3], [0, 0, 0, 1])):
        raise ValueError('Invalid rigid transform')
    corners = np.array(list(itertools.product(*zip(lower, upper))))
    points = corners @ r.T + pose[:3, 3]
    return np.array([points.min(axis=0), points.max(axis=0)])


def finger_geometry(pose, reference, joint_positions, object_top_z):
    """Use each recorded finger opening; do not silently assume fully open."""
    if not np.isfinite(object_top_z):
        raise ValueError('Invalid object height')
    rows, centers = [], []
    for pad in reference['pads']:
        name = 'gripper_' + pad['side'] + '_joint'
        opening = float(joint_positions[name])
        if not np.isfinite(opening):
            raise ValueError('Invalid finger feedback')
        axis = np.asarray(pad['axis'])
        lower, upper = np.asarray(pad['open_mesh_bounds_in_tool_m'])
        shift = axis * (opening - pad['open_joint_m'])
        full = transformed_bounds(lower + shift, upper + shift, pose)
        center = np.asarray(pad['center_at_zero_in_tool_m']) + axis * opening
        size = np.asarray(pad['pad_size_m'])
        contact = transformed_bounds(center - size / 2, center + size / 2, pose)
        centers.append(center)
        rows.append({
            'side': pad['side'], 'recorded_opening_m': opening,
            'full_mesh_envelope_base_m': full.tolist(),
            'contact_pad_envelope_base_m': contact.tolist(),
            'model_full_envelope_gap_above_top_m': float(full[0, 2] - object_top_z),
            'model_contact_pad_gap_above_top_m': float(contact[0, 2] - object_top_z),
        })
    return {'fingers': rows,
            'nominal_inner_width_m': float(np.linalg.norm(centers[0] - centers[1])),
            'independent_physical_measurement': False}


def main():
    """Produce reproducible geometry evidence from a saved approach capture."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--capture', type=Path, required=True)
    parser.add_argument('--model', type=Path, required=True)
    parser.add_argument('--reference', type=Path, required=True)
    parser.add_argument('--object-top-z-base', type=float, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    reference = json.loads(args.reference.read_text())
    capture = json.loads(args.capture.read_text())
    xml = args.model.read_text()
    model_hash = hashlib.sha256(args.model.read_bytes()).hexdigest()
    if model_hash not in reference['input_sha256'].values():
        raise ValueError('Finger reference was not derived from this model')
    rows = []
    for frame in capture['frames']:
        joints = frame['joints_latest_radians']
        pose = fk(xml, joints)
        row = finger_geometry(pose, reference, joints, args.object_top_z_base)
        row.update(index=frame['index'], sensor_fk_tool_m=pose[:3, 3].tolist())
        rows.append(row)
    if not rows:
        raise ValueError('Empty capture')
    full = [min(f['model_full_envelope_gap_above_top_m'] for f in r['fingers'])
            for r in rows]
    pads = [min(f['model_contact_pad_gap_above_top_m'] for f in r['fingers'])
            for r in rows]
    summary = {
        'samples': len(rows),
        'model_full_envelope_gap_range_mm': [1000 * min(full), 1000 * max(full)],
        'model_contact_pad_gap_range_mm': [1000 * min(pads), 1000 * max(pads)],
        'nominal_inner_width_range_mm': [
            1000 * min(r['nominal_inner_width_m'] for r in rows),
            1000 * max(r['nominal_inner_width_m'] for r in rows)],
    }
    report = {
        'summary': summary, 'rows': rows, 'object_top_z_base_m': args.object_top_z_base,
        'limitations': [
            'Recorded encoders plus URDF, not physical fingertip measurements.',
            'Latest joint samples are not necessarily synchronized to image stamps.',
            'Positive model gap contradicts reported contact only conditional on '
            'object height, model, joint references and mounting being correct.',
            'A contradiction is not an estimated correction or evidence of a unique cause.',
        ],
        'input_sha256': {str(p): hashlib.sha256(p.read_bytes()).hexdigest()
                         for p in (args.capture, args.model, args.reference, Path(__file__))},
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open('x') as stream:
        json.dump(report, stream, indent=2, allow_nan=False)
        stream.write('\n')
    print(json.dumps(summary, indent=2))


if __name__ == '__main__':
    main()
