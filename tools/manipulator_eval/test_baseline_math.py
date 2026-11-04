"""Tests for the pure manipulator-baseline helpers."""

import math

import pytest

from baseline_math import (
    axis_stats,
    checked_min_clearance_m,
    forward_kinematics,
    jog_target_problems,
    project_to_image,
    sweep_candidates,
    lift_target,
    grid_mm_to_base,
    gripper_width_from_joint,
    GripperMargin,
    height_to_base_z,
    mean_point,
    parse_moveit_log,
    parse_vision_log,
    point_error_mm,
    point_spread_mm,
    probe_target_problems,
    ProbeLimits,
    reach_from_shoulder_m,
    summarize_errors,
)


def test_height_to_base_z_uses_floor_offset():
    assert height_to_base_z(0.0) == pytest.approx(-0.010)
    assert height_to_base_z(60.0) == pytest.approx(0.050)
    assert height_to_base_z(60.0, surface_z_m=0.0) == pytest.approx(0.060)


def test_grid_mm_to_base():
    assert grid_mm_to_base(250.0, -40.0, 61.0) == pytest.approx(
        (0.250, -0.040, 0.051)
    )


def test_point_error_is_measured_minus_truth():
    error = point_error_mm((0.253, -0.040, 0.050), (0.250, -0.044, 0.050))
    assert error.ex == pytest.approx(3.0)
    assert error.ey == pytest.approx(4.0)
    assert error.ez == pytest.approx(0.0)
    assert error.horizontal == pytest.approx(5.0)
    assert error.total == pytest.approx(5.0)


def test_point_error_rejects_bad_input():
    with pytest.raises(ValueError):
        point_error_mm((0.0, 0.0), (0.0, 0.0, 0.0))
    with pytest.raises(ValueError):
        point_error_mm((math.nan, 0.0, 0.0), (0.0, 0.0, 0.0))


def test_axis_stats_separates_bias_and_spread():
    stats = axis_stats([8.0, 10.0, 12.0])
    assert stats.n == 3
    assert stats.mean == pytest.approx(10.0)
    assert stats.std == pytest.approx(2.0)
    assert stats.rms == pytest.approx(math.sqrt((64 + 100 + 144) / 3))
    assert stats.max_abs == pytest.approx(12.0)


def test_axis_stats_single_value_and_empty():
    assert axis_stats([-3.0]).std == 0.0
    assert axis_stats([-3.0]).max_abs == 3.0
    with pytest.raises(ValueError):
        axis_stats([])


def test_summarize_errors_keys():
    errors = [
        point_error_mm((0.003, 0.004, 0.0), (0.0, 0.0, 0.0)),
        point_error_mm((0.003, -0.004, 0.0), (0.0, 0.0, 0.0)),
    ]
    summary = summarize_errors(errors)
    assert set(summary) == {'x', 'y', 'z', 'xy', 'xyz'}
    assert summary['x'].mean == pytest.approx(3.0)
    assert summary['y'].mean == pytest.approx(0.0)
    assert summary['xy'].mean == pytest.approx(5.0)


def test_mean_point_and_spread():
    samples = [(0.0, 0.0, 0.0), (0.002, 0.0, 0.0)]
    assert mean_point(samples) == pytest.approx((0.001, 0.0, 0.0))
    assert point_spread_mm(samples) == pytest.approx(1.0)
    with pytest.raises(ValueError):
        mean_point([])


def test_gripper_margin():
    margin = GripperMargin(open_width_mm=72.0, object_width_mm=66.0)
    assert margin.total_mm == pytest.approx(6.0)
    assert margin.per_side_mm == pytest.approx(3.0)
    assert margin.fits
    assert not GripperMargin(50.0, 52.0).fits


def test_gripper_width_from_joint_is_linear():
    def width(q):
        return gripper_width_from_joint(q, 0.019, 70.0, -0.010, 12.0)

    assert width(0.019) == pytest.approx(70.0)
    assert width(-0.010) == pytest.approx(12.0)
    assert width(0.0045) == pytest.approx(41.0)
    with pytest.raises(ValueError):
        gripper_width_from_joint(0.0, 0.01, 1.0, 0.01, 2.0)


def test_probe_target_accepts_normal_point():
    assert probe_target_problems((0.20, 0.0, 0.05)) == []


@pytest.mark.parametrize(
    'point, keyword',
    [
        ((0.20, 0.0, 0.010), 'height above surface'),
        ((0.05, 0.0, 0.10), 'too close to the body'),
        ((0.20, 0.25, 0.10), '|y|'),
        ((0.20, 0.0, 0.40), 'z 0.400'),
        ((0.40, 0.0, 0.05), 'arm length'),
        ((math.inf, 0.0, 0.05), 'finite'),
    ],
)
def test_probe_target_refusals(point, keyword):
    problems = probe_target_problems(point)
    assert any(keyword in problem for problem in problems), problems


def test_probe_limits_follow_surface():
    raised = ProbeLimits(surface_z_m=0.020)
    assert probe_target_problems((0.20, 0.0, 0.040), raised)
    assert probe_target_problems((0.20, 0.0, 0.060), raised)
    assert probe_target_problems((0.20, 0.0, 0.070), raised) == []


def test_min_clearance_floor_cannot_be_lowered():
    assert checked_min_clearance_m(50.0) == pytest.approx(0.050)
    assert checked_min_clearance_m(80.0) == pytest.approx(0.080)
    for value in (49.0, 0.0, -10.0, math.nan):
        with pytest.raises(ValueError):
            checked_min_clearance_m(value)


def test_default_limits_refuse_points_below_5cm():
    assert probe_target_problems((0.20, 0.0, 0.035))  # 45 mm above floor
    assert probe_target_problems((0.20, 0.0, 0.040)) == []  # 50 mm


def test_lift_target():
    assert lift_target((0.2, 0.0, 0.05), 0.05, 0.35) == pytest.approx(
        (0.2, 0.0, 0.10))
    assert lift_target((0.2, 0.0, 0.33), 0.05, 0.35) == pytest.approx(
        (0.2, 0.0, 0.35))
    # Already above the cap: never move down.
    assert lift_target((0.2, 0.0, 0.36), 0.05, 0.35) == pytest.approx(
        (0.2, 0.0, 0.36))
    with pytest.raises(ValueError):
        lift_target((0.2, 0.0, 0.05), 0.0, 0.35)


def test_reach_from_shoulder():
    # Directly in front of joint1 at shoulder height.
    assert reach_from_shoulder_m((0.220, 0.0, 0.1675)) == pytest.approx(0.30)


def test_parse_vision_log():
    text = 'Vision target received in base_link: (0.251, -0.032, 0.048) m'
    assert parse_vision_log(text) == pytest.approx((0.251, -0.032, 0.048))
    assert parse_vision_log('Arm reached HOME') is None


def test_parse_moveit_log():
    label, point = parse_moveit_log('MoveIt PICK reached (0.271, 0.010, 0.045)')
    assert label == 'PICK'
    assert point == pytest.approx((0.271, 0.010, 0.045))
    assert parse_moveit_log('MoveIt PICK failed: error=-1') is None


def test_forward_kinematics_matches_known_poses():
    recognition = forward_kinematics((0.0, -30.0, -41.0, 115.0))
    assert recognition.ee == pytest.approx((0.008, 0.0, 0.320), abs=0.002)
    assert recognition.camera == pytest.approx((-0.003, 0.0195, 0.403),
                                               abs=0.002)
    assert recognition.camera_pitch_deg == pytest.approx(44.0, abs=0.5)
    home = forward_kinematics((0.0, -101.0, 53.0, 70.0))
    assert home.ee == pytest.approx((-0.010, 0.0, 0.212), abs=0.002)


def test_jog_target_problems():
    assert jog_target_problems((0.0, -30.0, -41.0, 115.0)) == []
    assert any('joint4' in p
               for p in jog_target_problems((0.0, -30.0, -41.0, 125.0)))
    # Reaching far down: hand below the 50 mm clearance.
    assert any('hand' in p
               for p in jog_target_problems((0.0, 60.0, 20.0, 30.0)))
    # Raised surface (8 cm box) makes the same pose stricter.
    low = (0.0, 30.0, -10.0, 60.0)
    assert len(jog_target_problems(low, surface_z_m=0.07)) >= \
        len(jog_target_problems(low))


def test_project_to_image_recognition_pose():
    pixel = project_to_image((0.0, -30.0, -41.0, 115.0), (0.24, 0.0, 0.105))
    assert pixel is not None
    column, row, depth = pixel
    assert 600 < column < 760 and 420 < row < 540 and 0.3 < depth < 0.45
    # Behind the camera.
    assert project_to_image((0.0, -30.0, -41.0, 115.0), (-0.5, 0, 0.4)) \
        is None


def test_sweep_candidates_are_safe_and_start_at_current_pose():
    current = (0.0, -30.0, -41.0, 115.0)
    poses = sweep_candidates((0.24, 0.0, 0.105), (-45, -35, -25, -15),
                             (-50, -41, -30, -20), (95, 105, 115), 0.07,
                             first=current)
    assert poses[0] == current
    assert len(poses) > 5
    for pose in poses[1:]:
        assert jog_target_problems(pose, 0.07) == []
        _, row, _ = project_to_image(pose, (0.24, 0.0, 0.105))
        assert 120 <= row <= 600
