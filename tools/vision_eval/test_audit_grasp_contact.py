"""Check actual finger opening and rotation-dependent contact geometry."""

import numpy as np
import pytest

from audit_grasp_contact import finger_geometry, transformed_bounds


def test_pitch_rotates_horizontal_extent_into_height():
    pose = np.eye(4)
    pose[:3, :3] = [[0, 0, 1], [0, 1, 0], [-1, 0, 0]]
    pose[2, 3] = .3
    bounds = transformed_bounds([-.02, -.01, -.005], [.04, .01, .005], pose)
    assert bounds[:, 2] == pytest.approx([.26, .32])


def test_actual_closed_gap_does_not_inherit_planned_open_width():
    pads = []
    for side, sign in [('left', 1), ('right', -1)]:
        center = np.array([0., sign * .021, 0.])
        axis = np.array([0., sign, 0.])
        opened = center + axis * .019
        pads.append(dict(side=side, axis=axis, open_joint_m=.019,
                         center_at_zero_in_tool_m=center, pad_size_m=[.035, 0, .015],
                         open_mesh_bounds_in_tool_m=[opened - .002, opened + .002]))
    pose = np.eye(4)
    pose[2, 3] = .25
    result = finger_geometry(pose, {'pads': pads},
                             {'gripper_left_joint': 0., 'gripper_right_joint': 0.}, .204)
    assert result['nominal_inner_width_m'] == pytest.approx(.042)
    assert result['fingers'][0]['model_contact_pad_gap_above_top_m'] == pytest.approx(.0385)
    with pytest.raises(KeyError):
        finger_geometry(pose, {'pads': pads}, {}, .204)


@pytest.mark.parametrize('bad', [float('nan'), float('inf')])
def test_invalid_bounds_cannot_produce_a_clearance(bad):
    with pytest.raises(ValueError):
        transformed_bounds([bad, 0, 0], [1, 1, 1], np.eye(4))
