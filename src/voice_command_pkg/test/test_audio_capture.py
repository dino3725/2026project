import pytest

from voice_command_pkg.audio_capture import (
    FRAME_BYTES,
    qualify_speech_for_state,
    VadConfig,
    VadState,
    VadStateMachine,
)


def feed(detector, decisions):
    """Feed per-frame decisions and return every transition produced."""
    events = []
    for is_speech in decisions:
        transition = detector.process_frame(is_speech)
        if transition is not None:
            events.append((detector.frame_index, transition[0], transition[1]))
    return events


def test_default_config_matches_baseline_frame_counts():
    config = VadConfig()
    assert config.stream_warmup_frames == 40
    assert config.start_window_frames == 10
    assert config.start_required_speech_frames == 8
    assert config.end_silence_frames == 30
    assert config.pre_roll_frames == 15
    assert config.max_utterance_frames == 500


def test_end_silence_rounds_up_to_whole_frames():
    assert VadConfig(end_silence_ms=500).end_silence_frames == 17
    assert VadConfig(end_silence_ms=600).end_silence_frames == 20


def test_start_needs_ratio_of_speech_in_window():
    detector = VadStateMachine(VadConfig())
    # 7 of 10 frames are speech: not enough to start.
    assert feed(detector, [True] * 7 + [False] * 3) == []
    assert detector.state is VadState.LISTENING


def test_start_then_end_after_configured_silence():
    detector = VadStateMachine(VadConfig(end_silence_ms=300))
    events = feed(detector, [True] * 10 + [True] * 5 + [False] * 10)
    assert [e[1] for e in events] == ['start', 'end']
    start_frame, _, start = events[0]
    end_frame, _, end = events[1]
    assert start_frame == 10 and start['speech_frames'] == 10
    assert end_frame == 25 and end['silence_frames'] == 10
    assert end['silence_seconds'] == pytest.approx(0.3)
    assert detector.state is VadState.LISTENING


def test_speech_frame_resets_silence_counter():
    detector = VadStateMachine(VadConfig(end_silence_ms=300))
    feed(detector, [True] * 10)
    assert feed(detector, [False] * 9 + [True] + [False] * 9) == []
    assert detector.state is VadState.SPEAKING


def test_noise_gate_applies_only_while_listening():
    silent = b'\x00' * FRAME_BYTES
    assert qualify_speech_for_state(VadState.LISTENING, True, silent, 200) is False
    assert qualify_speech_for_state(VadState.SPEAKING, True, silent, 200) is True


@pytest.mark.parametrize('kwargs', [
    {'vad_mode': 4},
    {'start_speech_ratio': 0.0},
    {'end_silence_ms': 10},
    {'start_window_ms': 0},
    {'max_utterance_seconds': 0.0},
    {'pre_roll_ms': -1},
])
def test_invalid_config_is_rejected(kwargs):
    with pytest.raises(ValueError):
        VadConfig(**kwargs)
