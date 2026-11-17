"""Ensure trial counts, missing poses and signed errors remain explicit."""

from summarize_pose_trials import summarize


def pose(trial, label, value):
    return dict(trial=trial, kind='pose', label=label,
                mean_error_deg=[value] * 4,
                mean_absolute_error_deg=[abs(value)] * 4,
                max_absolute_error_deg=[abs(value)] * 4,
                nominal_pass=False)


def test_missing_stages_and_return_home_are_not_filled_or_mixed():
    result = summarize([
        dict(trial=1, kind='started'), pose(1, 'HOME', 2),
        pose(1, 'RECOGNITION', 1), pose(1, 'HOME', -1),
        dict(trial=1, kind='finished'), dict(trial=2, kind='started'),
        pose(2, 'HOME', 4), dict(trial=2, kind='failed', error='planning failed')])
    assert result['poses']['HOME_START']['mean_signed_error_deg'] == [3] * 4
    assert result['poses']['HOME_END']['mean_signed_error_deg'] == [-1] * 4
    assert result['poses']['PICK']['count'] == 0
    assert 'mean_signed_error_deg' not in result['poses']['PICK']
    assert result['completed_trials'] == [1]
    assert result['attempted_trials'] == [1, 2]
