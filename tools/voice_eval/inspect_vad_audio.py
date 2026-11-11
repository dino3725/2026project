#!/usr/bin/env python3
"""Replay explicit PCM16 WAVs through the current VAD; never open a microphone."""

import argparse
import audioop
from collections import deque
from dataclasses import asdict
import hashlib
import json
from pathlib import Path

from compare_stt_inputs import read_pcm
import webrtcvad

from voice_command_pkg.audio_capture import (
    FRAME_BYTES, FRAME_MS, qualify_speech_for_state, SAMPLE_RATE,
    VadConfig, VadStateMachine,
)


def inspect(pcm, config):
    """Keep frame evidence, including frames rejected before utterance start."""
    vad = webrtcvad.Vad(config.vad_mode)
    detector = VadStateMachine(config)
    window = deque(maxlen=config.start_window_frames)
    events, frames = [], []
    peak_qualified_window = 0
    for offset in range(0, len(pcm) - FRAME_BYTES + 1, FRAME_BYTES):
        frame = pcm[offset:offset + FRAME_BYTES]
        speech = vad.is_speech(frame, SAMPLE_RATE)
        rms = audioop.rms(frame, 2)
        window.append(speech and rms >= config.start_min_rms)
        peak_qualified_window = max(peak_qualified_window, sum(window))
        qualified = qualify_speech_for_state(detector.state, speech, frame, config.start_min_rms)
        state = detector.state.name
        event = detector.process_frame(qualified)
        frames.append({'time': offset / 32000, 'rms': rms, 'vad_speech': speech,
                       'qualified': qualified, 'state_before': state})
        if event:
            events.append({'event': event[0], **event[1]})
    return {'events': events, 'frames': frames,
            'max_start_qualified_frames': peak_qualified_window,
            'trailing_bytes_ignored': len(pcm) % FRAME_BYTES,
            'frame_ms': FRAME_MS, 'config': asdict(config)}


def main():
    """Write per-frame diagnostics without retuning runtime thresholds."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('files', type=Path, nargs='+')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error('Choose a fresh --output path')
    results = []
    for path in args.files:
        pcm = read_pcm(path)
        result = {'file': str(path), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
                  'duration_seconds': len(pcm) / 32000, **inspect(pcm, VadConfig())}
        results.append(result)
        print(json.dumps({k: v for k, v in result.items() if k != 'frames'}, ensure_ascii=False))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open('x', encoding='utf-8') as handle:
        json.dump({'note': ('Fresh VAD per WAV; no warmup discarded. Cropped utterances cannot '
                            'reproduce stream history or reveal missing attempts.'),
                   'results': results}, handle, ensure_ascii=False, indent=2)
        handle.write('\n')


if __name__ == '__main__':
    main()
