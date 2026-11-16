"""Pinned CPU SpeechBrain ECAPA architecture and weights, without remote code execution."""

import hashlib
import importlib.metadata
from pathlib import Path

import numpy as np

from voice_command_pkg.speaker_profile import MODEL_SPECS


BACKEND = 'speechbrain-ecapa-voxceleb-v1'
MODEL_PATH = ('~/.cache/eated/speaker_ecapa/'
              '0f99f2d0ebe89ac095bcc5903c4dd8f72b367286/embedding_model.ckpt')


class EcapaEncoder:
    """Use the official feature/normalization/network configuration with local weights."""

    def __init__(self):
        import torch
        from speechbrain.lobes.features import Fbank
        from speechbrain.lobes.models.ECAPA_TDNN import ECAPA_TDNN
        from speechbrain.processing.features import InputNormalization

        if importlib.metadata.version('speechbrain') != '1.0.3':
            raise ValueError('expected speechbrain 1.0.3')
        path = Path(MODEL_PATH).expanduser()
        if hashlib.sha256(path.read_bytes()).hexdigest() != MODEL_SPECS[BACKEND]['sha256']:
            raise ValueError('ECAPA speaker model SHA256 mismatch')
        self.features = Fbank(n_mels=80)
        self.normalize = InputNormalization(norm_type='sentence', std_norm=False)
        self.model = ECAPA_TDNN(
            input_size=80, channels=[1024, 1024, 1024, 1024, 3072],
            kernel_sizes=[5, 3, 3, 3, 1], dilations=[1, 2, 3, 4, 1],
            attention_channels=128, lin_neurons=192,
        )
        self.model.load_state_dict(torch.load(path, map_location='cpu', weights_only=True))
        self.model.eval()
        self.embed_utterance(np.zeros(16000, dtype=np.float32))

    def embed_utterance(self, speech):
        """Quantize the trimmed waveform consistently with the offline candidate evaluation."""
        import torch

        pcm = (np.clip(speech, -1, 1) * 32767).astype('<i2')
        samples = pcm.astype('float32') / 32768
        with torch.inference_mode():
            audio = torch.from_numpy(samples).unsqueeze(0)
            features = self.normalize(self.features(audio), torch.ones(1))
            return self.model(features).detach().numpy().reshape(-1)
