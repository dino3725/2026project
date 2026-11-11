#!/usr/bin/env python3
"""Compare decoding on a labelled development manifest; no ROS or microphone access."""

import argparse
from dataclasses import asdict
import hashlib
import importlib.metadata
import json
from pathlib import Path
import time
import unicodedata

from compare_stt_inputs import read_pcm
from faster_whisper import WhisperModel
import numpy as np

from voice_command_pkg.command_classifier import (
    classify_command_with_metrics, ClassifierConfig, SYSTEM_PROMPT,
)
from voice_command_pkg.pipeline import PipelineError, transcribe_audio
from voice_command_pkg.stt_config import DEVELOPMENT_HINT, SttConfig
from voice_command_pkg.transcript_rules import find_stop_keyword, stop_command


SETTINGS = {
    'beam5_plain': SttConfig(),
    'beam5_hint': SttConfig(initial_prompt=DEVELOPMENT_HINT),
    'beam1_plain': SttConfig(beam_size=1),
    'beam1_hint': SttConfig(beam_size=1, initial_prompt=DEVELOPMENT_HINT),
    'beam5_temperature0': SttConfig(temperatures=(0.0,)),
}
DEFAULT_SETTINGS = ['beam5_plain', 'beam5_hint', 'beam1_plain', 'beam1_hint']


def normalize(text):
    """Ignore spacing/punctuation but preserve Korean spelling differences."""
    return ''.join(c for c in unicodedata.normalize('NFKC', text).casefold() if c.isalnum())


def distance(reference, hypothesis):
    """Compute character edit distance, including insertions and deletions."""
    previous = list(range(len(hypothesis) + 1))
    for i, left in enumerate(reference, 1):
        current = [i]
        for j, right in enumerate(hypothesis, 1):
            current.append(min(current[-1] + 1, previous[j] + 1,
                               previous[j - 1] + (left != right)))
        previous = current
    return previous[-1]


class ObservedModel:
    """Observe returned segment diagnostics without changing production inference."""

    def __init__(self, model):
        self.model = model
        self.segments = []

    def transcribe(self, *args, **kwargs):
        self.segments = []
        segments, info = self.model.transcribe(*args, **kwargs)

        def consume():
            for segment in segments:
                self.segments.append({key: getattr(segment, key) for key in (
                    'text', 'temperature', 'avg_logprob', 'no_speech_prob', 'compression_ratio',
                )})
                yield segment

        return consume(), info


def load_samples(manifest_path):
    """Reject missing, duplicate or unlabelled audio instead of guessing its order."""
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    if manifest.get('purpose') != 'development':
        raise ValueError('Only explicitly labelled development data is supported')
    samples, seen = [], set()
    allowed = {('fetch', x) for x in ('coke', 'tissue', 'vaseline', 'airpod')}
    allowed |= {('stop', ''), ('unknown', '')}
    for entry in manifest['samples']:
        path = (manifest_path.parent / entry['file']).resolve()
        if path in seen or (entry['action'], entry['object']) not in allowed:
            raise ValueError(f'Duplicate file or invalid label: {path}')
        if not isinstance(entry['reference'], str) or not entry['reference'].strip():
            raise ValueError(f'Missing reference: {path}')
        pcm = read_pcm(path)
        if not pcm:
            raise ValueError(f'Empty WAV: {path}')
        seen.add(path)
        sha = hashlib.sha256(path.read_bytes()).hexdigest()
        if entry.get('sha256', sha) != sha:
            raise ValueError(f'Audio SHA256 mismatch: {path}')
        samples.append(({**entry, 'file': str(path), 'sha256': sha,
                         'duration_seconds': len(pcm) / 32000, 'control': False}, pcm))
    if not samples:
        raise ValueError('Manifest has no samples')
    return samples


def summarize(rows, with_commands):
    """Report development accuracy separately from synthetic silence controls."""
    result = {}
    for name in SETTINGS:
        selected = [r for r in rows if r['setting'] == name and not r['control']]
        if not selected:
            continue
        controls = [r for r in rows if r['setting'] == name and r['control']]
        stops = [r for r in selected if r['action'] == 'stop']
        negatives = [r for r in selected if r['action'] != 'stop']
        result[name] = {
            'runs': len(selected),
            'normalized_exact': sum(r['normalized_exact'] for r in selected),
            'cer': (sum(r['edits'] for r in selected)
                    / sum(r['reference_length'] for r in selected)),
            'median_seconds': float(np.median([r['seconds'] for r in selected])),
            'p90_seconds': float(np.percentile([r['seconds'] for r in selected], 90)),
            'max_seconds': max(r['seconds'] for r in selected),
            'stop_detected': sum(bool(r['stop_keyword']) for r in stops),
            'stop_runs': len(stops),
            'false_stop': sum(bool(r['stop_keyword']) for r in negatives),
            'non_stop_runs': len(negatives),
            'control_nonempty': sum(bool(r['transcript']) for r in controls),
            'control_false_stop': sum(bool(r['stop_keyword']) for r in controls),
            'control_runs': len(controls),
            'errors': sum(bool(r['error']) for r in selected + controls),
        }
        if with_commands:
            result[name]['command_correct'] = sum(
                r.get('command_correct', False) for r in selected
            )
            result[name]['command_errors'] = sum(bool(r.get('command_error')) for r in selected)
            result[name]['control_command_errors'] = sum(
                bool(r.get('command_error')) for r in controls
            )
            result[name]['control_wrong_command'] = sum(
                r.get('command') is not None and not r.get('command_correct', False)
                for r in controls
            )
    return result


def main():
    """Alternate order, checkpoint each run, then optionally check frozen LLM outputs."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('manifest', type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--rounds', type=int, default=2)
    parser.add_argument(
        '--settings', nargs='+', choices=list(SETTINGS), default=DEFAULT_SETTINGS,
    )
    parser.add_argument(
        '--classify', action='store_true', help='Use local Ollama after STT finishes',
    )
    args = parser.parse_args()
    trace = args.output.with_suffix('.jsonl')
    if args.rounds < 1 or args.output.exists() or trace.exists():
        parser.error('Use positive rounds and fresh output/trace paths')
    samples = load_samples(args.manifest)
    # These expose decoder hallucinations on zero PCM, not room-noise robustness.
    for duration in (1, 3):
        samples.append(({'file': f'synthetic_silence_{duration}s', 'reference': '',
                         'action': 'unknown', 'object': '', 'control': True,
                         'duration_seconds': duration}, bytes(duration * 32000)))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    started = time.perf_counter()
    model = ObservedModel(WhisperModel(
        'small', device='cpu', compute_type='int8', local_files_only=True,
    ))
    report = {
        'manifest_sha256': hashlib.sha256(args.manifest.read_bytes()).hexdigest(),
        'model': 'small', 'device': 'cpu', 'compute_type': 'int8', 'language': 'ko',
        'faster_whisper': importlib.metadata.version('faster-whisper'),
        'ctranslate2': importlib.metadata.version('ctranslate2'),
        'load_seconds': time.perf_counter() - started, 'rounds': args.rounds,
        'settings': {k: asdict(SETTINGS[k]) for k in args.settings},
        'system_prompt_sha256': hashlib.sha256(SYSTEM_PROMPT.encode()).hexdigest(),
        'note': ('Development tuning, not held-out accuracy. '
                 'Cached LLM results are not latency data.'),
        'results': [],
    }
    transcribe_audio(model, samples[0][1], 'ko', SttConfig())  # Exclude warmup from metrics.
    names = list(dict.fromkeys(args.settings))
    with trace.open('x', encoding='utf-8') as handle:
        for round_index in range(args.rounds):
            for index, (entry, pcm) in enumerate(samples):
                offset = (index + round_index) % len(names)
                order = names[offset:] + names[:offset]
                if round_index % 2:
                    order.reverse()
                for name in order:
                    started = time.perf_counter()
                    error, text = '', ''
                    try:
                        text, seconds = transcribe_audio(model, pcm, 'ko', SETTINGS[name])
                    except PipelineError as exc:
                        seconds = time.perf_counter() - started
                        # Empty on silence is expected; decoder failures are still errors.
                        if not (entry['control'] and '전사 결과가 비어' in str(exc)):
                            error = exc.describe()
                    ref, hyp = normalize(entry['reference']), normalize(text)
                    row = {**entry, 'setting': name, 'round': round_index + 1,
                           'transcript': text, 'seconds': seconds, 'error': error,
                           'normalized_exact': ref == hyp, 'edits': distance(ref, hyp),
                           'reference_length': len(ref), 'stop_keyword': find_stop_keyword(text),
                           'segments': model.segments}
                    report['results'].append(row)
                    handle.write(json.dumps(row, ensure_ascii=False) + '\n')
                    handle.flush()
                print(f'round={round_index + 1} sample={index + 1}/{len(samples)}', flush=True)
    report['summary'] = summarize(report['results'], False)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8',
    )
    if args.classify:
        cache = {}
        classify_command_with_metrics('콜라 가져다줘', ClassifierConfig(timeout_seconds=120))
        for row in report['results']:
            text = row['transcript']
            if text not in cache:
                try:
                    if find_stop_keyword(text):
                        command = stop_command()
                    elif not text:
                        command = {'action': 'unknown', 'object': ''}
                    else:
                        command, _ = classify_command_with_metrics(text)
                    cache[text] = (command, '')
                except Exception as exc:
                    cache[text] = (None, f'{type(exc).__name__}: {exc}')
                print(f'classified unique transcript={len(cache)}', flush=True)
            row['command'], row['command_error'] = cache[text]
            row['command_correct'] = row['command'] == {
                'action': row['action'], 'object': row['object'],
            }
        report['unique_transcripts'] = len(cache)
        report['summary'] = summarize(report['results'], True)
        args.output.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8',
        )
    print(json.dumps(report['summary'], ensure_ascii=False, indent=2), flush=True)


if __name__ == '__main__':
    main()
