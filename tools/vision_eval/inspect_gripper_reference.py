"""Extract a nominal contact reference from this robot's URDF and pad meshes."""

import argparse
import hashlib
import json
from pathlib import Path
import struct
import xml.etree.ElementTree as ET

import numpy as np


def inspect(model, package_share):
    """Reject unfamiliar pad geometry rather than guessing a contact point."""
    root = ET.parse(model).getroot()
    ee = root.find("joint[@name='end_effector_joint']")
    ee_origin = np.fromstring(ee.find('origin').get('xyz'), sep=' ')
    if not np.allclose(np.fromstring(ee.find('origin').get('rpy', '0 0 0'), sep=' '), 0):
        raise ValueError('Rotated tool reference requires explicit handling')
    hashes = {str(model): hashlib.sha256(model.read_bytes()).hexdigest()}
    pads = []
    for side in ('left', 'right'):
        joint = root.find(f"joint[@name='gripper_{side}_joint']")
        link = root.find(f"link[@name='gripper_{side}_link']")
        mesh = link.find('collision/geometry/mesh')
        relative = mesh.get('filename').split('package://turtlebot3_manipulation_description/')
        if len(relative) != 2:
            raise ValueError('Unexpected mesh package')
        path = package_share / relative[1]
        raw = path.read_bytes()
        hashes[str(path)] = hashlib.sha256(raw).hexdigest()
        count = struct.unpack_from('<I', raw, 80)[0]
        if len(raw) != 84 + 50 * count:
            raise ValueError('Expected binary STL with complete triangles')
        triangles = np.frombuffer(raw, offset=84, dtype=np.dtype([
            ('normal', '<f4', (3,)), ('vertices', '<f4', (3, 3)), ('attr', '<u2')]))
        vertices = triangles['vertices'].astype(float)
        normals = np.cross(vertices[:, 1] - vertices[:, 0],
                           vertices[:, 2] - vertices[:, 0])
        areas = np.linalg.norm(normals, axis=1) / 2
        # This mesh's distal inward rectangular pad lies at local Y=0.
        mask = ((np.abs(vertices[:, :, 1]).max(axis=1) < .0001)
                & (areas > 200) & (vertices[:, :, 0].min(axis=1) > 20))
        if mask.sum() != 2 or not np.isclose(areas[mask].sum(), 525, atol=.01):
            raise ValueError('Pad geometry differs from reviewed 35x15 mm rectangle')
        scale = np.fromstring(mesh.get('scale'), sep=' ')
        pad = vertices[mask].reshape(-1, 3) * scale
        origin = np.fromstring(joint.find('origin').get('xyz'), sep=' ')
        if joint.find('parent').get('link') != ee.find('parent').get('link'):
            raise ValueError('Finger and tool must share the reviewed parent link')
        collision_origin = np.fromstring(
            link.find('collision/origin').get('xyz', '0 0 0'), sep=' ')
        axis = np.fromstring(joint.find('axis').get('xyz'), sep=' ')
        limits = joint.find('limit')
        for element in (joint.find('origin'), link.find('collision/origin')):
            if not np.allclose(np.fromstring(element.get('rpy', '0 0 0'), sep=' '), 0):
                raise ValueError('Nonzero mesh rotation requires explicit handling')
        center = (pad.min(axis=0) + pad.max(axis=0)) / 2 + origin + collision_origin - ee_origin
        open_vertices = (vertices.reshape(-1, 3) * scale + origin + collision_origin
                         - ee_origin + axis * float(limits.get('upper')))
        pads.append({'side': side, 'center_at_zero_in_tool_m': center.tolist(),
                     'axis': axis.tolist(), 'open_joint_m': float(limits.get('upper')),
                     'pad_size_m': (pad.max(axis=0)-pad.min(axis=0)).tolist(),
                     'open_mesh_bounds_in_tool_m': [open_vertices.min(axis=0).tolist(),
                                                    open_vertices.max(axis=0).tolist()],
                     'tip_x_in_tool_m': float(vertices[:, :, 0].max()*scale[0]
                                              + origin[0]-ee_origin[0])})
    centers = np.array([p['center_at_zero_in_tool_m'] for p in pads])
    opened = np.array([np.array(p['center_at_zero_in_tool_m'])
                       + np.array(p['axis'])*p['open_joint_m'] for p in pads])
    return {
        'model_only': True, 'hardware_tcp_validated': False,
        'assumptions': ['Installed meshes match physical finger pads.',
                        'Desired contact is at the middle of the pad rectangle.',
                        'Pad compression and attachment differences are not measured.'],
        'input_sha256': hashes, 'pads': pads,
        'tcp_in_tool_m': centers.mean(axis=0).tolist(),
        'nominal_open_inner_width_m': float(abs(opened[0, 1]-opened[1, 1])),
        'nominal_coke_width_m': .053,
        'nominal_centered_side_clearance_m': float((abs(opened[0, 1]-opened[1, 1])-.053)/2),
    }


def main():
    """Save a reviewable model calculation; send no commands."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--model', type=Path, required=True)
    parser.add_argument('--package-share', type=Path,
                        default=Path('/opt/ros/humble/share/turtlebot3_manipulation_description'))
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    result = inspect(args.model, args.package_share)
    with args.output.open('x') as stream:
        json.dump(result, stream, indent=2, allow_nan=False)
        stream.write('\n')
    print(json.dumps({key: result[key] for key in (
        'tcp_in_tool_m', 'nominal_open_inner_width_m',
        'nominal_centered_side_clearance_m', 'hardware_tcp_validated')}, indent=2))


if __name__ == '__main__':
    main()
