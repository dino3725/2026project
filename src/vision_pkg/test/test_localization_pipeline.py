"""Check candidate integration without loading a model or ROS hardware node."""

import importlib
import sys
import threading
from types import SimpleNamespace

import numpy as np
import pytest
from rclpy.clock import ClockType
from rclpy.time import Time

from vision_pkg.vision_utils import Detection


@pytest.fixture
def module(monkeypatch):
    """Replace only the model dependency; use real ROS message definitions."""
    monkeypatch.setitem(sys.modules, 'ultralytics', SimpleNamespace(YOLO=object))
    module = importlib.import_module('vision_pkg.vision_node')
    yield module
    sys.modules.pop('vision_pkg.vision_node', None)


def fake_node(module, mode, depth):
    """Build a frame-processing context with a translation-only test TF."""
    rgb = SimpleNamespace(header=SimpleNamespace(frame_id='optical'))
    depth_msg = SimpleNamespace(encoding='16UC1')
    bridge = SimpleNamespace(imgmsg_to_cv2=lambda msg, desired_encoding:
                             depth if msg is depth_msg else np.zeros((31, 31, 3)))
    node = SimpleNamespace(
        bridge=bridge, localization_mode=mode, depth_scale=.001,
        depth_window_size=7, camera_frame='optical',
        _intrinsics_lock=threading.Lock(),
        _camera_intrinsics=module.CameraIntrinsics(100., 100., 15., 15.),
        find_target_detection=lambda *_: Detection('coke', .9, 1, 1, 29, 29),
        transform_point_to_base=lambda p, _: (p[0] + .1, p[1], p[2]),
    )
    return node, rgb, depth_msg


def test_candidate_propagates_actual_surface_ray_through_tf(module):
    """An offset valid patch changes X; legacy keeps the bounding-box ray."""
    depth = np.zeros((31, 31), dtype=np.uint16)
    depth[8:23, 8:12] = 300
    node, rgb, message = fake_node(module, 'candidate', depth)
    result = module.VisionNode._process_frame(node, rgb, message, 'coke')
    assert result.base_point[0] < .1
    assert 'source=inner' in result.surface_quality
    node.localization_mode = 'legacy'
    old = module.VisionNode._process_frame(node, rgb, message, 'coke')
    assert old.base_point == pytest.approx((.1, 0., .3))


def test_rejected_candidate_never_falls_back_silently_to_legacy(module):
    """Depth ambiguity must remain a failed observation in candidate mode."""
    depth = np.full((31, 31), 800, dtype=np.uint16)
    depth[:, :15] = 300
    node, rgb, message = fake_node(module, 'candidate', depth)
    node.transform_point_to_base = lambda *_: pytest.fail('TF should not be called')
    result = module.VisionNode._process_frame(node, rgb, message, 'coke')
    assert result.base_point is None
    assert result.error == 'ambiguous depth groups'


def test_unavailable_intrinsics_returns_failure(module):
    """No guessed focal lengths may be used when CameraInfo is missing."""
    node, rgb, message = fake_node(
        module, 'candidate', np.full((31, 31), 300, dtype=np.uint16))
    node._camera_intrinsics = None
    result = module.VisionNode._process_frame(node, rgb, message, 'coke')
    assert result.base_point is None
    assert result.error == 'CameraInfo unavailable'


def run_recognition(module, monkeypatch, detections, duplicate_stamps=False, latency=0.):
    """Exercise the action loop with a deterministic clock and synthetic images."""
    clock = SimpleNamespace(now=100., index=0)
    monkeypatch.setattr(module, 'time', SimpleNamespace(
        monotonic=lambda: clock.now,
        sleep=lambda duration: setattr(clock, 'now', clock.now + duration)))
    monkeypatch.setattr(module.rclpy, 'ok', lambda: True)
    outcome = SimpleNamespace(succeeded=False, aborted=False, finished_at=None)
    goal = SimpleNamespace(
        is_cancel_requested=False,
        succeed=lambda: setattr(outcome, 'succeeded', True),
        abort=lambda: setattr(outcome, 'aborted', True))

    def wait(*args, **kwargs):
        clock.now += .1
        if clock.index >= len(detections):
            return None
        clock.index += 1
        stamp = 100.1 if duplicate_stamps else clock.now
        msg = SimpleNamespace(header=SimpleNamespace(
            stamp=Time(nanoseconds=round(stamp * 1e9)).to_msg()))
        return clock.index, msg, msg

    def process(*args):
        clock.now += latency
        detected = detections[clock.index - 1]
        return SimpleNamespace(
            detection=SimpleNamespace(confidence=.9) if detected else None,
            base_point=(.1, 0., .3) if detected else None,
            error='' if detected else 'target not detected', surface_quality='')

    def success(values):
        outcome.finished_at = clock.now
        return values

    def noop(*args, **kwargs):
        return None

    node = SimpleNamespace(
        localization_mode='candidate', stable_detection_duration=.3,
        localization_max_gap_sec=.5, localization_max_spread_m=.01,
        min_valid_samples=3, max_inference_rate_hz=100., recognition_timeout=2.,
        max_frame_age=.5, get_clock=lambda: SimpleNamespace(
            now=lambda: Time(nanoseconds=round(clock.now * 1e9),
                             clock_type=ClockType.ROS_TIME)),
        _wait_for_new_frame=wait, _process_frame=process,
        _record_observation=noop, _publish_feedback=noop, _log_frame_error=noop,
        _log_observation=noop, _log_final_point=noop, publish_debug_image=noop,
        _camera_stream_is_stale=lambda now: True,
        _success_result=success, _failure_result=lambda reason: reason)
    node._validate_frame_age = lambda msg: module.VisionNode._validate_frame_age(node, msg)
    result = module.VisionNode._run_recognition(node, goal, 'coke')
    return outcome, result


def test_action_restarts_stability_after_brief_detection_loss(module, monkeypatch):
    """Old pre-loss samples cannot complete the next detection immediately."""
    outcome, result = run_recognition(
        module, monkeypatch, [True, True, False] + [True] * 8)
    assert outcome.succeeded
    assert outcome.finished_at >= 100.7
    assert result == pytest.approx((.1, 0., .3, .9))


def test_action_duplicate_images_do_not_accumulate_evidence(module, monkeypatch):
    """New callback sequence numbers cannot make repeated images independent."""
    outcome, _ = run_recognition(module, monkeypatch, [True] * 12, duplicate_stamps=True)
    assert not outcome.succeeded
    assert outcome.aborted


def test_action_rejects_images_that_age_during_inference(module, monkeypatch):
    """A fresh input can be too old when the result becomes available."""
    outcome, _ = run_recognition(module, monkeypatch, [True] * 12, latency=.6)
    assert not outcome.succeeded
    assert outcome.aborted


def test_candidate_transforms_at_depth_time(module):
    """An aligned depth point uses the depth acquisition pose, not RGB time."""
    node, rgb, depth = fake_node(module, 'candidate', np.full((31, 31), 300, np.uint16))
    used = []
    node.transform_point_to_base = lambda p, msg: used.append(msg) or p
    module.VisionNode._process_frame(node, rgb, depth, 'coke')
    assert used == [depth]
