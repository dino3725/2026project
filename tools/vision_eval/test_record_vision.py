"""Check trial denominators and repeated-coordinate summaries."""

import pytest

from record_vision import summarize


def test_failed_trials_remain_in_denominator():
    rows = [
        {'success': True, 'x_mm': 10, 'y_mm': 0,
         'z_raw_mm': 20, 'z_adjusted_mm': 45},
        {'success': False},
        {'success': True, 'x_mm': 14, 'y_mm': 2,
         'z_raw_mm': 24, 'z_adjusted_mm': 49},
    ]
    result = summarize(rows)
    assert result['trials'] == 3
    assert result['successes'] == 2
    assert result['x_mm']['mean'] == 12
    assert result['x_mm']['sample_std'] == pytest.approx(8 ** 0.5)
    assert result['z_adjusted_mm']['mean'] == 47


def test_no_success_has_no_coordinate_statistics():
    result = summarize([{'success': False}])
    assert result['successes'] == 0
    assert result['z_raw_mm']['mean'] is None
    assert result['z_raw_mm']['sample_std'] is None
