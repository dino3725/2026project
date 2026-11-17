"""Exercise bringup failure paths without starting any robot processes."""

import os
import signal
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

import eated_auto as auto


def test_audit_is_opt_in_and_does_not_change_poses(tmp_path):
    """Audit mode records legacy inference and disables only Pick."""
    import yaml
    normal = dict(auto.stages())
    audit = dict(auto.stages(tmp_path))
    assert 'localization_mode:=legacy' in audit['vision']
    assert not any('observation_output' in arg for arg in normal['vision'])
    assert audit['hardware'] == normal['hardware']
    auto.write_audit_config(tmp_path)
    expected = yaml.safe_load(
        (auto.WORKSPACE / 'src/control_pkg/config/control.yaml').read_text())
    actual = yaml.safe_load((tmp_path / 'control_audit.yaml').read_text())
    expected['control_node']['ros__parameters']['pick_enabled'] = False
    assert actual == expected


def test_shutdown_publishes_one_stop_even_with_busy_callbacks(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(auto.time, 'monotonic', lambda: clock[0])
    messages = []

    def spin():
        clock[0] += 0.001

    observer = SimpleNamespace(
        command_type=lambda: SimpleNamespace(header=SimpleNamespace()),
        node=SimpleNamespace(get_clock=lambda: SimpleNamespace(
            now=lambda: SimpleNamespace(to_msg=lambda: 'stamp'))),
        stop_publisher=SimpleNamespace(publish=messages.append), spin=spin)
    auto.Readiness.stop_control(observer)
    assert len(messages) == 1
    assert messages[0].action == 'stop'
    assert clock[0] >= 2.0


def test_neutral_joy_requires_live_buttons_and_axes():
    observer = auto.Readiness.__new__(auto.Readiness)
    observer.received = {}
    assert not observer.ready('joy')
    message = SimpleNamespace(buttons=[0, 0, 0, 0], axes=[0.0, 0.0])
    observer.receive('joy', message)
    assert observer.ready('joy')
    message.buttons[3] = 1
    assert not observer.ready('joy')
    message.buttons[3] = 0
    message.axes[0] = 0.5
    assert not observer.ready('joy')
    message.axes[0] = 0.0
    observer.received['joy'] = (time.monotonic() - 2, message)
    assert not observer.ready('joy')


def test_error_does_not_count_as_control_ready():
    observer = auto.Readiness.__new__(auto.Readiness)
    observer.state = 'RETURN_HOME'
    assert not observer.ready('control')
    observer.state = 'ERROR'
    with pytest.raises(RuntimeError, match='ERROR'):
        observer.ready('control')
    observer.state = 'IDLE'
    assert observer.ready('control')


def test_wait_times_out_and_stops_on_dead_process():
    observer = SimpleNamespace(
        spin=lambda: None, fresh=lambda key: True, ready=lambda name: False)
    processes = SimpleNamespace(check=lambda: None)
    with pytest.raises(RuntimeError, match='준비되지'):
        auto.wait_ready('hardware', observer, processes, 0.01)

    def failed():
        raise RuntimeError('child failed')

    processes.check = failed
    with pytest.raises(RuntimeError, match='child failed'):
        auto.wait_ready('hardware', observer, processes, 1)


def test_joy_disconnect_prevents_next_startup_stage():
    observer = SimpleNamespace(
        spin=lambda: None, fresh=lambda key: False, ready=lambda name: True)
    with pytest.raises(RuntimeError, match='조이스틱'):
        auto.wait_ready('control', observer,
                        SimpleNamespace(check=lambda: None), 1)


def test_cleanup_continues_after_one_failure(tmp_path):
    processes = auto.Processes(tmp_path)
    order = []

    def stop(name):
        order.append(name)
        if name == 'vision':
            raise RuntimeError('example failure')

    processes.stop = stop
    errors = processes.close()
    assert order == ['joystick', 'control', 'vision', 'camera',
                     'moveit', 'hardware', 'joy']
    assert len(errors) == 1


def test_headless_environment_and_manual_drive_order(monkeypatch):
    monkeypatch.setenv('DISPLAY', ':0')
    monkeypatch.setenv('WAYLAND_DISPLAY', 'wayland-0')
    monkeypatch.setenv('PYTHONPATH', '/existing')
    env = auto.child_environment('vision')
    assert 'DISPLAY' not in env and 'WAYLAND_DISPLAY' not in env
    assert env['PYTHONPATH'].endswith(':/existing')
    names = [name for name, _ in auto.stages()]
    assert names.index('joy') < names.index('hardware')
    assert names.index('control') < names.index('joystick')


def test_dry_run_does_not_need_ros_or_create_logs(tmp_path, monkeypatch):
    monkeypatch.setattr(auto, 'WORKSPACE', tmp_path)
    monkeypatch.setattr(sys, 'argv', ['eateded', '--dry-run'])
    assert auto.main() == 0
    assert not (tmp_path / 'log').exists()


def test_child_exit_is_failure_even_when_exit_code_is_zero(tmp_path):
    processes = auto.Processes(tmp_path)
    processes.start('vision', [sys.executable, '-c', 'pass'])
    processes.children['vision'].wait(timeout=3)
    with pytest.raises(RuntimeError, match='code=0'):
        processes.check()
    assert processes.close() == []


def test_shutdown_signals_descendants_and_keeps_other_groups(tmp_path):
    child_marker = tmp_path / 'child_stopped'
    child_ready = tmp_path / 'child_ready'
    child_code = (
        'import signal, time\nfrom pathlib import Path\n'
        'def stop(*args):\n'
        f'    Path({str(child_marker)!r}).touch()\n'
        '    raise SystemExit(0)\n'
        'signal.signal(signal.SIGINT, stop)\n'
        f'Path({str(child_ready)!r}).touch()\n'
        'while True: time.sleep(0.05)\n'
    )
    parent_code = (
        'import signal, subprocess, sys\n'
        'signal.signal(signal.SIGINT, lambda *args: None)\n'
        f'child = subprocess.Popen([sys.executable, "-c", {child_code!r}])\n'
        'child.wait()\n'
    )
    unrelated = subprocess.Popen(
        [sys.executable, '-c', 'import time; time.sleep(30)'],
        start_new_session=True,
    )
    processes = auto.Processes(tmp_path)
    try:
        processes.start('hardware', [sys.executable, '-c', parent_code])
        deadline = time.monotonic() + 3
        while not child_ready.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert child_ready.exists()
        assert processes.close() == []
        assert child_marker.exists()
        assert unrelated.poll() is None
    finally:
        processes.close()
        os.killpg(unrelated.pid, signal.SIGTERM)
        unrelated.wait(timeout=3)


def test_reviewed_approach_uses_one_guarded_controller_and_no_start_trigger(tmp_path):
    import hashlib
    plan = tmp_path / 'plan.json'
    plan.write_text('{}')
    commands = dict(auto.stages(reviewed_approach_plan=plan))
    control = commands['control']
    assert any(x.endswith('high_approach_control.py') for x in control)
    assert f'reviewed_plan_path:={plan}' in control
    assert f'reviewed_plan_sha256:={hashlib.sha256(b"{}").hexdigest()}' in control
    assert not any('Trigger' in ' '.join(command) or 'service call' in ' '.join(command)
                   for command in commands.values())
    assert list(commands).count('control') == 1
