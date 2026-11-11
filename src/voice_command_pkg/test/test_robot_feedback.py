"""Check admission, response ordering and feedback independently of ROS."""

import pytest

from voice_command_pkg.robot_feedback import (
    DEFAULT_MESSAGES,
    render_feedback,
    ROBOT_STATES,
    RobotFeedback,
    validate_templates,
)


def idle_robot():
    robot = RobotFeedback(response_timeout_seconds=5)
    robot.observe_state('IDLE', 10)
    return robot


def test_unknown_state_and_speech_before_first_state_are_blocked():
    robot = RobotFeedback()
    assert robot.fetch_drop_reason(1) == 'robot_unknown'
    robot.observe_state('IDLE', 10)
    assert robot.fetch_drop_reason(9) == 'robot_before_idle'
    assert robot.fetch_drop_reason(10) == 'robot_before_idle'
    assert robot.fetch_drop_reason(11) == ''


@pytest.mark.parametrize('state', sorted(ROBOT_STATES - {'IDLE'}))
def test_non_idle_states_block_fetch(state):
    robot = idle_robot()
    robot.observe_state(state, 20)
    expected = 'robot_error' if state == 'ERROR' else 'robot_busy'
    assert robot.fetch_drop_reason(21) == expected


def test_unknown_enum_does_not_keep_old_idle_permission():
    robot = idle_robot()
    robot.observe_state('NEW_UNSUPPORTED_STATE', 20)
    assert robot.fetch_drop_reason(21) == 'robot_unknown'


def test_long_idle_and_duplicate_idle_samples_do_not_expire_permission():
    robot = idle_robot()
    robot.observe_state('IDLE', 100000)
    assert robot.fetch_drop_reason(99999) == ''
    assert robot.tick(100001) == []


def test_busy_speech_does_not_replay_when_idle_returns():
    robot = idle_robot()
    robot.observe_state('PICKING', 20)
    robot.observe_state('IDLE', 30)
    assert robot.fetch_drop_reason(25) == 'robot_before_idle'
    assert robot.fetch_drop_reason(31) == ''


def test_send_reserves_before_state_or_status_and_accept_does_not_unlock():
    robot = idle_robot()
    robot.mark_sent('fetch', 'coke', 11)
    assert robot.fetch_drop_reason(12) == 'robot_pending'
    assert robot.observe_status('fetch', 'coke', True, 12) == [('robot_accepted', 'coke')]
    robot.observe_state('IDLE', 13)  # Repeated old IDLE is not job completion.
    assert robot.fetch_drop_reason(14) == 'robot_pending'
    assert robot.observe_status('fetch', 'coke', True, 14) == []
    assert robot.tick(100) == []


@pytest.mark.parametrize('status_first', [True, False])
def test_status_and_state_order_both_produce_one_accept_and_one_carry(status_first):
    robot = idle_robot()
    robot.mark_sent('fetch', 'coke', 11)
    events = []
    if status_first:
        events += robot.observe_status('fetch', 'coke', True, 12)
    events += robot.observe_state('MOVE_TO_RECOGNITION', 13)
    events += robot.observe_state('DRIVE_TO_PLACE', 14)
    if not status_first:
        events += robot.observe_status('fetch', 'coke', True, 15)
    events += robot.observe_state('DRIVE_TO_PLACE', 16)
    assert events == [('robot_accepted', 'coke'), ('robot_carrying', 'coke')]
    robot.observe_state('RETURN_HOME', 17)
    robot.observe_state('IDLE', 18)
    assert robot.pending is None
    assert robot.fetch_drop_reason(17) == 'robot_before_idle'
    assert robot.fetch_drop_reason(19) == ''
    assert robot.observe_status('fetch', 'coke', True, 20) == []


def test_rejection_releases_only_new_speech_and_unmatched_status_is_ignored():
    robot = idle_robot()
    robot.mark_sent('fetch', 'tissue', 11)
    assert robot.observe_status('fetch', 'coke', False, 12) == []
    assert robot.fetch_drop_reason(13) == 'robot_pending'
    assert robot.observe_status('fetch', 'tissue', False, 14) == [('robot_rejected', '')]
    assert robot.fetch_drop_reason(13) == 'robot_before_idle'
    assert robot.fetch_drop_reason(15) == ''


def test_timeout_never_resends_or_releases_on_duplicate_idle_or_late_status():
    robot = idle_robot()
    robot.mark_sent('fetch', 'coke', 11)
    assert robot.tick(15) == []
    assert robot.tick(16) == [('robot_response_timeout', '')]
    assert robot.tick(17) == []
    assert robot.observe_status('fetch', 'coke', True, 18) == []
    robot.observe_state('IDLE', 19)
    assert robot.fetch_drop_reason(20) == 'robot_response_timeout'
    robot.observe_state('ERROR', 21)
    robot.observe_state('RETURN_HOME', 22)
    robot.observe_state('IDLE', 23)
    assert robot.fetch_drop_reason(24) == ''


def test_stop_replaces_pending_fetch_and_requires_error_recovery_before_fetch():
    robot = idle_robot()
    robot.mark_sent('fetch', 'coke', 11)
    robot.mark_sent('stop', '', 12)
    assert robot.observe_status('fetch', 'coke', True, 13) == []
    assert robot.observe_status('stop', '', True, 14) == [('robot_stop_accepted', '')]
    assert robot.fetch_drop_reason(15) == 'robot_pending'
    assert robot.observe_state('ERROR', 16) == []
    assert robot.fetch_drop_reason(17) == 'robot_error'
    robot.observe_state('RETURN_HOME', 18)
    robot.observe_state('IDLE', 19)
    assert robot.fetch_drop_reason(20) == ''


def test_error_is_generic_and_carrying_does_not_invent_target_after_restart():
    robot = RobotFeedback()
    assert robot.observe_state('DRIVE_TO_PLACE', 1) == []
    assert robot.observe_state('ERROR', 2) == [('robot_error', '')]
    assert robot.observe_state('ERROR', 3) == []


def test_disconnect_forgets_state_target_and_pending_reply():
    robot = idle_robot()
    robot.mark_sent('fetch', 'coke', 11)
    robot.reset()
    assert robot.fetch_drop_reason(12) == 'robot_unknown'
    assert robot.observe_status('fetch', 'coke', True, 13) == []
    robot.observe_state('IDLE', 14)
    assert robot.fetch_drop_reason(13) == 'robot_before_idle'


def test_uncertain_send_remains_blocked():
    robot = idle_robot()
    robot.mark_sent('fetch', 'coke', 11)
    robot.mark_send_uncertain()
    assert robot.fetch_drop_reason(12) == 'robot_response_timeout'


@pytest.mark.parametrize('state', ['ERROR', 'RETURN_HOME'])
def test_delayed_acceptance_does_not_promise_work_after_failure_or_return(state):
    robot = idle_robot()
    robot.mark_sent('fetch', 'coke', 11)
    robot.observe_state(state, 12)
    assert robot.observe_status('fetch', 'coke', True, 13) == []
    assert robot.active_target == ''


@pytest.mark.parametrize('value', [0, -1, float('inf'), float('nan')])
def test_invalid_response_timeout_is_rejected(value):
    with pytest.raises(ValueError):
        RobotFeedback(value)


def test_templates_render_and_bad_placeholders_are_rejected():
    validate_templates(DEFAULT_MESSAGES)
    assert render_feedback(DEFAULT_MESSAGES, ('robot_accepted', 'tissue')) == '휴지 가져올게요.'
    for template in ('{target}', '{object.name}', '{object!r}', '{object:10}', '{', ''):
        messages = {**DEFAULT_MESSAGES, 'robot_accepted': template}
        with pytest.raises(ValueError):
            validate_templates(messages)
