"""Verify intrinsic recovery and separation of training from validation data."""

from copy import deepcopy

import cv2
import numpy as np
import pytest

from handeye import BOARD_SPEC
from intrinsics_observation import metric_corners
from solve_intrinsics import evaluate, fit, prepare


def synthetic():
    """Project the measured rectangular grid from diverse known camera poses."""
    k = np.array([[920., 0, 640], [0, 905, 360], [0, 0, 1]])
    obj = metric_corners(list(range(24)), 177, 125)
    records = []
    for index in range(15):
        rv = np.array([0.4 * np.sin(index), 0.5 * np.cos(index), 0.1 * index])
        tv = np.array([-0.09 + 0.09 * np.sin(index * 2),
                       -0.06 + 0.06 * np.cos(index), 0.5 + 0.08 * np.sin(index)])
        pixels = cv2.projectPoints(obj, rv, tv, k, np.zeros(5))[0].reshape(-1, 2)
        records.append({
            'role': 'train', 'session_id': 'synthetic', 'rgb_stamp_ns': index,
            'color_info': {'k': [910., 0, 630, 0, 910, 370, 0, 0, 1],
                           'd': [0.] * 5, 'width': 1280, 'height': 720,
                           'model': 'plumb_bob'},
            'nominal_board': BOARD_SPEC, 'measured_pattern_size_mm': [177, 125],
            'corner_ids': list(range(24)), 'corners_px': pixels.tolist(),
            'object_points_m': obj.tolist(),
        })
    return records, k


def test_recovers_known_intrinsics_and_predicts_unfitted_corners():
    """Diverse planar views recover the known camera without robot geometry."""
    records, expected = synthetic()
    result = fit(records[:12], 'pinhole')
    assert np.allclose(result['k'], expected, atol=0.1)
    score = evaluate(records[12:], result['k'], result['d'])
    assert score['split_corner_rms_px'] < 0.001
    assert result['applied'] is False


def test_refuses_validation_or_audit_in_fit():
    """Holdout and preview observations must never enter candidate fitting."""
    records, _ = synthetic()
    for role in ['validation', 'audit']:
        records[-1]['role'] = role
        with pytest.raises(ValueError, match='training-only'):
            fit(records, 'pinhole')


def test_mixed_camera_metadata_rejected():
    """A changed stream cannot silently mix with the earlier observations."""
    records, _ = synthetic()
    records[-1] = deepcopy(records[-1])
    records[-1]['color_info']['k'][0] += 1
    with pytest.raises(ValueError, match='Mixed'):
        prepare(records)


def test_wrong_metric_geometry_rejected():
    """Nominal square coordinates cannot replace measured printed dimensions."""
    records, _ = synthetic()
    records[0]['object_points_m'][0][0] += 0.001
    with pytest.raises(ValueError, match='correspondences'):
        prepare(records)


def test_missing_corners_can_be_evaluated_without_imputation():
    """A partial ChArUco view retains valid split-corner pose prediction."""
    records, k = synthetic()
    r = records[0]
    for key in ['corner_ids', 'object_points_m', 'corners_px']:
        r[key] = [v for i, v in enumerate(r[key]) if i not in [3, 4, 9, 10]]
    assert evaluate([r], k, np.zeros(5))['split_corner_rms_px'] < 0.001
