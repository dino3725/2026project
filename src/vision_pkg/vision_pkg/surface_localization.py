"""Conservative surface sampling; outputs are not object centers or grasps."""

from dataclasses import dataclass

import numpy as np

from vision_pkg.vision_utils import deproject_pixel


@dataclass(frozen=True)
class SurfaceEstimate:
    """Describe an observed surface pixel and the evidence supporting it."""

    point: tuple[float, float, float] | None
    pixel: tuple[int, int] | None
    source: str
    valid_fraction: float
    support_fraction: float
    depth_span_m: float
    count: int
    reason: str


def estimate_surface(
    depth_image, detection, encoding, depth_scale, intrinsics,
    window_size=7, cluster_gap_m=0.015, min_support_fraction=0.7,
    max_depth_span_m=0.03,
):
    """
    Select a supported surface point, preserving its pixel-depth pairing.

    Try the central window, then the inner half of the detection. Split depth
    samples at large gaps and require a dominant group; never assume the
    nearest group is the object. Even a dominant group may be background.
    Thresholds are experimental and do not certify segmentation or accuracy.
    """
    image = np.asarray(depth_image)
    k = np.asarray(intrinsics, dtype=float)
    if image.ndim != 2 or k.shape != (4,):
        raise ValueError('Expected 2D depth and fx, fy, cx, cy')
    deproject_pixel(0, 0, 1.0, *k)
    if encoding.upper() not in ('16UC1', '32FC1'):
        raise ValueError('Unsupported depth encoding')
    if not np.isfinite(depth_scale) or depth_scale <= 0:
        raise ValueError('Depth scale must be finite and positive')
    if window_size < 1 or window_size % 2 != 1:
        raise ValueError('Window must be positive and odd')
    if (not np.isfinite([cluster_gap_m, max_depth_span_m]).all()
            or min(cluster_gap_m, max_depth_span_m) <= 0
            or not 0.5 < min_support_fraction <= 1):
        raise ValueError('Invalid surface thresholds')
    bounds = [detection.x1, detection.y1, detection.x2, detection.y2]
    if (not np.isfinite(bounds).all() or detection.x2 <= detection.x1
            or detection.y2 <= detection.y1):
        raise ValueError('Invalid detection bounds')
    height, width = image.shape
    u, v = detection.center
    empty = SurfaceEstimate(None, None, 'none', 0., 0., 0., 0,
                            'center outside image')
    if not (0 <= u < width and 0 <= v < height):
        return empty
    radius = window_size // 2
    regions = [
        ('center', radius, radius),
        ('inner', max(1, int((detection.x2 - detection.x1) / 4)),
         max(1, int((detection.y2 - detection.y1) / 4))),
    ]
    result = empty
    for source, rx, ry in regions:
        x0, x1 = max(0, u - rx), min(width, u + rx + 1)
        y0, y1 = max(0, v - ry), min(height, v + ry + 1)
        vv, uu = np.mgrid[y0:y1, x0:x1]
        z = image[y0:y1, x0:x1].astype(float).ravel()
        if encoding.upper() == '16UC1':
            z *= depth_scale
        valid = np.isfinite(z) & (z > 0)
        fraction = float(valid.mean())
        z, uu, vv = z[valid], uu.ravel()[valid], vv.ravel()[valid]
        result = SurfaceEstimate(None, None, source, fraction, 0., 0.,
                                 len(z), 'insufficient valid depth')
        if len(z) < 10 or fraction < 0.25:
            continue
        order = np.argsort(z)
        groups = np.split(order, np.where(np.diff(z[order]) > cluster_gap_m)[0] + 1)
        group = max(groups, key=len)
        support = len(group) / len(z)
        span = float(np.ptp(np.percentile(z[group], [10, 90])))
        reason = ''
        if len(group) < 10 or support < min_support_fraction:
            reason = 'ambiguous depth groups'
        elif span > max_depth_span_m:
            reason = 'surface depth spread too large'
        result = SurfaceEstimate(None, None, source, fraction, support, span,
                                 len(group), reason)
        if reason:
            continue
        fx, fy, cx, cy = k
        points = np.column_stack(((uu[group] - cx) * z[group] / fx,
                                  (vv[group] - cy) * z[group] / fy, z[group]))
        # Pick an actual point near the robust center, not a fabricated ray.
        index = int(np.argmin(np.linalg.norm(
            points - np.median(points, axis=0), axis=1)))
        pixel = (int(uu[group[index]]), int(vv[group[index]]))
        return SurfaceEstimate(tuple(float(a) for a in points[index]), pixel,
                               source, fraction, support, span, len(group), '')
    return result


class LocalizationWindow:
    """Bound samples by time and reset after gaps; never certify accuracy."""

    def __init__(self, duration=2.0, horizon=3.0, max_gap=0.5,
                 min_samples=10, max_spread_m=0.01):
        values = [duration, horizon, max_gap, max_spread_m]
        if (not np.isfinite(values).all() or min(values) <= 0
                or horizon <= duration or min_samples < 2):
            raise ValueError('Invalid localization window settings')
        self.duration = duration
        self.horizon = horizon
        self.max_gap = max_gap
        self.min_samples = min_samples
        self.max_spread_m = max_spread_m
        self.samples = []
        self.coverage = 0.0
        self.spread_m = 0.0
        self.reason = 'not enough recent samples'
        self._last_update = None

    def update(self, stamp, sample=None):
        """Return a robust result only from sufficiently stable recent samples."""
        if not np.isfinite(stamp):
            raise ValueError('Timestamp must be finite')
        if self._last_update is not None and stamp <= self._last_update:
            raise ValueError('Timestamps must strictly increase')
        if sample is not None:
            a = np.asarray(sample, dtype=float)
            if a.shape != (4,) or not np.isfinite(a).all():
                raise ValueError('Expected finite x, y, z, confidence')
        self._last_update = stamp
        self.expire(stamp)
        if sample is None:
            self.invalidate('observation unavailable')
            return None
        if sample is not None:
            self.samples.append((stamp, tuple(sample)))
        self.coverage = (self.samples[-1][0] - self.samples[0][0]
                         if len(self.samples) > 1 else 0.)
        self.spread_m = 0.0
        self.reason = 'not enough recent samples'
        if sample is None or len(self.samples) < self.min_samples:
            return None
        values = np.array([s for _, s in self.samples])
        median = np.median(values, axis=0)
        self.spread_m = float(np.percentile(
            np.linalg.norm(values[:, :3] - median[:3], axis=1), 90))
        if self.coverage < self.duration:
            self.reason = 'recent duration too short'
            return None
        if self.spread_m > self.max_spread_m:
            self.reason = 'recent coordinates unstable'
            return None
        self.reason = ''
        return tuple(float(v) for v in median)

    def invalidate(self, reason):
        """Discard prior evidence after an explicit failed observation."""
        self.samples.clear()
        self.coverage = 0.
        self.spread_m = 0.
        self.reason = reason

    def expire(self, stamp):
        """Drop stale samples even while no frames or detections arrive."""
        if not np.isfinite(stamp):
            raise ValueError('Timestamp must be finite')
        self.samples = [(t, s) for t, s in self.samples
                        if stamp - t <= self.horizon]
        if self.samples and stamp - self.samples[-1][0] > self.max_gap:
            self.samples.clear()
        self.coverage = (self.samples[-1][0] - self.samples[0][0]
                         if len(self.samples) > 1 else 0.)
        if not self.samples:
            self.spread_m = 0.
            self.reason = 'no recent samples'
