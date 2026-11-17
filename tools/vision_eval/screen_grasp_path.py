"""Conservatively screen saved arm paths with oriented mesh bounding boxes."""

import argparse
import hashlib
import json
from pathlib import Path
import struct
import xml.etree.ElementTree as ET

import numpy as np
from scipy.spatial.transform import Rotation
import yaml

from calibration_plan import JOINTS, fk

ROOT = Path(__file__).resolve().parents[2]


def trajectory_samples(points):
    """Sample quintic segments and bound motion between samples from derivatives."""
    samples, motion_bound = [], 0.
    for first, second in zip(points[:-1], points[1:]):
        def seconds(point):
            t = point['time_from_start']
            return t['sec'] + t['nanosec'] * 1e-9
        duration = seconds(second) - seconds(first)
        if duration <= 0:
            raise ValueError('Non-increasing trajectory time')
        q0, q1 = np.asarray(first['positions']), np.asarray(second['positions'])
        v0, v1 = np.asarray(first['velocities']), np.asarray(second['velocities'])
        a0, a1 = np.asarray(first['accelerations']), np.asarray(second['accelerations'])
        if any(x.shape != (4,) or not np.isfinite(x).all() for x in (q0,q1,v0,v1,a0,a1)):
            raise ValueError('Expected complete finite spline data')
        c = np.zeros((6, 4))
        c[:3] = [q0, v0*duration, a0*duration**2/2]
        c[3:] = np.linalg.solve([[1,1,1],[3,4,5],[6,12,20]], np.array([
            q1-c[:3].sum(axis=0), v1*duration-c[1]-2*c[2], a1*duration**2-2*c[2]]))
        divisions = max(1, int(np.ceil(duration/.02)),
                        int(np.ceil(np.max(abs(q1-q0))/np.deg2rad(.25))))
        fractions = np.linspace(0,1,divisions+1)
        samples.extend(np.polynomial.polynomial.polyval(fractions, c).T)
        derivative = c[1:] * np.arange(1,6)[:,None]
        second_derivative = derivative[1:] * np.arange(1,5)[:,None]
        roots = [np.polynomial.polynomial.polyroots(second_derivative[:,j]) for j in range(4)]
        for lo, hi in zip(fractions[:-1], fractions[1:]):
            slopes = []
            for j in range(4):
                candidates = [lo,hi] + [float(z.real) for z in roots[j]
                                        if abs(z.imag)<1e-8 and lo<z.real<hi]
                slopes.append(np.max(np.abs(np.polynomial.polynomial.polyval(candidates, derivative[:,j]))))
            # 0.8m exceeds the modeled arm/camera distance from each arm axis.
            motion_bound = max(motion_bound, .8*sum(slopes)*(hi-lo))
    return np.asarray(samples), float(motion_bound)


def box_separation(center, rotation, half_size, obstacle_center, obstacle_half):
    """Return maximum separating-axis gap; nonpositive means possible overlap."""
    axes = np.vstack((np.eye(3), rotation.T,
                      np.cross(np.eye(3)[:, None, :], rotation.T[None, :, :]).reshape(-1, 3)))
    norms = np.linalg.norm(axes, axis=1)
    axes = axes[norms > 1e-9] / norms[norms > 1e-9, None]
    delta = np.asarray(center) - obstacle_center
    radii = np.abs(axes @ rotation) @ half_size + np.abs(axes) @ obstacle_half
    return float(np.max(np.abs(axes @ delta) - radii))


def envelopes(xml, tcp_info):
    """Enclose full collision meshes and a conservative camera envelope."""
    root = ET.fromstring(xml)
    result = []
    for name in ('link2', 'link3', 'link4', 'link5'):
        collision = root.find(f"link[@name='{name}']/collision")
        mesh = collision.find('geometry/mesh')
        path = Path('/opt/ros/humble/share') / mesh.get('filename').removeprefix('package://')
        raw = path.read_bytes()
        count = struct.unpack_from('<I', raw, 80)[0]
        if len(raw) != 84 + 50 * count:
            raise ValueError('Unsupported mesh')
        triangles = np.frombuffer(raw, offset=84, dtype=np.dtype([
            ('normal', '<f4', (3,)), ('vertices', '<f4', (3, 3)), ('attr', '<u2')]))
        vertices = triangles['vertices'].reshape(-1, 3) * np.fromstring(mesh.get('scale'), sep=' ')
        origin = collision.find('origin')
        rotation = Rotation.from_euler('xyz', np.fromstring(origin.get('rpy', '0 0 0'), sep=' ')).as_matrix()
        translation = np.fromstring(origin.get('xyz', '0 0 0'), sep=' ')
        vertices = vertices @ rotation.T + translation
        result.append((name, name, np.eye(3), vertices.min(axis=0), vertices.max(axis=0)))
    for pad in tcp_info['pads']:
        lower, upper = np.array(pad['open_mesh_bounds_in_tool_m'])
        # Enclose both open and closed fingers, so no physical opening is implied.
        delta = np.array(pad['axis']) * (.019 - (-.010))
        result.append(('finger_' + pad['side'], 'end_effector_link', np.eye(3),
                       np.minimum(lower, lower-delta), np.maximum(upper, upper-delta)))
    mount = yaml.safe_load((ROOT / 'src/vision_pkg/config/camera_mount.yaml').read_text())
    r = Rotation.from_euler('xyz', mount['rpy_rad']).as_matrix()
    # Envelope centered at the mount; preserve its rotation separately.
    center_local = r.T @ np.array(mount['xyz_m'])
    result.append(('camera_envelope', 'end_effector_link', r,
                   center_local-[.07,.05,.05], center_local+[.07,.05,.05]))
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--plan', type=Path, required=True)
    parser.add_argument('--tcp', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    plan = json.loads(args.plan.read_text())
    model = ROOT / 'log/measured_model_20261111/measured.urdf'
    xml = model.read_text()
    bodies = envelopes(xml, json.loads(args.tcp.read_text()))
    xy = plan['axis_xy_m']
    obstacles = {
        'box_region': ([.325, 0, .040], [.225, .25, .050]),
        'can_envelope': ([xy[0], xy[1], .137], [.0365, .0365, .087]),
    }
    report = {'executed': False, 'clearance_proof': False,
              'assumptions': 'Box region x[.10,.55], y[-.25,.25], top .09m; can inflated 10mm XY/20mm Z. Camera envelope 140x100x100mm. No complete environment/self-collision proof.',
              'input_sha256': {str(p): hashlib.sha256(p.read_bytes()).hexdigest()
                               for p in (args.plan, args.tcp, model, Path(__file__))},
              'candidates': []}
    for row in plan['candidates']:
        if not row.get('paths'):
            continue
        path = row['paths'][0]
        if 'trajectory' in path:
            samples, motion_bound = trajectory_samples(path['trajectory']['joint_trajectory']['points'])
            interpolation = 'quintic_with_derivative_bound'
        else:
            q_path = np.asarray(path['positions_rad'])
            samples = []
            for first, second in zip(q_path[:-1], q_path[1:]):
                steps = max(1, int(np.ceil(np.max(abs(second-first))/np.deg2rad(.25))))
                samples.extend(first+(second-first)*f for f in np.linspace(0,1,steps+1))
            motion_bound = None
            interpolation = 'legacy_linear_screen_only'
        hits, minimum = [], float('inf')
        for index, q in enumerate(samples):
            angles = dict(zip(JOINTS, q))
            transforms = {link: fk(xml, angles, link) for link in {b[1] for b in bodies}}
            for name, link, local_r, lower, upper in bodies:
                transform = transforms[link]
                rotation = transform[:3,:3] @ local_r
                center = transform[:3,3] + rotation @ ((lower+upper)/2)
                for obstacle, (oc, oh) in obstacles.items():
                    gap = box_separation(center, rotation, (upper-lower)/2,
                                         np.array(oc), np.array(oh))
                    minimum = min(minimum, gap)
                    if gap <= 0:
                        hits.append(dict(sample=index, body=name, obstacle=obstacle,
                                         separating_gap_m=gap, joints_rad=q.tolist()))
        report['candidates'].append(dict(
            top_inset_m=row['top_inset_m'], pitch_deg=row['pitch_deg'],
            samples=len(samples), possible_overlap_count=len(hits),
            min_separating_axis_gap_m=minimum, first_overlaps=hits[:12],
            interpolation=interpolation,
            max_between_sample_motion_bound_m=motion_bound,
            sampled_envelope_clear=not hits))
    args.output.write_text(json.dumps(report, indent=2)+'\n')
    print(json.dumps([{k:v for k,v in row.items() if k != 'first_overlaps'}
                      for row in report['candidates']], indent=2))


if __name__ == '__main__':
    main()
