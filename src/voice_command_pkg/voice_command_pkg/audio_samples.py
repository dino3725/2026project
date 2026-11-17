"""Convert capture PCM to the 16 kHz mono float32 input expected by Whisper."""

import numpy as np


def amplify_pcm(audio_data: bytes, gain: float) -> bytes:
    """Amplify an optional KWS copy, capping gain to avoid PCM clipping."""
    if type(gain) not in (int, float) or not np.isfinite(gain) or not 1 <= gain <= 4:
        raise ValueError('KWS gain must be finite and between 1 and 4')
    if not audio_data or len(audio_data) % 2:
        raise ValueError('PCM audio must contain complete 16-bit samples')
    if gain == 1:
        return audio_data
    samples = np.frombuffer(audio_data, dtype='<i2').astype(np.float32)
    peak = float(np.max(np.abs(samples)))
    if peak == 0 or peak >= 32767:
        return audio_data
    factor = min(gain, 32767 / peak)
    return np.rint(samples * factor).astype('<i2').tobytes()


def pcm_to_float32(audio_data: bytes) -> np.ndarray:
    """Normalize little-endian signed 16-bit mono PCM without resampling."""
    if not audio_data:
        raise ValueError('PCM audio must not be empty')
    if len(audio_data) % 2:
        raise ValueError('PCM audio must contain complete 16-bit samples')
    return np.frombuffer(audio_data, dtype='<i2').astype(np.float32) / np.float32(32768.0)
