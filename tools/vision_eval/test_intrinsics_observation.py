"""Check measured print geometry and intrinsic-independent corner extraction."""

import cv2
import numpy as np
import pytest

from handeye import board
from intrinsics_observation import extract, metric_corners


def test_nonuniform_print_dimensions():
    """A stretched horizontal axis must not stretch the vertical axis."""
    points = metric_corners([0, 23], 177, 125)
    assert np.allclose(points, [[0.177 / 7, 0.025, 0], [6 * 0.177 / 7, 0.1, 0]])


@pytest.mark.parametrize('width,height', [(0, 125), (177, -1), (float('nan'), 125)])
def test_invalid_dimensions(width, height):
    """Prevent invalid measurement units from producing calibration inputs."""
    with pytest.raises(ValueError, match='dimensions'):
        metric_corners([0], width, height)


def test_stretched_board_pixels_without_intrinsics():
    """Detect a horizontally stretched render without any factory K or D."""
    rendered = board().draw((1050, 750))
    rendered = cv2.resize(rendered, (1062, 750))
    image = cv2.copyMakeBorder(rendered, 30, 30, 30, 30, cv2.BORDER_CONSTANT, value=255)
    found = extract(image, 177, 125)
    assert found['corner_count'] == 24
    points = np.asarray(found['corners_px'])
    objects = np.asarray(found['object_points_m'])
    homography, _ = cv2.findHomography(objects[:, :2], points, 0)
    projected = cv2.perspectiveTransform(objects[:, None, :2], homography)[:, 0]
    assert np.max(np.linalg.norm(points - projected, axis=1)) < 0.5


def test_blank_frame_is_rejected():
    """A camera frame without a target cannot become a calibration sample."""
    with pytest.raises(ValueError, match='No markers'):
        extract(np.full((720, 1280), 255, dtype=np.uint8), 177, 125)
