#!/usr/bin/env python3
"""Replay completed development utterances with real KWS/STT/speaker, without ROS."""

import argparse
import hashlib
import json
from pathlib import Path
import threading
import time
from types import SimpleNamespace
from unittest.mock import patch

from evaluate_audio_stop import read_pcm
from voice_command_pkg import pipeline
from voice_command_pkg.audio_stop_model import AudioStopConfig
from voice_command_pkg.speaker_profile import SpeakerConfig
from voice_command_pkg.utterance_buffer import Utterance


def run(samples, profile, phases, gain=1.0):
    """Exercise real worker deadlines and local callbacks; LLM/IDLE are simulated."""
    logs, commands, rows = [], [], []
    ready = threading.Event()

    def log(message):
        logs.append(message)
        if '[SPEAKER READY]' in message or '[SPEAKER ERROR]' in message:
            ready.set()

    def publish(command, text, capture):
        commands.append({'action': command['action'], 'text': text,
                         'capture': capture, 'wall': time.perf_counter()})
        return True

    def fake_llm(*args):
        return SimpleNamespace(
            drop_reason=lambda *a: '', close=lambda: None,
            classify=lambda *a: ({'action': 'fetch', 'object': 'coke'}, {'wall_seconds': 0}),
        )

    config = pipeline.PipelineConfig(
        audio_stop=AudioStopConfig(
            mode='active', recheck_phase_shifts=phases, recheck_gain=gain,
        ),
        speaker=SpeakerConfig(mode='enforce', profile_path=str(profile),
                              allow_development_profile=True),
    )
    with patch.object(pipeline, 'LlmRuntime', fake_llm):
        instance = pipeline.VoiceCommandPipeline(config, publish, log, fetch_gate=lambda t: '')
    try:
        if not ready.wait(60) or not instance.speaker.ready:
            raise RuntimeError('speaker not ready')
        instance._start_audio_stop()
        if instance.audio_stop_recheck is None:
            raise RuntimeError('recheck not ready')
        audio_end = 0
        for sequence, sample in enumerate(samples, 1):
            path = Path(sample['path'])
            if hashlib.sha256(path.read_bytes()).hexdigest() != sample['sha256']:
                raise ValueError(f'WAV changed: {path}')
            pcm = read_pcm(path)
            now = time.perf_counter()
            audio_end += len(pcm) / 32000 + 1
            capture = {'voice_start_wall': now, 'utterance_start_wall': now,
                       'voice_end_wall': now, 'voice_end_audio': audio_end,
                       'utterance_seconds': len(pcm) / 32000, 'vad_end_silence_seconds': .9}
            utterance = Utterance(sequence, pcm, capture)
            log_start, command_start = len(logs), len(commands)
            instance.audio_stop_recheck.submit(utterance)
            result = instance.process_utterance(utterance)
            # STT usually finishes later, but explicitly wait for the recheck log.
            deadline = time.perf_counter() + 2
            while not any(f'sequence={sequence} ' in m and '[AUDIO STOP RECHECK ' in m
                          for m in logs[log_start:]):
                if time.perf_counter() > deadline:
                    raise RuntimeError('recheck did not finish')
                time.sleep(.01)
            row = {**sample, 'sequence': sequence, 'transcript': result['transcript'],
                   'speaker_reason': result['speaker_reason'],
                   'stt_seconds': result['stt_seconds'],
                   'commands': commands[command_start:], 'logs': logs[log_start:]}
            actions = [c['action'] for c in row['commands']]
            expected = {'target_stop': ['stop'], 'owner_fetch': ['fetch'], 'other_fetch': []}
            if actions != expected[sample['role']]:
                raise RuntimeError(f'Unexpected callback actions: {row}')
            if sample['role'] == 'other_fetch' and result['speaker_reason'] != 'speaker_rejected':
                raise RuntimeError(f'Other speaker was not rejected: {row}')
            rows.append(row)
            print(f'phases={phases} {sample["id"]} callbacks={actions}', flush=True)
    finally:
        instance.request_stop()
    return {'phases': phases, 'gain': gain, 'rows': rows, 'logs': logs}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--speaker-profile', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--recheck-gain', type=float, default=1.0)
    parser.add_argument('--extra-manifest', type=Path,
                        help='Additional target_stop clips; ambiguous inputs are excluded.')
    args = parser.parse_args()
    samples = json.loads(args.manifest.read_text())['samples']
    selected = []
    for row in samples:
        if row['id'] in {f'recheck_live_{i:02}' for i in range(1, 10)}:
            role = 'target_stop'
        elif row['id'] in {'genuine-dev_01', 'genuine-dev_02'}:
            role = 'owner_fetch'
        elif row['id'] in {'other-dev_04', 'other-dev_06'}:
            role = 'other_fetch'
        else:
            continue
        selected.append({**row, 'role': role,
                         'path': str((args.manifest.parent / row['file']).resolve())})
    if len(selected) != 13:
        raise ValueError('expected 9 stop + 2 owner fetch + 2 other fetch clips')
    if args.extra_manifest:
        for row in json.loads(args.extra_manifest.read_text())['samples']:
            if row['category'] == 'target_stop':
                selected.append({**row, 'role': 'target_stop', 'path': str((
                    args.extra_manifest.parent / row['file']
                ).resolve())})
    result = {
        'no_ros_no_microphone': True, 'development_only': True,
        'limitations': 'Completed WAV injection, fake IDLE/LLM, real KWS/STT/speaker. '
                       'No continuous stream input, live microphone or physical latency.',
        'profile_sha256': hashlib.sha256(args.speaker_profile.read_bytes()).hexdigest(),
        'manifest_sha256': hashlib.sha256(args.manifest.read_bytes()).hexdigest(),
        'extra_manifest_sha256': hashlib.sha256(args.extra_manifest.read_bytes()).hexdigest()
        if args.extra_manifest else None,
        'runs': [run(selected, args.speaker_profile, phases,
                     args.recheck_gain if phases else 1.0) for phases in (False, True)],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    print(f'results: {args.output}')


if __name__ == '__main__':
    main()
