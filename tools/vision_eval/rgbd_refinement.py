"""Fit fixed-arm camera geometry from saved RGB and aligned depth, without ROS."""

import cv2
import numpy as np
from scipy.optimize import least_squares
from scipy.spatial.transform import Rotation

from handeye import transform
from refine_handeye import pack, unpack


def fit_plane(points):
    """Fit a deterministic robust plane and reject insufficient planar support."""
    points = np.asarray(points, dtype=float)
    if points.ndim != 2 or points.shape[1] != 3 or len(points) < 100:
        raise ValueError('Need at least 100 depth points')
    if not np.isfinite(points).all():
        raise ValueError('Nonfinite depth points')
    rng = np.random.default_rng(12026)
    best = np.zeros(len(points), dtype=bool)
    for _ in range(120):
        a, b, c = points[rng.choice(len(points), 3, replace=False)]
        normal = np.cross(b - a, c - a)
        length = np.linalg.norm(normal)
        if length < 1e-8:
            continue
        keep = np.abs((points - a) @ (normal / length)) < .003
        if keep.sum() > best.sum():
            best = keep
    if best.mean() < .7:
        raise ValueError('Insufficient planar depth support')
    for _ in range(4):
        center = points[best].mean(axis=0)
        _, values, vt = np.linalg.svd(points[best] - center, full_matrices=False)
        if values[1] < .01:
            raise ValueError('Degenerate depth plane')
        normal = vt[-1]
        errors = (points - center) @ normal
        best = np.abs(errors) < .003
        if best.mean() < .7:
            raise ValueError('Insufficient refined planar support')
    center = points[best].mean(axis=0)
    normal = np.linalg.svd(points[best] - center, full_matrices=False)[2][-1]
    if normal[2] > 0:
        normal = -normal
    offset = -normal @ center
    rms = np.sqrt(np.mean((points[best] @ normal + offset) ** 2))
    return {'normal': normal, 'offset_m': float(offset), 'inlier_fraction': float(best.mean()),
            'inlier_rms_mm': float(rms * 1000), 'point_count': len(points)}


def plane_intersections(pixels, k, plane):
    """Intersect factory-camera rays with an observed aligned-depth plane."""
    rays = np.c_[pixels, np.ones(len(pixels))] @ np.linalg.inv(k).T
    denominator = rays @ plane['normal']
    if np.any(np.abs(denominator) < 1e-6):
        raise ValueError('Ray parallel to depth plane')
    z = -plane['offset_m'] / denominator
    if np.any(z <= 0) or not np.isfinite(z).all():
        raise ValueError('Plane intersection behind camera')
    return rays * z[:, None]


def extract_depth(depth, pixels, info):
    """Use the eroded inner-corner hull, preserving recorded aligned intrinsics."""
    if (depth is None or depth.dtype != np.uint16
            or depth.shape != (info['height'], info['width'])):
        raise ValueError('Expected recorded 16UC1 aligned depth dimensions')
    if info['model'] != 'plumb_bob' or np.any(np.array(info['d']) != 0):
        raise ValueError('This depth path requires recorded zero-distortion intrinsics')
    k = np.array(info['k']).reshape(3, 3)
    hull = cv2.convexHull(np.asarray(pixels, dtype=np.float32)).astype(np.int32)
    mask = np.zeros(depth.shape, dtype=np.uint8)
    cv2.fillConvexPoly(mask, hull, 255)
    mask = cv2.erode(mask, np.ones((9, 9), dtype=np.uint8))
    vv, uu = np.where((mask > 0) & (depth > 200) & (depth < 1200))
    vv, uu = vv[::4], uu[::4]
    z = depth[vv, uu].astype(float) * .001
    points = np.c_[(uu - k[0, 2]) * z / k[0, 0], (vv - k[1, 2]) * z / k[1, 1], z]
    plane = fit_plane(points)
    return plane, plane_intersections(pixels, k, plane)


def grouped_folds(rows, count=5, tolerance_deg=3):
    """Keep connected near-identical poses together across sessions during CV."""
    if any(row['role'] != 'train' for row in rows):
        raise ValueError('Cross-validation uses training observations only')
    groups = [{i} for i in range(len(rows))]
    q = np.rad2deg(np.array([row['q'] for row in rows]))
    for i in range(len(rows)):
        for j in range(i):
            if np.max(np.abs(q[i] - q[j])) <= tolerance_deg:
                a = next(g for g in groups if i in g)
                b = next(g for g in groups if j in g)
                if a is not b:
                    a.update(b)
                    groups.remove(b)
    if len(groups) < count:
        raise ValueError('Not enough independent pose groups for requested folds')
    folds = [[] for _ in range(count)]
    for group in sorted(groups, key=lambda g: (-len(g), min(g))):
        smallest = min(range(count), key=lambda i: (len(folds[i]), i))
        folds[smallest].extend(sorted(group))
    return [sorted(f) for f in folds]


def initialize(rows, k, d, nominal_mount):
    """Initialize from this training subset only, never a full-data fit."""
    boards = []
    for session in range(2):
        observed = []
        for row in rows:
            if row['session_index'] != session:
                continue
            ok, rv, tv = cv2.solvePnP(row['objects'], row['pixels'], k, d)
            if not ok:
                raise ValueError('Initialization PnP failed')
            observed.append(row['ee'] @ nominal_mount @ transform(cv2.Rodrigues(rv)[0], tv))
        if len(observed) < 4:
            raise ValueError('Need four training poses per session')
        observed = np.array(observed)
        boards.append(transform(Rotation.from_matrix(observed[:, :3, :3]).mean().as_matrix(),
                                np.median(observed[:, :3, 3], axis=0)))
    return np.concatenate([pack(nominal_mount)] + [pack(b) for b in boards])


def predict(parameters, row):
    """Predict the board in camera coordinates with fixed recorded arm TF."""
    board = unpack(parameters[6 + 6 * row['session_index']:12 + 6 * row['session_index']])
    optical_board = np.linalg.inv(unpack(parameters[:6])) @ row['inverse_ee'] @ board
    return optical_board, row['objects'] @ optical_board[:3, :3].T + optical_board[:3, 3]


def objective(parameters, rows, k, d, depth_scale_mm):
    """Balance per-view pixels and optional plane distances; no height constraint."""
    errors = []
    for row in rows:
        ob, points = predict(parameters, row)
        pixels = cv2.projectPoints(row['objects'], cv2.Rodrigues(ob[:3, :3])[0],
                                   ob[:3, 3], k, d)[0].reshape(-1, 2)
        weight = np.sqrt(24 / len(points))
        errors.append(((pixels - row['pixels']) * weight).ravel())
        if depth_scale_mm is not None:
            plane = row['plane']
            distances = (points @ plane['normal'] + plane['offset_m']) * 1000
            errors.append(distances * weight / depth_scale_mm)
    return np.concatenate(errors)


def fit_fixed_arm(rows, k, d, nominal_mount, loss='linear', depth_scale_mm=None):
    """Fit a shared mount and two free boards, rejecting evaluation-role data."""
    if any(r['role'] != 'train' for r in rows) or len(rows) < 8:
        raise ValueError('Training-only observations required')
    if depth_scale_mm is not None and depth_scale_mm <= 0:
        raise ValueError('Depth scale must be positive')
    initial = initialize(rows, k, d, nominal_mount)
    fitted = least_squares(objective, initial, args=(rows, k, d, depth_scale_mm),
                           loss=loss, f_scale=1, x_scale='jac', max_nfev=300,
                           ftol=1e-9, xtol=1e-9, gtol=1e-9)
    if not fitted.success or not np.isfinite(fitted.x).all():
        raise ValueError('Fixed-arm fit did not converge')
    if any(np.any(predict(fitted.x, row)[1][:, 2] <= 0) for row in rows):
        raise ValueError('Fitted board behind camera')
    return fitted.x


def evaluate(parameters, rows, k, d):
    """Evaluate fixed geometry using pixels, depth planes, and 3D corner agreement."""
    output = []
    mount = unpack(parameters[:6])
    for row in rows:
        ob, points = predict(parameters, row)
        if np.any(points[:, 2] <= 0):
            raise ValueError('Evaluation board behind camera')
        image = cv2.projectPoints(row['objects'], cv2.Rodrigues(ob[:3, :3])[0],
                                  ob[:3, 3], k, d)[0].reshape(-1, 2)
        plane = row['plane']
        plane_mm = (points @ plane['normal'] + plane['offset_m']) * 1000
        corner_mm = np.linalg.norm(points - row['depth_corners'], axis=1) * 1000
        world = row['ee'] @ mount
        center = row['depth_corners'].mean(axis=0)
        world_height = (world @ np.r_[center, 1])[2] * 1000 + 10
        output.append({'name': row['name'], 'role': row['role'],
                       'pixel_rms_px': float(np.sqrt(np.mean(np.sum(
                           (image - row['pixels']) ** 2, axis=1)))),
                       'plane_rms_mm': float(np.sqrt(np.mean(plane_mm ** 2))),
                       'corner_3d_rms_mm': float(np.sqrt(np.mean(corner_mm ** 2))),
                       'depth_corner_centroid_height_mm': float(world_height)})
    return output


def score(rows):
    """Rank candidates by declared image/plane balance, not inferred table height."""
    return float(np.mean([(r['pixel_rms_px'] ** 2 + (r['plane_rms_mm'] / 3) ** 2) / 2
                          for r in rows]) ** .5)


def summarize(rows):
    """Keep pixel, depth, and coordinate metrics separate for interpretation."""
    return {key: {'mean': float(np.mean([r[key] for r in rows])),
                  'max': float(np.max([r[key] for r in rows]))}
            for key in ['pixel_rms_px', 'plane_rms_mm', 'corner_3d_rms_mm']}
