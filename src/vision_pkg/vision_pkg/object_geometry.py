"""Experimental upright-cylinder axis estimation from observed 3D surfaces."""

import numpy as np


def fit_upright_axis(points_base, camera_base, support_z, height, radius,
                     max_residual_p90_m=0.005):
    """
    Fit a known-radius horizontal circle to body points in base coordinates.

    Requires an upright cylinder and a known support plane parallel to base XY.
    Input points must already belong to a target ROI. This is not a general
    object segmenter. A small residual does not certify absolute accuracy.
    """
    points = np.asarray(points_base, dtype=float)
    camera = np.asarray(camera_base, dtype=float)
    if points.ndim != 2 or points.shape[1] != 3 or camera.shape != (3,):
        raise ValueError('Expected Nx3 points and a 3D camera position')
    if (not np.isfinite([support_z, height, radius, max_residual_p90_m]).all()
            or not np.isfinite(camera).all()
            or min(height, radius, max_residual_p90_m) <= 0):
        raise ValueError('Invalid cylinder geometry')
    body = points[np.isfinite(points).all(axis=1)]
    body = body[(body[:, 2] > support_z + .15 * height)
                & (body[:, 2] < support_z + .85 * height)]
    result = {'accepted': False, 'axis_xy_m': None, 'body_points': len(body)}
    if len(body) < 30 or np.ptp(body[:, 2]) < .15 * height:
        return dict(result, reason='insufficient body height coverage')
    xy = body[:, :2]
    surface = np.median(xy, axis=0)
    away = surface - camera[:2]
    distance = np.linalg.norm(away)
    if distance < 2 * radius:
        return dict(result, reason='camera too close to axis hypothesis')
    center = surface + radius * away / distance
    for _ in range(20):
        offset = center - xy
        lengths = np.maximum(np.linalg.norm(offset, axis=1), 1e-9)
        residual = lengths - radius
        weights = np.minimum(1., .002 / np.maximum(np.abs(residual), 1e-9))
        jacobian = offset / lengths[:, None]
        step = np.linalg.lstsq(jacobian * np.sqrt(weights[:, None]),
                               -residual * np.sqrt(weights), rcond=None)[0]
        # Reject unstable fits instead of accepting an arbitrary distant center.
        if np.linalg.norm(step) > 2 * radius:
            return dict(result, reason='unstable axis fit')
        center += step
        if np.linalg.norm(step) < 1e-7:
            break
    distances = np.linalg.norm(xy - center, axis=1)
    errors = np.abs(distances - radius)
    inliers = errors <= .005
    fraction = float(inliers.mean())
    result.update(inlier_fraction=fraction,
                  axis_hypothesis_xy_m=center.tolist(),
                  residual_p90_m=float(np.percentile(errors, 90)),
                  max_residual_p90_m=float(max_residual_p90_m))
    if fraction < .75 or inliers.sum() < 30:
        return dict(result, reason='cylinder does not explain ROI body points')
    if result['residual_p90_m'] > max_residual_p90_m:
        return dict(result, reason='body residual exceeds quality limit')
    vectors = (xy[inliers] - center) / np.maximum(distances[inliers, None], 1e-9)
    condition = float(np.linalg.cond(vectors.T @ vectors))
    if not np.isfinite(condition):
        return dict(result, reason='insufficient visible arc')
    view = camera[:2] - center
    view /= np.linalg.norm(view)
    visible_fraction = float((vectors @ view > 0).mean())
    result.update(fit_condition=condition, visible_fraction=visible_fraction)
    if condition > 50 or visible_fraction < .8:
        return dict(result, reason='insufficient visible arc or wrong side')
    return dict(result, accepted=True, axis_xy_m=center.tolist(), reason='')
