"""Pinned local speaker encoders; identical enrollment/runtime preprocessing."""

import hashlib
from pathlib import Path

import numpy as np

from voice_command_pkg.audio_samples import pcm_to_float32
from voice_command_pkg.speaker_profile import MODEL_SPECS, unit_vector


MODEL_SHA256 = '39373b86598fa3da9fcddee6142382efe09777e8d37dc9c0561f41f0070f134e'


class SpeakerEncoder:
    """Use one CPU inference thread and the same preprocessing for enrollment and checks."""

    def __init__(self, min_speech_seconds=0.6, backend='resemblyzer-0.1.4'):
        import importlib.metadata
        import resemblyzer
        import torch
        from resemblyzer import VoiceEncoder

        if importlib.metadata.version('resemblyzer') != '0.1.4':
            raise ValueError('expected resemblyzer 0.1.4')
        if backend not in MODEL_SPECS:
            raise ValueError('unsupported speaker backend')
        self.dimension = MODEL_SPECS[backend]['dimension']
        torch.set_num_threads(1)
        if backend == 'resemblyzer-0.1.4':
            path = Path(resemblyzer.__file__).parent / 'pretrained.pt'
            if hashlib.sha256(path.read_bytes()).hexdigest() != MODEL_SHA256:
                raise ValueError('speaker model SHA256 mismatch')
            self.encoder = VoiceEncoder(device='cpu', verbose=False)
            self.encoder.eval()
        else:
            from voice_command_pkg.speaker_ecapa import EcapaEncoder
            self.encoder = EcapaEncoder()
        self.min_speech_seconds = min_speech_seconds
        # Compile/load feature extraction before advertising readiness. Never
        # enroll this synthetic warmup or use it to approve a speaker.
        self.encoder.embed_utterance(np.zeros(16000, dtype=np.float32))

    def encode(self, pcm):
        """Reject silent, too-short or clipped speech before extracting an embedding."""
        samples = pcm_to_float32(pcm)
        if len(samples) > 16000 * 15:
            raise ValueError('speaker_audio_too_long')
        if np.sqrt(np.mean(samples ** 2)) < 0.003162:
            raise ValueError('speaker_audio_too_quiet')
        if np.mean(np.abs(samples) >= .999) > .02:
            raise ValueError('speaker_audio_clipped')
        from resemblyzer import preprocess_wav

        speech = preprocess_wav(samples)
        if len(speech) < 16000 * self.min_speech_seconds or not np.isfinite(speech).all():
            raise ValueError('speaker_audio_too_short')
        return unit_vector(self.encoder.embed_utterance(speech), self.dimension)
