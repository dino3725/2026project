"""Test explicit cylinder assumptions on known synthetic geometry."""

import numpy as np
import pytest

from vision_pkg.object_geometry import fit_upright_axis


def cylinder(arc=.8):
    """Generate a visible body arc, not a full synthetic circumference."""
    theta, z = np.meshgrid(np.linspace(np.pi - arc, np.pi + arc, 30),
                           np.linspace(.10, .17, 12))
    return np.column_stack((.30 + .0265 * np.cos(theta.ravel()),
                            .02 + .0265 * np.sin(theta.ravel()), z.ravel()))


def test_known_radius_visible_arc_recovers_axis_not_surface_centroid():
    """A visible-surface median is closer to the camera than the fitted axis."""
    points = cylinder()
    result = fit_upright_axis(points, [0., .02, .4], .07, .134, .0265)
    assert result['accepted']
    assert result['axis_xy_m'] == pytest.approx([.30, .02], abs=1e-5)
    assert np.median(points[:, 0]) < .28


def test_nearly_single_ray_cannot_certify_a_center():
    """An extremely narrow visible arc does not constrain lateral center well."""
    result = fit_upright_axis(cylinder(.01), [0., .02, .4], .07, .134, .0265)
    assert not result['accepted']
    assert 'arc' in result['reason']


def test_support_and_top_points_cannot_be_fitted_as_body():
    """Wrong vertical coverage must not produce a plausible center."""
    points = cylinder()
    points[:, 2] = .07
    result = fit_upright_axis(points, [0., .02, .4], .07, .134, .0265)
    assert not result['accepted']


def test_large_wrong_radius_cloud_is_rejected():
    """Known size rejects a different object's surface."""
    points = cylinder(1.5)
    points[:, :2] = (points[:, :2] - [.30, .02]) * 3 + [.30, .02]
    result = fit_upright_axis(points, [0., .02, .4], .07, .134, .0265)
    assert not result['accepted']


def test_supported_circle_with_contamination_is_not_automatically_accepted():
    """A majority of close points cannot hide a large residual tail."""
    points = cylinder()
    # Keep the original true geometry on 80 percent of the body points.
    points[::5, :2] = (points[::5, :2] - [.30, .02]) * 1.6 + [.30, .02]
    result = fit_upright_axis(points, [0., .02, .4], .07, .134, .0265)
    assert not result['accepted']
    assert result['reason'] == 'body residual exceeds quality limit'
    assert result['axis_xy_m'] is None
    assert result['axis_hypothesis_xy_m'] is not None


def test_small_measurement_noise_retains_a_well_supported_axis():
    """A fixed synthetic noise draw should not reject a millimeter-scale fit."""
    points = cylinder()
    points[:, :2] += np.random.default_rng(42).normal(0, .0005, (len(points), 2))
    result = fit_upright_axis(points, [0., .02, .4], .07, .134, .0265)
    assert result['accepted']
    assert result['axis_xy_m'] == pytest.approx([.30, .02], abs=.001)


@pytest.mark.parametrize('limit', [0., -1., float('nan')])
def test_invalid_quality_limit_is_rejected(limit):
    """A missing numerical limit cannot silently disable quality gating."""
    with pytest.raises(ValueError):
        fit_upright_axis(cylinder(), [0., .02, .4], .07, .134, .0265, limit)
