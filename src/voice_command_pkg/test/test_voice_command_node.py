"""Unit tests for VoiceCommandNode.publish_command without a running ROS graph."""

from types import MethodType, SimpleNamespace
import threading
import time

import pytest

pytest.importorskip('rclpy')
pytest.importorskip('eated_interfaces')

from voice_command_pkg.voice_command_node import VoiceCommandNode  # noqa: E402
from voice_command_pkg.robot_feedback import DEFAULT_MESSAGES, RobotFeedback  # noqa: E402


class FakeStamp:

    def to_msg(self):
        from builtin_interfaces.msg import Time
        return Time(sec=1, nanosec=2)


def make_node(publish_enabled=True):
    published = []
    logs = []
    node = SimpleNamespace(
        publish_enabled=publish_enabled,
        publish_lock=threading.RLock(),
        stop_event=threading.Event(),
        _closing=False,
        feedback=RobotFeedback(),
        feedback_messages=DEFAULT_MESSAGES.copy(),
        pipeline_config=SimpleNamespace(max_fetch_age_seconds=10),
        _refresh_robot_connection=lambda: None,
        publisher=SimpleNamespace(publish=published.append),
        get_clock=lambda: SimpleNamespace(now=FakeStamp),
        get_logger=lambda: SimpleNamespace(info=logs.append, error=logs.append),
    )
    for name in ('_notice', '_feedback_tick', 'check_fetch_allowed', '_apply_state'):
        setattr(node, name, MethodType(getattr(VoiceCommandNode, name), node))
    node.feedback.observe_state('IDLE', time.perf_counter() - 1)
    return node, published, logs


def publish(node, command, transcript='문장'):
    now = time.perf_counter()
    return VoiceCommandNode.publish_command(node, command, transcript, {
        'voice_start_wall': now, 'utterance_start_wall': now, 'voice_end_wall': now,
    })


def test_fetch_is_published_with_transcript_and_stamp():
    node, published, _ = make_node()
    assert publish(node, {'action': 'fetch', 'object': 'coke'}, '목말라') is True
    (message,) = published
    assert (message.action, message.target_class, message.transcript) == ('fetch', 'coke', '목말라')
    assert (message.header.stamp.sec, message.header.stamp.nanosec) == (1, 2)


def test_stop_is_published():
    node, published, _ = make_node()
    assert publish(node, {'action': 'stop', 'object': ''}) is True
    assert published[0].action == 'stop'
    assert published[0].target_class == ''


def test_unknown_is_logged_and_not_published():
    node, published, logs = make_node()
    assert publish(node, {'action': 'unknown', 'object': ''}) is False
    assert published == []
    assert len(logs) == 1


def test_invalid_command_is_not_published():
    node, published, _ = make_node()
    assert publish(node, {'action': 'fetch', 'object': 'snack'}) is False
    assert published == []


def test_dry_run_never_publishes():
    node, published, _ = make_node(publish_enabled=False)
    assert publish(node, {'action': 'stop', 'object': ''}) is False
    assert published == []


@pytest.mark.parametrize('action, target', [('fetch', 'coke'), ('stop', '')])
def test_shutdown_never_publishes(action, target):
    node, published, _ = make_node()
    node.stop_event.set()
    assert publish(node, {'action': action, 'object': target}) is False
    assert published == []


def test_shutdown_during_message_preparation_blocks_publish():
    node, published, _ = make_node()

    def get_clock():
        node.stop_event.set()
        return SimpleNamespace(now=FakeStamp)

    node.get_clock = get_clock
    assert publish(node, {'action': 'fetch', 'object': 'coke'}) is False
    assert published == []


@pytest.mark.parametrize('state', [None, 'PICKING', 'RETURN_HOME', 'ERROR'])
def test_fetch_blocked_but_stop_published_in_unavailable_states(state):
    node, published, logs = make_node()
    node.feedback.observe_state(state, time.perf_counter())
    assert publish(node, {'action': 'fetch', 'object': 'coke'}) is False
    assert published == []
    assert publish(node, {'action': 'stop', 'object': ''}) is True
    assert [message.action for message in published] == ['stop']
    assert any('robot_stop_sent' in line for line in logs)
    assert not any('robot_stop_accepted' in line for line in logs)


def test_stop_does_not_call_graph_or_fetch_gate():
    node, published, _ = make_node()

    def forbidden(*args):
        pytest.fail('stop must not depend on graph availability')

    node.check_fetch_allowed = forbidden
    node._refresh_robot_connection = forbidden
    assert publish(node, {'action': 'stop', 'object': ''})
    assert len(published) == 1


def test_second_fetch_waits_and_publication_does_not_claim_acceptance():
    node, published, logs = make_node()
    assert publish(node, {'action': 'fetch', 'object': 'coke'})
    assert not publish(node, {'action': 'fetch', 'object': 'tissue'})
    assert len(published) == 1
    assert not any('robot_accepted' in line for line in logs)


def test_dry_run_fetch_creates_no_pending_request():
    node, published, logs = make_node(publish_enabled=False)
    assert not publish(node, {'action': 'fetch', 'object': 'coke'})
    assert node.feedback.pending is None
    assert published == []
    assert any('robot_dry_run' in line for line in logs)


def test_publication_error_preserves_block_and_still_allows_stop():
    node, published, logs = make_node()

    def failed(message):
        assert node.feedback.pending is not None, 'reserve before attempting publication'
        raise RuntimeError('transport failure')

    node.publisher.publish = failed
    assert not publish(node, {'action': 'fetch', 'object': 'coke'})
    assert node.feedback.pending.timed_out
    assert any('robot_send_error' in line for line in logs)
    node.publisher.publish = published.append
    assert publish(node, {'action': 'stop', 'object': ''})


def test_final_gate_observes_new_busy_state():
    node, published, _ = make_node()

    def refresh():
        node.feedback.observe_state('PICKING', time.perf_counter())

    node._refresh_robot_connection = refresh
    assert not publish(node, {'action': 'fetch', 'object': 'coke'})
    assert published == []


def test_fetch_age_is_rechecked_after_gate_before_publication():
    node, published, logs = make_node()
    node.pipeline_config.max_fetch_age_seconds = 0.01

    def slow_refresh():
        time.sleep(0.03)

    node._refresh_robot_connection = slow_refresh
    assert not publish(node, {'action': 'fetch', 'object': 'coke'})
    assert published == []
    assert any('robot_expired' in line for line in logs)


@pytest.mark.parametrize('capture', [None, {}, {'voice_start_wall': float('nan')}])
def test_missing_or_invalid_capture_cannot_publish_fetch(capture):
    node, published, _ = make_node()
    assert not VoiceCommandNode.publish_command(
        node, {'action': 'fetch', 'object': 'coke'}, '콜라', capture
    )
    assert published == []


def test_graph_loss_and_replacement_forget_idle_and_pending():
    node, _, _ = make_node()
    node._controller_source = None
    node._replace_feedback_subscriptions = lambda: None
    node._graph_problem = ''
    node.state_subscription = SimpleNamespace(topic_name='/control/state')
    endpoint = SimpleNamespace(
        endpoint_gid=[1], node_name='control_node', node_namespace='/',
        topic_type='std_msgs/msg/String',
    )
    endpoints = [endpoint]
    node.get_publishers_info_by_topic = lambda topic: endpoints

    def refresh():
        VoiceCommandNode._refresh_robot_connection(node)

    refresh()
    assert node.feedback.state is None
    node.feedback.observe_state('IDLE', time.perf_counter())
    node.feedback.mark_sent('fetch', 'coke', time.perf_counter())
    endpoints.clear()
    refresh()
    assert node.feedback.state is None
    assert node.feedback.pending is None
    endpoints.append(endpoint)
    refresh()
    assert node.feedback.state is None
    node.feedback.observe_state('IDLE', time.perf_counter())
    endpoint.endpoint_gid = [2]
    refresh()
    assert node.feedback.state is None


def test_callbacks_after_shutdown_never_mutate_or_log():
    node, _, logs = make_node()
    node._closing = True
    VoiceCommandNode._on_robot_state(node, SimpleNamespace(data='ERROR'), None)
    VoiceCommandNode._on_command_status(node, object(), None)
    assert node.feedback.state == 'IDLE'
    assert logs == []


def test_humble_single_argument_callbacks_and_old_generation_discard():
    node, _, _ = make_node()
    node._controller_source = (b'controller', 'control_node', '/')
    node._subscription_generation = 0
    node._state_qos = object()
    node.state_subscription = None
    node.status_subscription = None
    destroyed = []
    node.destroy_subscription = destroyed.append
    node.create_subscription = lambda cls, topic, callback, qos: SimpleNamespace(callback=callback)
    node._on_robot_state = MethodType(VoiceCommandNode._on_robot_state, node)
    node._on_command_status = MethodType(VoiceCommandNode._on_command_status, node)
    VoiceCommandNode._replace_feedback_subscriptions(node)
    old_callback = node.state_subscription.callback
    old_callback(SimpleNamespace(data='IDLE'))  # Humble passes only the message.
    VoiceCommandNode._replace_feedback_subscriptions(node)
    assert len(destroyed) == 2
    old_callback(SimpleNamespace(data='ERROR'))
    assert node.feedback.state == 'IDLE'
    node.state_subscription.callback(SimpleNamespace(data='PICKING'))
    assert node.feedback.state == 'PICKING'
