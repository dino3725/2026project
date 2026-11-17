"""Screen nominal descent/lift geometry and held-can motion; never execute."""

import argparse
import hashlib
import json
from pathlib import Path
import struct
import xml.etree.ElementTree as ET

import numpy as np
from scipy.spatial.transform import Rotation
import yaml

from calibration_plan import fk, JOINTS
from grasp_collision_geometry import cylinder_box_gap, cylinder_lowest_z
from grasp_collision_geometry import rigid_payload, sliced_mesh_bounds
from grasp_collision_geometry import review_segment_clearance
from screen_grasp_path import box_separation, envelopes, trajectory_samples

ROOT = Path(__file__).resolve().parents[2]
CAMERA_SOURCE = ('https://raw.githubusercontent.com/realsenseai/realsense-ros/'
                 'ros2-master/realsense2_description/urdf/_d435.urdf.xacro')


def finger_slices(xml, opening):
    """Build conservative longitudinal boxes using the installed finger meshes."""
    root = ET.fromstring(xml)
    ee = root.find("joint[@name='end_effector_joint']")
    ee_xyz = np.fromstring(ee.find('origin').get('xyz'), sep=' ')
    result = []
    hashes = {}
    for side in ('left', 'right'):
        joint = root.find(f"joint[@name='gripper_{side}_joint']")
        collision = root.find(f"link[@name='gripper_{side}_link']/collision")
        for origin in (joint.find('origin'), collision.find('origin'), ee.find('origin')):
            if not np.allclose(np.fromstring(origin.get('rpy', '0 0 0'), sep=' '), 0):
                raise ValueError('Finger slicing requires reviewed unrotated origins')
        if joint.find('parent').get('link') != ee.find('parent').get('link'):
            raise ValueError('Unreviewed finger parent')
        mesh = collision.find('geometry/mesh')
        path = Path('/opt/ros/humble/share') / mesh.get('filename').removeprefix('package://')
        raw = path.read_bytes()
        count = struct.unpack_from('<I', raw, 80)[0]
        if len(raw) != 84 + count * 50:
            raise ValueError('Unsupported mesh')
        hashes[str(path)] = hashlib.sha256(raw).hexdigest()
        data = np.frombuffer(raw, offset=84, dtype=np.dtype([
            ('normal', '<f4', (3,)), ('vertices', '<f4', (3, 3)), ('attr', '<u2')]))
        vertices = data['vertices'].astype(float) * np.fromstring(mesh.get('scale'), sep=' ')
        vertices += (np.fromstring(joint.find('origin').get('xyz'), sep=' ')
                     + np.fromstring(collision.find('origin').get('xyz'), sep=' ') - ee_xyz
                     + opening * np.fromstring(joint.find('axis').get('xyz'), sep=' '))
        for i, (lower, upper) in enumerate(sliced_mesh_bounds(vertices)):
            result.append((f'finger_{side}_{i}', 'end_effector_link', np.eye(3), lower, upper))
    return result, hashes


def world_box(body, transforms):
    """Transform a local oriented box into base coordinates."""
    name, link, local_r, lower, upper = body
    transform = transforms[link]
    rotation = transform[:3, :3] @ local_r
    return (name, transform[:3, 3] + rotation @ ((lower + upper) / 2),
            rotation, (upper - lower) / 2)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--plan', type=Path, required=True)
    parser.add_argument('--tcp', type=Path, required=True)
    parser.add_argument('--capture', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error('Output exists')
    plan = json.loads(args.plan.read_text())
    capture = json.loads(args.capture.read_text())
    model = ROOT / 'log/measured_model_20261111/measured.urdf'
    xml = model.read_text()
    reference = json.loads(args.tcp.read_text())
    all_bodies = envelopes(xml, reference)
    arm = [b for b in all_bodies if b[0].startswith('link')]
    coarse_camera = [b for b in all_bodies if b[0] == 'camera_envelope'][0]
    mount_path = ROOT / 'src/vision_pkg/config/camera_mount.yaml'
    mount = yaml.safe_load(mount_path.read_text())
    camera_r = Rotation.from_euler('xyz', mount['rpy_rad']).as_matrix()
    center = camera_r.T @ np.array(mount['xyz_m']) + [0., -.0175, 0.]
    half = np.array([.02505, .090, .025]) / 2
    camera_body = ('camera_body_nominal', 'end_effector_link', camera_r, center-half, center+half)
    opened, mesh_hashes = finger_slices(xml, .019)
    closing_envelope = []
    for name, link, rotation, lower, upper in opened:
        # Full legal opening range, including closure. No physical command.
        shift = np.array([0., -.029 if name.startswith('finger_left_') else .029, 0.])
        closing_envelope.append((name, link, rotation, np.minimum(lower, lower+shift),
                                 np.maximum(upper, upper+shift)))
    profile = plan['profile']
    radius, height, table_z = (profile[k] for k in
                              ('object_radius_m', 'object_height_m', 'support_z_base_m'))
    can_center = np.r_[plan['axis_xy_m'], table_z+height/2]
    # Conservative XY work region; measured box-top height. No arbitrary Z inflation
    # for an object that starts resting on this plane.
    table_center, table_half = np.array([.325, 0., table_z/2]), np.array([.225, .25, table_z/2])
    q = capture['frames'][-1]['joints_latest_radians']
    width = sum(abs(p['center_at_zero_in_tool_m'][1])
                + q['gripper_'+p['side']+'_joint'] for p in reference['pads'])
    report = dict(executed=False, execution_authorized=False,
                  current_nominal_inner_width_m=width,
                  planned_open_inner_width_m=reference['nominal_open_inner_width_m'],
                  opening_required_before_descent=width < 2*radius,
                  nominal_camera_source=CAMERA_SOURCE,
                  nominal_camera_size_xyz_m=(half*2).tolist(),
                  nominal_camera_center_in_camera_link_m=[0., -.0175, 0.],
                  assumptions=[
                      'Nominal meshes, camera-link mounting and upright rigid can; not measured clearance.',
                      'Finger mesh slices enclose the mesh solid; positive separating gap proves only those bounds are separated.',
                      'Nonpositive separating gap is possible overlap, not proof of physical collision.',
                      'Camera body source excludes custom mount/cables; original broad envelope is reported separately.',
                      'Descent uses fully open fingers, not current feedback opening.',
                      'Lift attaches the can rigidly at the first lift pose, with no slip.',
                      'Finger/can contact is intentional after grasp; fingers/table remain checked.',
                      'Sampled model review is not full environment, self-collision or physical safety certification.',
                  ], input_sha256={str(p): hashlib.sha256(p.read_bytes()).hexdigest()
                                   for p in (args.plan, args.tcp, args.capture, model,
                                             mount_path, Path(__file__),
                                             Path(__file__).with_name('grasp_collision_geometry.py'))},
                  finger_mesh_sha256=mesh_hashes, candidates=[])
    for index, row in enumerate(plan['candidates']):
        if not row.get('planning_chain_accepted'):
            continue
        candidate = dict(index=index, top_inset_m=row['top_inset_m'], stages=[])
        for leg, path in zip(('descent', 'lift'), row['paths'][1:]):
            samples, bound = trajectory_samples(path['trajectory']['joint_trajectory']['points'])
            initial = fk(xml, dict(zip(JOINTS, samples[0])))
            minima, hits = {}, {}
            can_low, records = [], []
            for sample, joints in enumerate(samples):
                angles = dict(zip(JOINTS, joints))
                fingers = opened if leg == 'descent' else closing_envelope
                bodies = arm + fingers + [camera_body, coarse_camera]
                transforms = {link: fk(xml, angles, link) for link in {b[1] for b in bodies}}
                if leg == 'lift':
                    can, axis = rigid_payload(initial, transforms['end_effector_link'], can_center)
                else:
                    can, axis = can_center, np.array([0., 0., 1.])
                can_low.append(cylinder_lowest_z(can, axis, radius, height)-table_z)
                for body in bodies:
                    name, center, rotation, half = world_box(body, transforms)
                    category = ('finger' if name.startswith('finger_') else name)
                    pairs = [('table', box_separation(center, rotation, half, table_center, table_half))]
                    if leg == 'descent' or not name.startswith('finger_'):
                        pairs.append(('can', cylinder_box_gap(can, axis, radius, height,
                                                             center, rotation, half)))
                    for obstacle, gap in pairs:
                        key = category+'/'+obstacle
                        minima[key] = min(minima.get(key, float('inf')), gap)
                        if gap < -1e-9:
                            hits[key] = hits.get(key, 0)+1
                            if len(records) < 12:
                                records.append(dict(sample=sample, body=name,
                                                    obstacle=obstacle, separating_gap_m=gap))
            # Lift finger/table bounds cover the full legal closure range.
            # Intended finger/can contact and gripping success remain unverified.
            candidate['stages'].append(dict(
                leg=leg, samples=len(samples), min_gaps_m=minima,
                possible_overlap_counts=hits, first_possible_overlaps=records,
                payload_bottom_above_table_m=dict(min=min(can_low), final=can_low[-1]),
                max_between_sample_motion_bound_m=bound,
                grasp_closure_certified=False))
        candidate['geometry_review'] = review_segment_clearance(candidate['stages'])
        candidate['total_joint_travel_rad'] = row['total_joint_travel_rad']
        report['candidates'].append(candidate)
        print('Reviewed candidate', index, candidate['geometry_review'], flush=True)
    accepted = [c for c in report['candidates'] if c['geometry_review']['nominal_geometry_accepted']]
    report['selected_nominal_candidate_index'] = None
    if accepted:
        chosen = min(accepted, key=lambda c: (
            -c['geometry_review']['min_nominal_separating_gap_m'],
            c['total_joint_travel_rad'], c['index']))
        report['selected_nominal_candidate_index'] = chosen['index']
    report['selection_scope'] = ('Nominal modeled bodies only, larger separation before shorter joint travel; '
                                 'mount/cables/real tracking/closure remain unverified.')
    args.output.write_text(json.dumps(report, indent=2, allow_nan=False)+'\n')
    print(json.dumps(report['candidates'], indent=2))


if __name__ == '__main__':
    main()
