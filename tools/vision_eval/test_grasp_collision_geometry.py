"""Exercise separation bounds and attached-object motion."""

import numpy as np
import pytest
from scipy.spatial.transform import Rotation

from grasp_collision_geometry import cylinder_box_gap, cylinder_lowest_z
from grasp_collision_geometry import rigid_payload, sliced_mesh_bounds


def test_cylinder_separation_touch_and_overlap():
    def gap(x):
        return cylinder_box_gap([0, 0, 0], [0, 0, 1], .0265, .134,
                                [x, 0, 0], np.eye(3), [.01, .01, .01])
    assert np.isclose(gap(.05), .0135)
    assert abs(gap(.0365)) < 1e-12
    assert gap(0.) < 0


def test_separation_is_rigid_rotation_invariant_for_known_axis():
    r = Rotation.from_euler('xyz', [25, 31, 17], degrees=True).as_matrix()
    gap = cylinder_box_gap([0, 0, 0], r[:, 2], .0265, .134,
                           r @ [.05, 0, 0], r, [.01, .01, .01])
    assert np.isclose(gap, .0135)


def test_slices_include_long_triangles_crossing_both_planes():
    triangles = np.array([[[0., 0., 0.], [.1, .02, 0.], [.1, 0., .02]]])
    boxes = sliced_mesh_bounds(triangles, .01)
    assert len(boxes) == 10
    lo, hi = boxes[4]
    assert np.allclose(lo, [.04, 0, 0])
    assert np.allclose(hi, [.05, .01, .01])
    for t in np.linspace(0, 1, 101):
        point = [.1*t, .01*t, .01*t]
        assert any(np.all(point >= low-1e-12) and np.all(point <= high+1e-12)
                   for low, high in boxes)


def test_payload_follows_pitch_and_translation():
    initial = np.eye(4)
    current = np.eye(4)
    current[:3, :3] = Rotation.from_euler('y', 90, degrees=True).as_matrix()
    current[2, 3] = .03
    center, axis = rigid_payload(initial, current, [0, 0, -.04])
    assert np.allclose(center, [-.04, 0, .03])
    assert np.allclose(axis, [1, 0, 0])
    assert np.isclose(cylinder_lowest_z(center, axis, .0265, .134), .0035)


def test_pitch_can_lower_bottom_despite_raising_tool():
    initial, current = np.eye(4), np.eye(4)
    current[:3, :3] = Rotation.from_euler('y', 10, degrees=True).as_matrix()
    current[2, 3] = .001
    center, axis = rigid_payload(initial, current, [0, 0, -.042])
    assert cylinder_lowest_z(center, axis, .0265, .134) < -.109


def test_invalid_geometry_rejected():
    with pytest.raises(ValueError):
        cylinder_box_gap([0, 0, 0], [0, 0, 2], .01, .1,
                         [0, 0, 0], np.eye(3), [.01]*3)
    with pytest.raises(ValueError):
        sliced_mesh_bounds(np.zeros((0, 3, 3)))


def test_selection_excludes_body_overlap_but_reports_mount_uncertainty():
    from grasp_collision_geometry import review_segment_clearance
    stages = []
    for leg in ('descent', 'lift'):
        gaps = {body+'/'+obstacle: .020 for body in
                ('link2', 'link3', 'link4', 'link5', 'camera_body_nominal', 'finger')
                for obstacle in ('table', 'can')}
        gaps['camera_envelope/can'] = -.03
        stages.append(dict(leg=leg, min_gaps_m=gaps,
                           payload_bottom_above_table_m=dict(min=0., final=.028)))
    result = review_segment_clearance(stages)
    assert result['nominal_geometry_accepted']
    assert not result['mounting_and_cables_verified']
    assert not result['execution_authorized']
    stages[0]['min_gaps_m']['camera_body_nominal/can'] = -.002
    assert not review_segment_clearance(stages)['nominal_geometry_accepted']


def test_selection_rejects_incomplete_geometry_and_payload_dip():
    from grasp_collision_geometry import review_segment_clearance
    with pytest.raises(ValueError):
        review_segment_clearance([])
    gaps = {body+'/'+obstacle: .02 for body in
            ('link2', 'link3', 'link4', 'link5', 'camera_body_nominal', 'finger')
            for obstacle in ('table', 'can')}
    stages = [dict(leg=leg, min_gaps_m=gaps.copy(),
                   payload_bottom_above_table_m=dict(min=-.002, final=.028))
              for leg in ('descent', 'lift')]
    assert not review_segment_clearance(stages)['nominal_geometry_accepted']
    stages[0]['min_gaps_m']['finger/can'] = float('nan')
    with pytest.raises(ValueError):
        review_segment_clearance(stages)
