"""Private speaker datasets: strict PCM input, disjoint hashes and local JSON."""

import hashlib
import json
import os
from pathlib import Path
import wave


def read_pcm(path):
    """Read bounded 16 kHz mono PCM16 audio without importing STT or ROS."""
    with wave.open(str(path), 'rb') as wav:
        if (wav.getframerate(), wav.getnchannels(), wav.getsampwidth(), wav.getcomptype()) != (
            16000, 1, 2, 'NONE'
        ) or not 0 < wav.getnframes() <= 15 * 16000:
            raise ValueError(f'{path}: expected 0–15 s of 16 kHz mono PCM16')
        pcm = wav.readframes(wav.getnframes())
        if len(pcm) != 2 * wav.getnframes():
            raise ValueError(f'{path}: truncated WAV')
        return pcm


def private_json(path, data):
    """Create a private JSON file exclusively; never overwrite enrollment evidence."""
    path = Path(path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)
        handle.write('\n')


def dataset(path, purpose, minimum, excluded=()):
    """Validate roles and PCM hashes; rewrapped WAV files cannot evade overlap checks."""
    path = Path(path).resolve()
    manifest = json.loads(path.read_text())
    if manifest.get('purpose') != purpose or manifest.get('complete') is not True:
        raise ValueError(f'{path}: wrong purpose or incomplete recording')
    if purpose.startswith('other') and manifest.get('consent') is not True:
        raise ValueError('other-speaker recordings require consent')
    rows = manifest['clips']
    if len(rows) < minimum:
        raise ValueError(f'{purpose}: need at least {minimum} clips')
    seen = set(excluded)
    result = []
    for row in rows:
        audio_path = (path.parent / row['file']).resolve()
        if audio_path.parent != path.parent:
            raise ValueError('clip must be inside its recording directory')
        pcm = read_pcm(audio_path)
        digest = hashlib.sha256(pcm).hexdigest()
        if digest != row['pcm_sha256'] or digest in seen:
            raise ValueError('audio changed, duplicated or reused across data splits')
        seen.add(digest)
        result.append((digest, pcm))
    return result
