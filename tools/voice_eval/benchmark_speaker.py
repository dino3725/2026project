#!/usr/bin/env python3
"""Compare local speaker CPU inference; optional ECAPA weights must be supplied explicitly."""

import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
from pathlib import Path
import statistics
import time

import numpy as np
from speaker_audio import private_json, read_pcm

from voice_command_pkg.speaker_backend import SpeakerEncoder


ECAPA_SHA256 = '0575cb64845e6b9a10db9bcb74d5ac32b326b8dc90352671d345e2ee3d0126a2'


def ecapa_encoder(path):
    """Instantiate the published ECAPA architecture locally, without remote YAML execution."""
    import torch
    from speechbrain.lobes.features import Fbank
    from speechbrain.lobes.models.ECAPA_TDNN import ECAPA_TDNN
    from speechbrain.processing.features import InputNormalization

    if hashlib.sha256(path.read_bytes()).hexdigest() != ECAPA_SHA256:
        raise ValueError('ECAPA model SHA256 mismatch')
    torch.set_num_threads(1)
    features = Fbank(n_mels=80)
    normalize = InputNormalization(norm_type='sentence', std_norm=False)
    model = ECAPA_TDNN(
        input_size=80, channels=[1024, 1024, 1024, 1024, 3072],
        kernel_sizes=[5, 3, 3, 3, 1], dilations=[1, 2, 3, 4, 1],
        attention_channels=128, lin_neurons=192,
    )
    model.load_state_dict(torch.load(path, map_location='cpu', weights_only=True))
    model.eval()

    def encode(pcm):
        with torch.inference_mode():
            samples = np.frombuffer(pcm, dtype='<i2').astype('float32') / 32768
            audio = torch.from_numpy(samples).unsqueeze(0)
            return model(normalize(features(audio), torch.ones(1)))
    encode(bytes(32000))
    return encode


def measure(files, encode):
    """Measure embeddings only, with quality failures visible in the result."""
    rows = []
    for path, pcm in files:
        started = time.perf_counter()
        error = ''
        try:
            encode(pcm)
        except ValueError as exc:
            error = str(exc)
        rows.append({'file': str(path), 'pcm_sha256': hashlib.sha256(pcm).hexdigest(),
                     'seconds': time.perf_counter() - started, 'error': error})
    return rows


def paired_stt(files, encode):
    """Alternate order to expose CPU contention with the configured Whisper model."""
    from voice_command_pkg.pipeline import load_whisper_model, PipelineConfig, transcribe_audio

    config = PipelineConfig()
    model, _ = load_whisper_model(config)
    transcribe_audio(model, files[0][1], config.language, config.stt)
    rows = []
    with ThreadPoolExecutor(max_workers=1) as pool:
        for index, (path, pcm) in enumerate(files):
            row = {'file': str(path)}
            for mode in (('alone', 'parallel') if index % 2 == 0 else ('parallel', 'alone')):
                started = time.perf_counter()
                future = pool.submit(encode, pcm) if mode == 'parallel' else None
                text, seconds = transcribe_audio(model, pcm, config.language, config.stt)
                done = future.done() if future else None
                if future:
                    future.result()
                row[mode] = {'seconds': time.perf_counter() - started,
                             'stt_seconds': seconds, 'text': text,
                             'auth_done_at_stt_end': done}
            rows.append(row)
    return rows


def main():
    """Benchmark development WAVs without enrolling a user or publishing commands."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('files', type=Path, nargs='+')
    parser.add_argument('--ecapa-model', type=Path)
    parser.add_argument('--with-stt', action='store_true')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error('choose a fresh output path')
    files = [(path, read_pcm(path)) for path in args.files]
    encoder = SpeakerEncoder()
    results = {'resemblyzer': measure(files, encoder.encode),
               'note': 'CPU timing only, not speaker accuracy. Different model preprocessing.'}
    if args.ecapa_model:
        results['ecapa'] = measure(files, ecapa_encoder(args.ecapa_model))
    if args.with_stt:
        results['paired_stt'] = paired_stt(files, encoder.encode)
    private_json(args.output, results)
    for name in ('resemblyzer', 'ecapa'):
        if name in results:
            median = statistics.median(row['seconds'] for row in results[name])
            print(f'{name}: median={median:.3f}s, clips={len(results[name])}')
    print(args.output)


if __name__ == '__main__':
    main()
