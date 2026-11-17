"""Do not label stale or changing arm feedback as stationary capture data."""

from capture_stationarity import stationary


def test_stationary_samples_and_encoder_noise():
    rows = [(i / 10, [i % 2 * .001, 0, 0, 0]) for i in range(16)]
    assert stationary(rows, 1.5)
    assert not stationary(rows, 2.)
    assert not stationary(rows[-3:], 1.5)


def test_moving_missing_and_nonfinite_samples():
    rows = [(i / 10, [i * .001, 0, 0, 0]) for i in range(16)]
    assert not stationary(rows, 1.5)
    rows = [(i / 10, [0, 0, 0, 0]) for i in range(16)]
    assert not stationary(rows[:4] + rows[10:], 1.5)
    rows[-1][1][0] = float('nan')
    assert not stationary(rows, 1.5)
