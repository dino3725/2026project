"""Check conservative bounding-box separation for approach review."""

import numpy as np
from scipy.spatial.transform import Rotation

from screen_grasp_path import box_separation


def test_overlap_and_touch_are_not_clear():
    for center in ([0, 0, 0], [2, 0, 0]):
        assert box_separation(center, np.eye(3), np.ones(3),
                              np.zeros(3), np.ones(3)) <= 0


def test_diagonal_separation_and_rotated_overlap():
    rotation = Rotation.from_euler('z', 45, degrees=True).as_matrix()
    half = np.array([1., 1., .2])
    assert box_separation([2.1, 0, 0], np.eye(3), half,
                          np.zeros(3), half) > 0
    assert box_separation([2.1, 0, 0], rotation, half,
                          np.zeros(3), half) < 0
    assert box_separation([4, 4, 0], rotation, half,
                          np.zeros(3), half) > 0


def test_vertical_gap_survives_yaw_rotation():
    rotation = Rotation.from_euler('z', 79, degrees=True).as_matrix()
    assert np.isclose(box_separation([0, 0, 3], rotation, np.ones(3),
                                    np.zeros(3), np.ones(3)), 1.)


def test_quintic_samples_reproduce_endpoint_and_cover_observed_motion():
    from screen_grasp_path import trajectory_samples
    points = [dict(positions=[x,0,0,0], velocities=[0.]*4, accelerations=[0.]*4,
                   time_from_start=dict(sec=t,nanosec=0)) for x,t in [(0.,0),(1.,2)]]
    samples, bound = trajectory_samples(points)
    assert np.allclose(samples[0], [0,0,0,0])
    assert np.allclose(samples[-1], [1,0,0,0])
    assert np.all(np.diff(samples[:,0]) >= 0)
    assert bound >= .8*np.abs(np.diff(samples,axis=0)).sum(axis=1).max()-1e-10


def test_quintic_detects_overshoot_hidden_by_waypoint_positions():
    from screen_grasp_path import trajectory_samples
    points = [dict(positions=[0.]*4, velocities=[v,0,0,0], accelerations=[0.]*4,
                   time_from_start=dict(sec=t,nanosec=0)) for v,t in [(1.,0),(-1.,1)]]
    samples, bound = trajectory_samples(points)
    assert samples[:,0].max() > .25
    assert bound > 0
