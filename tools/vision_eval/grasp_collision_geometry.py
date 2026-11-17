"""Nominal convex bounds for grasp diagnostics, independent of ROS."""

import numpy as np


def cylinder_box_gap(cylinder_center, cylinder_axis, radius, height,
                     box_center, box_rotation, box_half):
    """Find a separating axis; a nonpositive gap means overlap is only possible."""
    center, axis, bc, rotation, half = [np.asarray(v, dtype=float) for v in
                                      (cylinder_center, cylinder_axis, box_center,
                                       box_rotation, box_half)]
    if (center.shape != (3,) or axis.shape != (3,) or bc.shape != (3,)
            or rotation.shape != (3, 3) or half.shape != (3,)
            or not all(np.isfinite(v).all() for v in (center, axis, bc, rotation, half))
            or not np.isfinite([radius, height]).all() or min(radius, height) <= 0
            or np.any(half < 0) or not np.isclose(np.linalg.norm(axis), 1.)
            or not np.allclose(rotation.T @ rotation, np.eye(3), atol=1e-7)
            or not np.isclose(np.linalg.det(rotation), 1.)):
        raise ValueError('Invalid cylinder/box geometry')
    delta = bc - center
    radial = delta - axis * np.dot(delta, axis)
    axes = np.vstack((np.eye(3), rotation.T, axis, radial,
                      np.cross(rotation.T, axis)))
    norms = np.linalg.norm(axes, axis=1)
    axes = axes[norms > 1e-9] / norms[norms > 1e-9, None]
    dot = np.clip(axes @ axis, -1., 1.)
    cylinder_projection = radius * np.sqrt(np.maximum(0., 1. - dot**2)) + height / 2 * abs(dot)
    box_projection = abs(axes @ rotation) @ half
    return float(np.max(abs(axes @ delta) - cylinder_projection - box_projection))


def sliced_mesh_bounds(triangles, step_m=.002):
    """Enclose every longitudinal slab of a closed mesh, including crossing faces."""
    triangles = np.asarray(triangles, dtype=float)
    if (triangles.ndim != 3 or triangles.shape[1:] != (3, 3) or len(triangles) == 0
            or not np.isfinite(triangles).all() or not np.isfinite(step_m) or step_m <= 0):
        raise ValueError('Expected finite mesh triangles and a positive slice width')
    lower, upper = triangles[:, :, 0].min(), triangles[:, :, 0].max()
    cuts = np.linspace(lower, upper, max(1, int(np.ceil((upper - lower) / step_m))) + 1)
    boxes = []
    for lo, hi in zip(cuts[:-1], cuts[1:]):
        selected = triangles[(triangles[:, :, 0].max(axis=1) >= lo)
                             & (triangles[:, :, 0].min(axis=1) <= hi)]
        if not len(selected):
            continue
        vertices = selected.reshape(-1, 3)
        points = [vertices[(vertices[:, 0] >= lo) & (vertices[:, 0] <= hi)]]
        for i, j in ((0, 1), (1, 2), (2, 0)):
            a, b = selected[:, i], selected[:, j]
            dx = b[:, 0] - a[:, 0]
            for plane in (lo, hi):
                crosses = ((np.minimum(a[:, 0], b[:, 0]) <= plane)
                           & (np.maximum(a[:, 0], b[:, 0]) >= plane) & (abs(dx) > 1e-14))
                fraction = (plane - a[crosses, 0]) / dx[crosses]
                points.append(a[crosses] + fraction[:, None] * (b[crosses] - a[crosses]))
        points = np.concatenate(points)
        if len(points):
            boxes.append((points.min(axis=0), points.max(axis=0)))
    if not boxes:
        raise ValueError('Mesh has no enclosed slices')
    return boxes


def rigid_payload(tool_at_grasp, tool_now, center_at_grasp, axis_at_grasp=(0., 0., 1.)):
    """Follow the tool assuming no slip; do not leave a held object world-fixed."""
    initial, current = np.asarray(tool_at_grasp), np.asarray(tool_now)
    rotation = current[:3, :3] @ initial[:3, :3].T
    center = current[:3, 3] + rotation @ (np.asarray(center_at_grasp) - initial[:3, 3])
    return center, rotation @ axis_at_grasp


def cylinder_lowest_z(center, axis, radius, height):
    """Compute the exact lowest Z of a finite cylinder with a unit axis."""
    return float(center[2] - height / 2 * abs(axis[2]) - radius * np.hypot(axis[0], axis[1]))


def review_segment_clearance(stages, minimum_gap_m=.005):
    """Screen nominal body bounds, keeping mounting uncertainty explicit."""
    if not np.isfinite(minimum_gap_m) or minimum_gap_m < 0:
        raise ValueError('Invalid nominal clearance threshold')
    if [s['leg'] for s in stages] != ['descent', 'lift']:
        raise ValueError('Both descent and lift required')
    reasons, gaps = [], []
    for stage in stages:
        required = [body+'/'+obstacle for body in
                    ('link2', 'link3', 'link4', 'link5', 'camera_body_nominal')
                    for obstacle in ('table', 'can')]
        required += ['finger/table']
        if stage['leg'] == 'descent':
            required += ['finger/can']
        values = [stage['min_gaps_m'][key] for key in required]
        if not np.isfinite(values).all():
            raise ValueError('Nonfinite clearance')
        gaps.extend(values)
        reasons.extend(stage['leg']+': '+key+' below nominal clearance threshold'
                       for key, value in zip(required, values) if value < minimum_gap_m)
    payload = stages[1]['payload_bottom_above_table_m']
    if not np.isfinite([payload['min'], payload['final']]).all():
        raise ValueError('Nonfinite payload clearance')
    if payload['min'] < -1e-6:
        reasons.append('Held object crosses support surface during lift')
    if payload['final'] < .020:
        reasons.append('Held object finishes less than 20mm above support')
    return dict(nominal_geometry_accepted=not reasons, reasons=reasons,
                min_nominal_separating_gap_m=min(gaps), minimum_gap_m=minimum_gap_m,
                mounting_and_cables_verified=False, execution_authorized=False)
