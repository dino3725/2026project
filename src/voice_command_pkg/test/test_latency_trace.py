"""Validate timing provenance without introducing dependencies into stop dispatch."""

import json
import time

from test_voice_command_node import make_node, publish
from voice_command_pkg.latency_trace import log_publication


def test_actual_and_dry_run_traces_are_distinct():
    for enabled in (True, False):
        node, sent, logs = make_node(enabled)
        publish(node, {'action': 'stop', 'object': ''})
        row = json.loads(next(line.split('[LATENCY] ')[1] for line in logs
                              if line.startswith('[LATENCY] ')))
        assert row['event'] == ('published' if enabled else 'dry_run')
        assert row['command_stamp_ns'] == (1000000002 if enabled else None)
        assert row['vad_end_to_publish_call_seconds'] >= 0
        assert row['publish_call_seconds'] is not None if enabled else (
            row['publish_call_seconds'] is None)
        assert bool(sent) == enabled


def test_measurement_logging_failure_does_not_change_success():
    node, sent, _ = make_node()

    def log(line):
        if line.startswith('[LATENCY]'):
            raise OSError('logging unavailable')

    node.get_logger().info = log
    # Use a stable logger since the fixture normally creates one for each call.
    from types import SimpleNamespace
    node.get_logger = lambda: SimpleNamespace(info=log, error=log)
    assert publish(node, {'action': 'stop', 'object': ''})
    assert len(sent) == 1


def test_invalid_capture_timing_never_changes_command_dispatch():
    node, sent, logs = make_node()
    assert publish(node, {'action': 'stop', 'object': ''})
    log_publication(logs.append, sent[0], {'voice_end_wall': float('nan')},
                    time.perf_counter(), time.perf_counter(), True)
    row = json.loads(logs[-1].split('[LATENCY] ')[1])
    assert 'vad_end_to_publish_call_seconds' not in row


def test_speech_frame_timestamp_is_not_end_of_silence(monkeypatch):
    import threading
    from test_continuous_capture import install_capture_pipe
    from voice_command_pkg import pipeline
    from voice_command_pkg.audio_capture import FRAME_BYTES, VadConfig
    speech = b'\x01\x00' * (FRAME_BYTES // 2)
    silence = bytes(FRAME_BYTES)
    install_capture_pipe(monkeypatch, [speech, silence, silence])
    taps = []
    config = VadConfig(stream_warmup_ms=0, start_window_ms=30, start_speech_ratio=1.0,
                       end_silence_ms=60, pre_roll_ms=30, start_min_rms=0)
    stream = pipeline.capture_utterances(config, lambda text: None, threading.Event(),
                                         frame_sink=lambda pcm, pos, stamp: taps.append(stamp))
    try:
        _, capture = next(stream)
        assert capture['last_speech_frame_read_wall'] == taps[0]
        assert capture['last_speech_frame_read_wall'] < capture['voice_end_wall']
        assert capture['vad_end_silence_seconds'] == .06
    finally:
        stream.close()
