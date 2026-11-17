"""Check frame conventions without assuming observed surfaces are centers."""

import json
import math
import pytest

from control_pkg.grasp_reference import contact_to_tool
from control_pkg.grasp_reference import upright_contact
from control_pkg.grasp_reference import upright_candidate, review_contact_path


def test_fixed_support_height_uses_base_origin_not_floor_origin():
    """An 80 mm box above a floor 10 mm below base gives 70 mm support z."""
    contact = upright_contact((.285, .02), .07, .134, .03)
    assert contact == pytest.approx((.285, .02, .174))


def test_tcp_offset_rotates_before_subtraction():
    """A tool X offset becomes base Z for this tilted gripper."""
    rotation = ((0., 0., -1.), (0., 1., 0.), (1., 0., 0.))
    tool = contact_to_tool((.285, .02, .174), (.03, 0., 0.), rotation)
    assert tool == pytest.approx((.285, .02, .144))


def test_reflection_is_not_a_valid_tool_orientation():
    """Reject a matrix that would silently mirror the contact offset."""
    with pytest.raises(ValueError):
        contact_to_tool((0, 0, 0), (0, 0, 0),
                        ((-1, 0, 0), (0, 1, 0), (0, 0, 1)))


@pytest.mark.parametrize('inset', [0, -.01, .134, float('nan')])
def test_invalid_contact_height_is_rejected(inset):
    """Do not command the rim, the support or an undefined height."""
    with pytest.raises(ValueError):
        upright_contact((.285, 0), .07, .134, inset)


IDENTITY = ((1., 0., 0.), (0., 1., 0.), (0., 0., 1.))


def test_model_approach_uses_fingers_and_not_just_tool_height():
    """A deep contact increases descent distance while preserving top clearance."""
    def candidate(inset):
        return upright_candidate((.27, .05), .07, .134, inset, (.0004, 0, 0),
                                 IDENTITY, [(-.06, -.04, -.029), (.02, .04, .029)],
                                 .05, .025, .03, .002, 0.)
    upper, lower = candidate(.025), candidate(.055)
    assert upper['approach_tool_base_m'] == pytest.approx(lower['approach_tool_base_m'])
    assert upper['pick_tool_base_m'] == pytest.approx((.2696, .05, .179))
    assert lower['pick_tool_base_m'][2] == pytest.approx(.149)
    assert upper['nominal_approach_clearance_m'] == pytest.approx(.027)


def test_model_rotates_tcp_and_bounds_for_tilted_fingers():
    """Tool X offset becomes vertical when the wrist rotates by 90 degrees."""
    rotation = ((0., 0., 1.), (0., 1., 0.), (-1., 0., 0.))
    result = upright_candidate((.27, 0), .07, .134, .04, (.02, 0, 0),
                               rotation, [(-.06, -.04, -.029), (.06, .04, .029)],
                               .05, .01, .03, .002, 0.)
    assert result['pick_tool_base_m'][2] == pytest.approx(.184)
    assert result['nominal_approach_clearance_m'] == pytest.approx(.012)


def test_backoff_moves_toward_arm_origin_without_changing_contact():
    """Side targets need radial approach backoff, not a constant base-X shift."""
    result = upright_candidate((.24, .24), .07, .134, .04, (0, 0, 0),
                               IDENTITY, [(0, 0, -.029), (0, 0, .029)],
                               .05, .04, .03, .002, 0., .04, (-.08, 0.))
    assert result['contact_base_m'] == pytest.approx((.24, .24, .164))
    assert result['approach_contact_base_m'][:2] == pytest.approx((.208, .216))


def test_correct_endpoints_do_not_hide_a_bowed_path():
    """A 12 mm lateral excursion must fail even with correct endpoints."""
    result = review_contact_path([(0, 0, .2), (.012, 0, .175), (0, 0, .15)],
                                 [IDENTITY] * 3, (0, 0, .2), (0, 0, .15),
                                 IDENTITY, .002, .1)
    assert not result['accepted']
    assert result['max_line_error_m'] == pytest.approx(.012)


def test_position_only_ik_cannot_hide_wrong_orientation():
    """A straight TCP path still fails when the gripper rotates away."""
    rotated = ((0., -1., 0.), (1., 0., 0.), (0., 0., 1.))
    points = [(0, 0, .2), (0, 0, .175), (0, 0, .15)]
    result = review_contact_path(points, [IDENTITY, rotated, IDENTITY],
                                 points[0], points[-1], IDENTITY, .002, .1)
    assert not result['accepted']
    good = review_contact_path(points, [IDENTITY] * 3, points[0], points[-1],
                               IDENTITY, .002, .1)
    assert good['accepted']
    assert json.loads(json.dumps(good))['accepted'] is True


def test_path_rejects_reverse_and_incomplete_descent():
    """Fraction/endpoint and direction are distinct from distance to the line."""
    start, end = (0, 0, .2), (0, 0, .15)
    assert not review_contact_path([start, (0, 0, .18)], [IDENTITY] * 2,
                                   start, end, IDENTITY, .002, .1)['accepted']
    points = [start, (0, 0, .16), (0, 0, .19), end]
    assert not review_contact_path(points, [IDENTITY] * 4, start, end,
                                   IDENTITY, .002, .1)['accepted']


def test_pitch_interval_does_not_relax_yaw_or_allow_out_of_range_pitch():
    """Permit a declared gripper pitch range while retaining other constraints."""
    def rotation(angle):
        c, s = math.cos(angle), math.sin(angle)
        return ((c, 0., s), (0., 1., 0.), (-s, 0., c))
    start, end = (0, 0, .2), (0, 0, .15)
    result = review_contact_path([start, end], [IDENTITY, rotation(.15)], start, end,
                                 IDENTITY, .002, .05, (-.1, .3))
    assert result['accepted']
    assert not review_contact_path([start, end], [IDENTITY, rotation(.4)], start, end,
                                   IDENTITY, .002, .05, (-.1, .3))['accepted']
    wrong_yaw = ((0., -1., 0.), (1., 0., 0.), (0., 0., 1.))
    assert not review_contact_path([start, end], [IDENTITY, wrong_yaw], start, end,
                                   IDENTITY, .002, .05, (-.1, .3))['accepted']
