"""Unit tests for VoiceCommandNode.publish_command without a running ROS graph."""

from types import SimpleNamespace

import pytest

pytest.importorskip('rclpy')
pytest.importorskip('eated_interfaces')

from voice_command_pkg.voice_command_node import VoiceCommandNode  # noqa: E402


class FakeStamp:

    def to_msg(self):
        from builtin_interfaces.msg import Time
        return Time(sec=1, nanosec=2)


def make_node(publish_enabled=True):
    published = []
    logs = []
    node = SimpleNamespace(
        publish_enabled=publish_enabled,
        publisher=SimpleNamespace(publish=published.append),
        get_clock=lambda: SimpleNamespace(now=FakeStamp),
        get_logger=lambda: SimpleNamespace(info=logs.append),
    )
    return node, published, logs


def publish(node, command, transcript='문장'):
    return VoiceCommandNode.publish_command(node, command, transcript)


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
