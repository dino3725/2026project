import pytest

from voice_command_pkg.command_protocol import command_payload, TOPIC_NAME, VALID_OBJECTS


def test_topic_and_objects_match_robot_contract():
    assert TOPIC_NAME == '/voice/robot_command'
    assert VALID_OBJECTS == {'airpod', 'coke', 'tissue', 'vaseline'}


@pytest.mark.parametrize('obj', sorted(VALID_OBJECTS))
def test_fetch_with_supported_object_passes(obj):
    assert command_payload({'action': 'fetch', 'object': obj}) == ('fetch', obj)


def test_stop_passes_with_empty_object():
    assert command_payload({'action': 'stop', 'object': ''}) == ('stop', '')


@pytest.mark.parametrize('command', [
    {'action': 'unknown', 'object': ''},
    {'action': 'fetch', 'object': ''},
    {'action': 'fetch', 'object': 'snack'},
    {'action': 'stop', 'object': 'coke'},
    {'action': 'fetch', 'object': 'coke', 'extra': 1},
    {'action': 'fetch'},
    None,
    'stop',
])
def test_everything_else_is_blocked(command):
    assert command_payload(command) is None
