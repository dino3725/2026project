"""Exercise missing centers, background ambiguity and stale sample windows."""

import numpy as np
import pytest

from vision_pkg.surface_localization import estimate_surface
from vision_pkg.surface_localization import LocalizationWindow
from vision_pkg.vision_utils import deproject_pixel
from vision_pkg.vision_utils import Detection


K = (100., 100., 15., 15.)
BOX = Detection('coke', 0.9, 1, 1, 29, 29)


def test_missing_center_preserves_selected_pixel_depth_correspondence():
    """An off-center supported patch must not be assigned to the center ray."""
    image = np.zeros((31, 31), dtype=np.uint16)
    image[8:23, 8:12] = 300
    result = estimate_surface(image, BOX, '16UC1', .001, K)
    assert result.source == 'inner'
    assert result.pixel[0] < 12
    assert result.point == pytest.approx(
        deproject_pixel(*result.pixel, .3, *K))
    assert result.point[0] < 0


def test_mixed_foreground_background_is_rejected_without_nearest_guess():
    """Two similarly supported depths are ambiguous even if both are valid."""
    image = np.full((31, 31), .8)
    image[:, :15] = .3
    result = estimate_surface(image, BOX, '32FC1', .001, K)
    assert result.point is None
    assert result.reason == 'ambiguous depth groups'


def test_sparse_center_outlier_does_not_override_supported_interior():
    """One close return at the center is not enough to certify that surface."""
    image = np.full((31, 31), 300, dtype=np.uint16)
    image[12:19, 12:19] = 0
    image[15, 15] = 100
    result = estimate_surface(image, BOX, '16UC1', .001, K)
    assert result.point[2] == pytest.approx(.3)
    assert result.source == 'inner'


@pytest.mark.parametrize('encoding,scale', [('bad', .001), ('16UC1', np.nan)])
def test_invalid_input_is_not_interpreted_as_meters(encoding, scale):
    """Unknown units or nonfinite scale must not produce a point."""
    with pytest.raises(ValueError):
        estimate_surface(np.ones((31, 31)), BOX, encoding, scale, K)


def test_float_depth_ignores_invalid_and_is_already_meters():
    """Invalid values are omitted without applying millimeter scaling."""
    image = np.full((31, 31), .3)
    image[15, 15] = np.nan
    image[15, 16] = np.inf
    result = estimate_surface(image, BOX, '32FC1', .001, K)
    assert result.point[2] == pytest.approx(.3)


def test_window_discards_pre_gap_location_and_waits_again():
    """A reappearing target cannot complete with samples from before loss."""
    window = LocalizationWindow(duration=1., horizon=2., min_samples=3)
    window.update(0., (0., 0., .3, .9))
    window.update(.4, (0., 0., .3, .9))
    window.update(1., None)
    assert not window.samples
    assert window.update(1.1, (.1, 0., .3, .9)) is None
    assert len(window.samples) == 1
    for t in (1.5, 1.9):
        assert window.update(t, (.1, 0., .3, .9)) is None
    assert window.update(2.2, (.1, 0., .3, .9)) == (.1, 0., .3, .9)


def test_window_rejects_motion_then_recovers_after_old_samples_expire():
    """Enough frames alone must not pass a moving target."""
    window = LocalizationWindow(duration=1., horizon=1.5, min_samples=3)
    for i in range(8):
        assert window.update(i * .2, (.1 * (i % 2), 0., .3, .9)) is None
    assert window.reason == 'recent coordinates unstable'
    result = None
    for i in range(8, 20):
        result = window.update(i * .2, (.2, 0., .3, .9))
    assert result == (.2, 0., .3, .9)
    assert all(t >= 2.3 for t, _ in window.samples)


def test_window_rejects_duplicate_stamp_and_nonfinite_sample():
    """Duplicate observations cannot inflate the valid sample count."""
    window = LocalizationWindow()
    window.update(1., (0., 0., .3, .9))
    with pytest.raises(ValueError):
        window.update(1., (0., 0., .3, .9))
    with pytest.raises(ValueError):
        window.update(2., (np.nan, 0., .3, .9))


def test_brief_failed_observation_discards_prior_stability():
    """Even a short explicit depth/TF failure needs new continuous evidence."""
    window = LocalizationWindow(duration=1., horizon=2., min_samples=3)
    for t in (0., .4, .8):
        window.update(t, (0., 0., .3, .9))
    window.update(.9, None)
    assert not window.samples
    assert window.coverage == 0.
    assert window.update(1., (0., 0., .3, .9)) is None
    assert window.update(1.4, (0., 0., .3, .9)) is None
    assert window.update(1.8, (0., 0., .3, .9)) is None
    assert window.update(2.1, (0., 0., .3, .9)) == (0., 0., .3, .9)
