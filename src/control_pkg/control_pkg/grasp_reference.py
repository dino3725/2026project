"""Model-based grasp targets, separate from observations and execution."""

import math


def contact_to_tool(contact_base, tcp_in_tool, rotation_base_tool):
    """
    Convert a desired TCP position to the tool origin with a known rotation.

    Rotation rows map tool-frame vectors into base coordinates. Callers must
    establish the contact point and TCP from geometry; this does not estimate
    either quantity from a single observed surface point.
    """
    contact = tuple(contact_base)
    tcp = tuple(tcp_in_tool)
    rotation = tuple(tuple(row) for row in rotation_base_tool)
    if (len(contact) != 3 or len(tcp) != 3 or len(rotation) != 3
            or any(len(row) != 3 for row in rotation)):
        raise ValueError('Expected two 3D vectors and a 3x3 rotation')
    if not all(math.isfinite(v) for v in contact + tcp + sum(rotation, ())):
        raise ValueError('Geometry must be finite')
    for i in range(3):
        for j in range(3):
            dot = sum(rotation[i][k] * rotation[j][k] for k in range(3))
            if abs(dot - float(i == j)) > 1e-6:
                raise ValueError('Rotation must be orthonormal')
    a, b, c = rotation
    determinant = (a[0] * (b[1] * c[2] - b[2] * c[1])
                   - a[1] * (b[0] * c[2] - b[2] * c[0])
                   + a[2] * (b[0] * c[1] - b[1] * c[0]))
    if abs(determinant - 1.) > 1e-6:
        raise ValueError('Rotation must be right handed')
    return tuple(contact[i] - sum(rotation[i][j] * tcp[j] for j in range(3))
                 for i in range(3))


def upright_contact(center_xy, support_z_base, object_height, top_inset):
    """
    Choose a contact on an established upright object axis, in meters.

    support_z_base is already in base coordinates, not height above the floor.
    center_xy must be an estimated object axis, never an unlabeled surface.
    """
    xy = tuple(center_xy)
    values = xy + (support_z_base, object_height, top_inset)
    if len(xy) != 2 or not all(math.isfinite(v) for v in values):
        raise ValueError('Expected finite geometry and an XY axis')
    if object_height <= 0 or not 0 < top_inset < object_height:
        raise ValueError('Contact must lie strictly inside object height')
    return xy + (support_z_base + object_height - top_inset,)


def upright_candidate(center_xy, support_z_base, object_height, top_inset,
                      tcp_in_tool, rotation_base_tool, finger_corners_in_tool,
                      minimum_approach_m, minimum_clearance_m, lift_m,
                      position_tolerance_m, orientation_tolerance_rad,
                      approach_backoff_m=0., approach_origin_xy=(0., 0.)):
    """
    Build contact and tool targets with a nominal finger-envelope clearance.

    This is geometry, not an execution authorization or a hardware correction.
    The buffer covers specified IK tolerances, not unmeasured physical error.
    """
    values = (minimum_approach_m, minimum_clearance_m, lift_m,
              position_tolerance_m, orientation_tolerance_rad)
    if (not all(math.isfinite(v) for v in values) or min(values[:3]) <= 0
            or position_tolerance_m < 0 or not 0 <= orientation_tolerance_rad < math.pi):
        raise ValueError('Invalid candidate clearances or tolerances')
    contact = upright_contact(center_xy, support_z_base, object_height, top_inset)
    pick_tool = contact_to_tool(contact, tcp_in_tool, rotation_base_tool)
    relative = []
    for corner in finger_corners_in_tool:
        if len(corner) != 3 or not all(math.isfinite(x) for x in corner):
            raise ValueError('Invalid finger corner')
        relative.append(tuple(corner[i] - tcp_in_tool[i] for i in range(3)))
    if not relative:
        raise ValueError('Finger envelope required')
    relative_bottom = min(sum(rotation_base_tool[2][i] * p[i] for i in range(3))
                          for p in relative)
    radius = max(math.sqrt(sum(x * x for x in p)) for p in relative)
    buffer = position_tolerance_m + 2 * radius * math.sin(orientation_tolerance_rad / 2)
    approach_height = max(minimum_approach_m,
                          top_inset + minimum_clearance_m - relative_bottom + buffer)
    approach = (contact[0], contact[1], contact[2] + approach_height)
    if not math.isfinite(approach_backoff_m) or not 0 <= approach_backoff_m <= .1:
        raise ValueError('Approach backoff must be between zero and 0.1m')
    if len(approach_origin_xy) != 2 or not all(math.isfinite(v) for v in approach_origin_xy):
        raise ValueError('Invalid approach reference origin')
    if approach_backoff_m:
        direction = [contact[i] - approach_origin_xy[i] for i in range(2)]
        distance = math.hypot(*direction)
        if distance <= approach_backoff_m:
            raise ValueError('Approach backoff crosses reference origin')
        approach = tuple(contact[i] - approach_backoff_m * direction[i] / distance
                         for i in range(2)) + (approach[2],)
    lift = (contact[0], contact[1], contact[2] + lift_m)
    return {
        'contact_base_m': contact,
        'pick_tool_base_m': pick_tool,
        'approach_contact_base_m': approach,
        'approach_tool_base_m': contact_to_tool(approach, tcp_in_tool, rotation_base_tool),
        'lift_contact_base_m': lift,
        'lift_tool_base_m': contact_to_tool(lift, tcp_in_tool, rotation_base_tool),
        'object_top_z_base_m': support_z_base + object_height,
        'approach_height_m': approach_height,
        'approach_backoff_m': approach_backoff_m,
        'ik_clearance_buffer_m': buffer,
        'nominal_approach_clearance_m': approach[2] + relative_bottom
        - support_z_base - object_height,
        'reference': 'nominal_model_not_physical_measurement',
    }


def review_contact_path(contacts, rotations, start, finish, requested_rotation,
                        position_tolerance_m, orientation_tolerance_rad,
                        pitch_range_rad=None):
    """Check FK samples of a complete straight contact path, not just its ends."""
    if (not math.isfinite(position_tolerance_m) or position_tolerance_m <= 0
            or not math.isfinite(orientation_tolerance_rad)
            or not 0 <= orientation_tolerance_rad < math.pi):
        raise ValueError('Invalid path tolerances')
    points, matrices = list(contacts), list(rotations)
    if pitch_range_rad is not None:
        if (len(pitch_range_rad) != 2 or not all(math.isfinite(v) for v in pitch_range_rad)
                or not -math.pi / 2 < pitch_range_rad[0] < pitch_range_rad[1] < math.pi / 2):
            raise ValueError('Expected a nonsingular pitch interval')
    if len(points) < 2 or len(matrices) != len(points):
        raise ValueError('At least two matching contact/rotation samples required')
    for point, matrix in zip(points + [start, finish],
                             matrices + [requested_rotation, requested_rotation]):
        contact_to_tool(point, (0., 0., 0.), matrix)
    delta = [finish[i] - start[i] for i in range(3)]
    length = math.sqrt(sum(x * x for x in delta))
    if length <= 1e-9:
        raise ValueError('Path must have nonzero length')
    progress, errors, angles, pitches, transverse_angles = [], [], [], [], []
    reference_yaw = math.atan2(requested_rotation[1][0], requested_rotation[0][0])
    reference_roll = math.atan2(requested_rotation[2][1], requested_rotation[2][2])
    for point, matrix in zip(points, matrices):
        distance = sum((point[i] - start[i]) * delta[i] for i in range(3)) / length
        progress.append(distance)
        clipped = min(length, max(0., distance))
        errors.append(math.sqrt(sum(
            (point[i] - start[i] - clipped * delta[i] / length) ** 2 for i in range(3))))
        trace = sum(matrix[i][j] * requested_rotation[i][j]
                    for i in range(3) for j in range(3))
        angles.append(math.acos(min(1., max(-1., (trace - 1.) / 2))))
        pitches.append(math.atan2(-matrix[2][0], math.hypot(matrix[0][0], matrix[1][0])))
        yaw_delta = math.atan2(matrix[1][0], matrix[0][0]) - reference_yaw
        roll_delta = math.atan2(matrix[2][1], matrix[2][2]) - reference_roll
        transverse_angles.append(max(abs(math.atan2(math.sin(v), math.cos(v)))
                                     for v in (yaw_delta, roll_delta)))
    endpoint_error = max(math.dist(points[0], start), math.dist(points[-1], finish))
    backward = max([0.] + [a - b for a, b in zip(progress, progress[1:])])
    orientation_ok = max(angles) <= orientation_tolerance_rad
    if pitch_range_rad is not None:
        orientation_ok = (min(pitches) >= pitch_range_rad[0] - 1e-9
                          and max(pitches) <= pitch_range_rad[1] + 1e-9
                          and max(transverse_angles) <= orientation_tolerance_rad)
    accepted = (endpoint_error <= position_tolerance_m
                and max(errors) <= position_tolerance_m
                and orientation_ok
                and backward <= position_tolerance_m)
    return {'accepted': bool(accepted), 'endpoint_error_m': endpoint_error,
            'max_line_error_m': max(errors), 'max_orientation_error_rad': max(angles),
            'pitch_range_rad': [min(pitches), max(pitches)],
            'max_yaw_roll_error_rad': max(transverse_angles),
            'orientation_rule': ('pitch_interval' if pitch_range_rad is not None
                                 else 'fixed_rotation'),
            'max_backward_step_m': backward, 'samples': len(points),
            'physical_path_verified': False}
