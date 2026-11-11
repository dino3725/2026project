"""Test robust depth extraction, leakage prevention, and fixed-arm recovery."""

import copy

import numpy as np
import pytest

from refine_handeye import unpack
from rgbd_refinement import (evaluate, extract_depth, fit_fixed_arm, fit_plane, grouped_folds,
                             plane_intersections, score)
from test_pose_diagnostics import scene as arm_scene  # noqa: F401


@pytest.fixture
def rgbd_scene(request):
    """Add exact depth planes and recorded transforms to the synthetic scene."""
    _, chain, rows, k, d, mount, boards = request.getfixturevalue('arm_scene')
    for i, row in enumerate(rows):
        row['name'] = str(i)
        row['ee'] = chain.forward(np.array([row['q']]))[0]
        row['inverse_ee'] = np.linalg.inv(row['ee'])
        ob = np.linalg.inv(row['ee'] @ mount) @ boards[row['session_index']]
        normal = ob[:3, 2]
        row['plane'] = {'normal': normal, 'offset_m': -normal @ ob[:3, 3]}
        row['depth_corners'] = plane_intersections(row['pixels'], k, row['plane'])
    return rows, k, d, mount


def test_plane_rejects_gross_outliers_and_recovers_metric_surface():
    """A few invalid depths must not tilt the entire board estimate."""
    rng = np.random.default_rng(6)
    xy = rng.uniform(-.1, .1, (1000, 2))
    z = .5 + .2 * xy[:, 0] + rng.normal(0, .0002, len(xy))
    z[:100] += .06
    plane = fit_plane(np.c_[xy, z])
    normal = np.array([.2, 0, -1])
    normal /= np.linalg.norm(normal)
    assert plane['normal'] @ normal > .99999
    assert abs(plane['offset_m'] - .5 / np.sqrt(1.04)) < .0001
    assert .88 < plane['inlier_fraction'] < .92


def test_plane_and_ray_degeneracies_fail_explicitly():
    """Reject unsupported planes instead of producing fictitious 3D coordinates."""
    with pytest.raises(ValueError, match='100'):
        fit_plane(np.zeros((10, 3)))
    with pytest.raises(ValueError, match='Nonfinite'):
        fit_plane(np.full((100, 3), np.nan))
    with pytest.raises(ValueError, match='parallel'):
        plane_intersections(np.array([[0., 0]]), np.eye(3),
                            {'normal': np.array([1., 0, 0]), 'offset_m': -.5})
    with pytest.raises(ValueError, match='behind'):
        plane_intersections(np.array([[0., 0]]), np.eye(3),
                            {'normal': np.array([0., 0, 1]), 'offset_m': .5})


def test_aligned_depth_keeps_millimetres_and_rejects_mismatched_metadata():
    """Require the recorded zero-distortion depth path and preserve metric scale."""
    depth = np.full((480, 640), 500, dtype=np.uint16)
    pixels = np.array([[200., 160], [440., 160], [440., 320], [200., 320]])
    info = {'height': 480, 'width': 640, 'model': 'plumb_bob', 'd': [0.] * 5,
            'k': [300., 0, 320, 0, 300., 240, 0, 0, 1]}
    plane, points = extract_depth(depth, pixels, info)
    assert np.allclose(points[:, 2], .5)
    assert plane['inlier_rms_mm'] < 1e-8
    with pytest.raises(ValueError, match='zero-distortion'):
        extract_depth(depth, pixels, dict(info, d=[.1, 0, 0, 0, 0]))
    with pytest.raises(ValueError, match='16UC1'):
        extract_depth(depth.astype(np.float32), pixels, info)
    with pytest.raises(ValueError, match='100'):
        extract_depth(np.zeros_like(depth), pixels, info)


def test_close_poses_across_sessions_never_split_between_cv_folds():
    """Keep transitive pose groups together and exclude validation-role input."""
    rows = [{'role': 'train', 'q': np.deg2rad([angle, 0, 0, 0])}
            for angle in [0, 2, 4, 10, 11, 20, 21]]
    folds = grouped_folds(rows, count=3)
    assert sorted(i for f in folds for i in f) == list(range(7))
    assert any(set([0, 1, 2]) <= set(f) for f in folds)
    assert any(set([3, 4]) <= set(f) for f in folds)
    with pytest.raises(ValueError, match='training'):
        grouped_folds([dict(r, role='validation') for r in rows])


def test_rgbd_fit_recovers_known_geometry_and_held_views_are_not_fitted(rgbd_scene):
    """Recover metric truth with noisy initialization while preserving role separation."""
    rows, k, d, mount = rgbd_scene
    guess = mount.copy()
    guess[:3, 3] += [.006, -.003, .004]
    fit = fit_fixed_arm(rows, k, d, guess, loss='soft_l1', depth_scale_mm=5)
    assert np.allclose(unpack(fit[:6]), mount, atol=1e-6)
    held = [dict(r, role='validation') for r in rows]
    result = evaluate(fit, held, k, d)
    assert max(r['corner_3d_rms_mm'] for r in result) < 1e-3
    with pytest.raises(ValueError, match='Training-only'):
        fit_fixed_arm(held, k, d, mount)


def test_robust_loss_limits_single_view_corner_contamination(rgbd_scene):
    """An image outlier must not dominate a shared camera mount."""
    rows, k, d, mount = rgbd_scene
    contaminated = copy.deepcopy(rows)
    contaminated[0]['pixels'] += [30., -20.]
    ordinary = fit_fixed_arm(contaminated, k, d, mount, depth_scale_mm=5)
    robust = fit_fixed_arm(contaminated, k, d, mount, loss='soft_l1', depth_scale_mm=5)
    ordinary_error = np.linalg.norm(unpack(ordinary[:6])[:3, 3] - mount[:3, 3])
    robust_error = np.linalg.norm(unpack(robust[:6])[:3, 3] - mount[:3, 3])
    assert robust_error < ordinary_error / 3


def test_ranking_does_not_reward_forcing_inferred_table_height_to_zero():
    """Keep the physical height check outside the selected optimization score."""
    row = {'pixel_rms_px': 2., 'plane_rms_mm': 3., 'depth_corner_centroid_height_mm': 40.}
    assert score([row]) == pytest.approx(np.sqrt(2.5))
    assert score([row]) == score([dict(row, depth_corner_centroid_height_mm=0)])
