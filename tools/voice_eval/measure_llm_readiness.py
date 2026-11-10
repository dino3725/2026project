#!/usr/bin/env python3
"""Measure warmup and development WAV processing with no microphone or ROS publisher."""

import argparse
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import threading
import time
import urllib.request

from compare_stt_inputs import read_pcm
from voice_command_pkg.command_classifier import SYSTEM_PROMPT
from voice_command_pkg.pipeline import PipelineConfig, VoiceCommandPipeline
from voice_command_pkg.utterance_buffer import Utterance


def get_json(url):
    """Read local Ollama status without loading or unloading a model."""
    with urllib.request.urlopen(url, timeout=5) as response:
        return json.load(response)


def main():
    """Record cold/warm conditions and real STT/LLM timings on explicit development audio."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--stop-wav', type=Path, required=True)
    parser.add_argument('--fetch-wavs', type=Path, nargs='+', required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--idle-seconds', type=float, default=60)
    args = parser.parse_args()
    if args.output.exists():
        parser.error('output already exists')
    if args.idle_seconds < 0:
        parser.error('idle seconds must be nonnegative')
    paths = [args.stop_wav, *args.fetch_wavs]
    audio = {path: read_pcm(path) for path in paths}
    config = PipelineConfig()
    base = config.classifier.ollama_url
    logs, results = [], []
    ready = threading.Event()

    def log(line):
        logs.append(line)
        print(line, flush=True)
        if line.startswith('[LLM READY]'):
            ready.set()

    report = {
        'ollama': get_json(base + '/api/version'),
        'models_before': get_json(base + '/api/ps'),
        'prompt_sha256': hashlib.sha256(SYSTEM_PROMPT.encode()).hexdigest(),
        'files': {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in paths},
        'measurement': 'Prerecorded PCM -> STT -> command, excludes microphone and VAD time',
        'publish_callback': None, 'results': results, 'logs': logs,
    }

    def process(instance, path, label, start=None):
        now = time.perf_counter()
        utterance = Utterance(len(results) + 1, audio[path], {
            'voice_start_wall': now if start is None else start,
            'utterance_start_wall': now if start is None else start,
            'voice_end_wall': now, 'utterance_seconds': len(audio[path]) / 32000,
            'vad_end_silence_seconds': 0,
        })
        row = instance.process_utterance(utterance)
        row.update(label=label, filename=path.name)
        results.append(row)
        return row

    before_warmup = time.perf_counter()
    instance = VoiceCommandPipeline(config, publish=None, log=log)
    try:
        process(instance, args.fetch_wavs[0], 'fetch_recorded_before_ready', before_warmup)
        process(instance, args.stop_wav, 'stop_during_startup')
        if not ready.wait(config.llm_warmup_timeout_seconds + 5):
            raise TimeoutError('Ollama warmup did not succeed')
        report['models_after_warmup'] = get_json(base + '/api/ps')
        for index, path in enumerate(args.fetch_wavs):
            process(instance, path, f'ready_fetch_{index + 1}')
        print(f'[BENCH IDLE] {args.idle_seconds}s', flush=True)
        time.sleep(args.idle_seconds)
        report['idle_seconds'] = args.idle_seconds
        report['models_after_idle'] = get_json(base + '/api/ps')
        process(instance, args.fetch_wavs[0], 'fetch_after_idle')
    finally:
        instance.request_stop()
        instance.llm.worker.join(timeout=1)

    # Port 1 is deliberately unavailable; the user's running Ollama is untouched.
    offline_config = replace(config, classifier=replace(
        config.classifier, ollama_url='http://127.0.0.1:1'
    ))
    offline = VoiceCommandPipeline(offline_config, publish=None, log=log)
    try:
        process(offline, args.stop_wav, 'stop_with_ollama_unavailable')
        process(offline, args.fetch_wavs[0], 'fetch_with_ollama_unavailable')
    finally:
        offline.request_stop()
        offline.llm.worker.join(timeout=1)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open('x', encoding='utf-8') as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2)
        handle.write('\n')
    print(f'[BENCH SAVED] {args.output}', flush=True)


if __name__ == '__main__':
    main()
