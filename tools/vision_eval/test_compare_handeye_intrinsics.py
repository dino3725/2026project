"""Check metric recovery and held-out isolation in the offline comparison."""

import cv2
import numpy as np
from scipy.spatial.transform import Rotation

from compare_handeye_intrinsics import calculate
from handeye import transform
from intrinsics_observation import metric_corners


def scene():
    """Create known rigid geometry and distortion with twelve diverse views."""
    rng = np.random.default_rng(413)
    k = np.array([[925., 0, 640], [0, 927, 362], [0, 0, 1]])
    d = np.array([.22, -.35, 0, 0, 0])
    mount = transform(Rotation.from_euler('xyz', [.1, -.05, .08]).as_matrix(),
                      [.02, -.03, .05])
    fixed = transform(Rotation.from_euler('xyz', [.05, .03, -.02]).as_matrix(), [0, 0, .6])
    obj = metric_corners(list(range(24)), 177, 125).astype(float)
    records, pixels = [], []
    for i in range(12):
        e = transform(Rotation.from_rotvec(rng.uniform(-.2, .2, 3)).as_matrix(),
                      rng.uniform(-.035, .035, 3))
        ob = np.linalg.inv(e @ mount) @ fixed
        pixels.append(cv2.projectPoints(obj, cv2.Rodrigues(ob[:3, :3])[0], ob[:3, 3],
                                        k, d)[0].reshape(-1, 2))
        records.append({'role': 'train' if i < 9 else 'validation',
                        'T_base_ee': e.tolist(), 'T_camera_optical': np.eye(4).tolist()})
    return records, [obj] * 12, pixels, k, d, mount, fixed


def test_recovers_geometry_without_forcing_support_height():
    """The fitted height must follow observations even when it is far from zero."""
    records, objects, pixels, k, d, mount, fixed = scene()
    result = calculate(records, objects, pixels, k, d, 177, 125)
    expected_height = ((fixed @ np.array([.0885, .0625, 0, 1]))[2] + .01) * 1000
    for method in result['methods'].values():
        assert np.allclose(method['T_ee_optical'], mount, atol=1e-5)
        assert abs(method['board_center_above_support_mm'] - expected_height) < .01
        assert method['reused_validation']['max_translation_mm'] < .01


def test_held_out_pixel_change_does_not_refit_mount():
    """Perturbing validation data changes its errors, never the training solution."""
    records, objects, pixels, k, d, _, _ = scene()
    first = calculate(records, objects, pixels, k, d, 177, 125)
    changed = [p.copy() for p in pixels]
    changed[-1] += [8, -4]
    second = calculate(records, objects, changed, k, d, 177, 125)
    for label in first['methods']:
        a, b = first['methods'][label], second['methods'][label]
        assert np.allclose(a['T_ee_optical'], b['T_ee_optical'], atol=1e-12)
        assert b['reused_validation']['max_translation_mm'] > 1
