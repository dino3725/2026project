"""ChArUco detection and eye-in-hand geometry; no ROS or actuator operations."""

import cv2
import numpy as np
from scipy.spatial.transform import Rotation


BOARD_SPEC = {
    'opencv_version': '4.5.4', 'dictionary': 'DICT_4X4_50',
    'squares_x': 7, 'squares_y': 5,
    'square_length_m': 0.025, 'marker_length_m': 0.018,
    'board_width_mm': 175, 'board_height_mm': 125,
}


def board():
    """Use the installed legacy board convention, avoiding silent version flips."""
    if cv2.__version__ != BOARD_SPEC['opencv_version']:
        raise ValueError('Use system Python with OpenCV 4.5.4 for this printed board')
    return cv2.aruco.CharucoBoard_create(
        7, 5, 0.025, 0.018, cv2.aruco.Dictionary_get(cv2.aruco.DICT_4X4_50))


def transform(rotation, translation):
    """Build T_parent_child: convert coordinates from child to parent."""
    result = np.eye(4)
    result[:3, :3] = rotation
    result[:3, 3] = np.asarray(translation).reshape(3)
    return result


def validate_transform(value):
    """Reject nonfinite, scaled or reflected transforms before solving."""
    value = np.asarray(value, dtype=float)
    if value.shape != (4, 4) or not np.isfinite(value).all():
        raise ValueError('Expected finite 4x4 transform')
    r = value[:3, :3]
    if (not np.allclose(value[3], [0, 0, 0, 1], atol=1e-8)
            or not np.allclose(r.T @ r, np.eye(3), atol=1e-6)
            or not np.isclose(np.linalg.det(r), 1, atol=1e-6)):
        raise ValueError('Expected rigid right-handed transform')
    return value


def detect(image, k, d):
    """Estimate board pose in RGB optical frame and report reprojection RMS."""
    target = board()
    k = np.asarray(k, dtype=float).reshape(3, 3)
    d = np.asarray(d, dtype=float)
    if not np.isfinite(k).all() or not np.isfinite(d).all() or min(k[0, 0], k[1, 1]) <= 0:
        raise ValueError('Invalid camera intrinsics')
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY) if image.ndim == 3 else image
    corners, ids, _ = cv2.aruco.detectMarkers(gray, target.dictionary)
    if ids is None:
        raise ValueError('No ChArUco markers detected')
    count, points, indices = cv2.aruco.interpolateCornersCharuco(
        corners, ids, gray, target, cameraMatrix=k, distCoeffs=d)
    if count < 12 or cv2.aruco.testCharucoCornersCollinear(target, indices):
        raise ValueError('Need at least 12 noncollinear ChArUco corners')
    obj = target.chessboardCorners[indices.flatten()]
    ok, rvec, tvec = cv2.solvePnP(obj, points, k, d)
    if not ok:
        raise ValueError('Board PnP failed')
    r = cv2.Rodrigues(rvec)[0]
    if np.min((r @ obj.T + tvec)[2]) <= 0:
        raise ValueError('Board pose is behind the camera')
    projected = cv2.projectPoints(obj, rvec, tvec, k, d)[0]
    rms = float(np.sqrt(np.mean(np.sum((points - projected) ** 2, axis=2))))
    if not np.isfinite(rms) or rms > 1.0:
        raise ValueError(f'Board reprojection RMS too high: {rms:.3f} px')
    return {
        'T_optical_board': transform(r, tvec).tolist(),
        'corner_count': int(count), 'reprojection_rms_px': rms,
        'corner_ids': indices.flatten().tolist(),
        'corners_px': points.reshape(-1, 2).tolist(),
    }


def check_diversity(poses):
    """Require appreciable rotations around at least two independent axes."""
    vectors = [Rotation.from_matrix(a[:3, :3].T @ b[:3, :3]).as_rotvec()
               for i, a in enumerate(poses) for b in poses[i + 1:]]
    vectors = np.asarray([v for v in vectors if np.linalg.norm(v) >= np.deg2rad(5)])
    if len(vectors) < 3:
        raise ValueError('Insufficient rotation diversity; fixed-pose repeats cannot calibrate')
    singular = np.linalg.svd(vectors, compute_uv=False)
    if singular[1] / singular[0] < 0.1:
        raise ValueError('Rotations are nearly parallel; need another rotation axis')
    return singular.tolist()


def solve(ee_poses, board_poses):
    """Solve X=T_ee_optical using T_base_ee and T_optical_board observations."""
    if len(ee_poses) != len(board_poses) or len(ee_poses) < 8:
        raise ValueError('Need at least 8 paired training poses')
    ee = [validate_transform(t) for t in ee_poses]
    targets = [validate_transform(t) for t in board_poses]
    singular = check_diversity(ee)
    r, t = cv2.calibrateHandEye(
        [p[:3, :3] for p in ee], [p[:3, 3] for p in ee],
        [p[:3, :3] for p in targets], [p[:3, 3] for p in targets],
        method=cv2.CALIB_HAND_EYE_PARK)
    return validate_transform(transform(r, t)), singular


def consistency(ee_poses, board_poses, mount, reference=None):
    """Measure fixed-board disagreement; held-out poses use training reference."""
    worlds = [validate_transform(e) @ mount @ validate_transform(b)
              for e, b in zip(ee_poses, board_poses)]
    if not worlds:
        raise ValueError('No validation poses')
    if reference is None:
        reference = transform(
            Rotation.from_matrix(np.array([w[:3, :3] for w in worlds])).mean().as_matrix(),
            np.mean([w[:3, 3] for w in worlds], axis=0))
    distances = [float(np.linalg.norm(w[:3, 3] - reference[:3, 3]) * 1000) for w in worlds]
    angles = [float(np.rad2deg(Rotation.from_matrix(
        reference[:3, :3].T @ w[:3, :3]).magnitude())) for w in worlds]
    return {'translation_errors_mm': distances, 'rotation_errors_deg': angles,
            'max_translation_mm': max(distances), 'max_rotation_deg': max(angles)}, reference
