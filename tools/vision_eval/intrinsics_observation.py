"""Extract calibration pixels without using the factory camera intrinsics."""

import cv2
import numpy as np

from handeye import board


def metric_corners(indices, width_mm, height_mm):
    """Scale the printed grid separately along its two measured axes."""
    dimensions = np.asarray([width_mm, height_mm], dtype=float)
    if not np.isfinite(dimensions).all() or np.any(dimensions <= 0):
        raise ValueError('Printed dimensions must be finite and positive')
    indices = np.asarray(indices)
    if (indices.ndim != 1 or len(indices) == 0
            or not np.issubdtype(indices.dtype, np.integer)
            or len(np.unique(indices)) != len(indices)
            or np.any(indices < 0) or np.any(indices >= 24)):
        raise ValueError('Expected unique integer ChArUco ids in [0, 23]')
    points = board().chessboardCorners[indices].copy()
    points[:, 0] *= width_mm / 175.0
    points[:, 1] *= height_mm / 125.0
    return points


def extract(image, width_mm, height_mm):
    """Use local homographies for detection; return pixels and physical points."""
    target = board()
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY) if image.ndim == 3 else image
    corners, marker_ids, _ = cv2.aruco.detectMarkers(gray, target.dictionary)
    if marker_ids is None:
        raise ValueError('No markers visible')
    # No cameraMatrix/distCoeffs: avoid using the intrinsics under investigation
    # to interpolate the observations. Do not reject using factory PnP residuals.
    count, points, ids = cv2.aruco.interpolateCornersCharuco(
        corners, marker_ids, gray, target)
    if count < 12 or cv2.aruco.testCharucoCornersCollinear(target, ids):
        raise ValueError('Need at least 12 noncollinear ChArUco corners')
    ids = ids.flatten()
    pixels = points.reshape(-1, 2)
    height, width = gray.shape
    return {
        'corner_ids': ids.tolist(), 'corners_px': pixels.tolist(),
        'object_points_m': metric_corners(ids, width_mm, height_mm).tolist(),
        'corner_count': int(count),
        'corner_hull_fraction': float(cv2.contourArea(cv2.convexHull(pixels))
                                      / (width * height)),
        'corner_bounds_px': [pixels.min(axis=0).tolist(), pixels.max(axis=0).tolist()],
        'detection': 'local_homography_without_camera_intrinsics',
    }
