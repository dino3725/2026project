"""Check signed support-plane height conversion for the historical audit."""

import numpy as np
import pytest

from audit_saved_surface import region_heights
from handeye import transform


def test_downward_camera_preserves_support_frame_and_millimetre_units():
    """An 80 mm lid remains 80 mm after a known optical-to-base transform."""
    depth = np.full((100, 100), 500, dtype=np.uint16)
    k = np.array([[100., 0, 50], [0, 100., 50], [0, 0, 1]])
    camera = transform(np.diag([1., -1., -1.]), [0, 0, .57])
    assert region_heights(depth, [10, 10, 90, 90], k, camera) == pytest.approx(80.)
    camera[2, 3] += .01
    assert region_heights(depth, [10, 10, 90, 90], k, camera) == pytest.approx(90.)


def test_empty_historical_region_cannot_produce_a_height():
    """Reject missing depth instead of interpreting zeros as a real surface."""
    with pytest.raises(ValueError, match='Insufficient'):
        region_heights(np.zeros((100, 100)), [10, 10, 90, 90], np.eye(3), np.eye(4))
