"""Convert capture PCM to the 16 kHz mono float32 input expected by Whisper."""

import numpy as np


def pcm_to_float32(audio_data: bytes) -> np.ndarray:
    """Normalize little-endian signed 16-bit mono PCM without resampling."""
    if not audio_data:
        raise ValueError('PCM audio must not be empty')
    if len(audio_data) % 2:
        raise ValueError('PCM audio must contain complete 16-bit samples')
    return np.frombuffer(audio_data, dtype='<i2').astype(np.float32) / np.float32(32768.0)
