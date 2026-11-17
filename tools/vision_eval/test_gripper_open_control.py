"""Verify one-shot opening and cancellation without any actuators."""

import json
import threading
from types import SimpleNamespace

from control_pkg.control_node import OperationCancelled
from gripper_open_control import GripperOpenControl


def fake_node(tmp_path):
    return SimpleNamespace(
        _lock=threading.RLock(), _stop_event=threading.Event(), used=False,
        _preparing=False, result_path=tmp_path/'result.json', gripper_open=.019,
        blocked_reason=lambda: '', open_sequence=lambda operation: None)


def test_open_once_does_not_repeat(tmp_path):
    n=fake_node(tmp_path); calls=[]; n._start_operation=lambda sequence:calls.append(sequence)
    response=GripperOpenControl.open_once(n,None,SimpleNamespace())
    assert response.success and n.used and n._preparing
    assert len(calls)==1
    assert not GripperOpenControl.open_once(n,None,SimpleNamespace()).success
    assert len(calls)==1


def test_stop_or_missing_heartbeat_prevents_open(tmp_path):
    n=fake_node(tmp_path);n._stop_event.set()
    assert not GripperOpenControl.open_once(n,None,SimpleNamespace()).success
    assert not n.result_path.exists()
    n._stop_event.clear();n.blocked_reason=lambda:'No fresh joystick heartbeat'
    assert not GripperOpenControl.open_once(n,None,SimpleNamespace()).success


def test_sequence_only_opens_and_uses_cancelable_wait(tmp_path):
    n=fake_node(tmp_path);calls=[]
    n._send_gripper=lambda *args:calls.append(args)
    n._sleep_cancelable=lambda *args:calls.append(args)
    GripperOpenControl.open_sequence(n,7)
    assert calls==[(.019,7,'MODEL TRIAL OPEN'),(.5,7)]
    assert json.loads(n.result_path.read_text())['completed']


def test_cancellation_never_marks_completed(tmp_path):
    n=fake_node(tmp_path)
    def cancel(*args):raise OperationCancelled()
    n._send_gripper=cancel
    GripperOpenControl.open_sequence(n,7)
    result=json.loads(n.result_path.read_text())
    assert not result['completed'] and result['error']=='Cancelled'


def test_prepare_never_moves_arm(tmp_path):
    assert not GripperOpenControl.prepare(fake_node(tmp_path),None,SimpleNamespace()).success
