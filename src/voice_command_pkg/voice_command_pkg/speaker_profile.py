"""Local speaker templates and cosine decisions; never used to gate stop."""

from dataclasses import dataclass
import json
import math
from pathlib import Path

import numpy as np


@dataclass(frozen=True)
class SpeakerConfig:
    """Keep uncalibrated speaker verification observational by default."""

    mode: str = 'off'
    profile_path: str = '~/2026project/speaker_profiles/owner.json'
    wait_seconds: float = 1.0
    min_speech_seconds: float = 0.6
    allow_development_profile: bool = False

    def __post_init__(self):
        if self.mode not in ('off', 'shadow', 'enforce'):
            raise ValueError('speaker.mode must be off, shadow or enforce')
        if type(self.allow_development_profile) is not bool:
            raise ValueError('speaker.allow_development_profile must be a boolean')
        if not isinstance(self.profile_path, str) or not self.profile_path.strip():
            raise ValueError('speaker.profile_path must be nonempty')
        for name in ('wait_seconds', 'min_speech_seconds'):
            value = getattr(self, name)
            if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
                raise ValueError(f'speaker.{name} must be finite and positive')
        if self.min_speech_seconds > 15:
            raise ValueError('speaker.min_speech_seconds must be <= 15')


def unit_vector(vector):
    """Reject malformed embeddings rather than treating them as an unknown speaker score."""
    vector = np.asarray(vector, dtype=np.float64)
    if vector.ndim != 1 or vector.size != 256 or not np.isfinite(vector).all():
        raise ValueError('expected a finite 256-dimensional embedding')
    length = np.linalg.norm(vector)
    if not math.isfinite(length) or length < 1e-8:
        raise ValueError('invalid speaker embedding norm')
    return vector / length


def centroid(embeddings):
    """Average normalized enrollment vectors and normalize the result."""
    if not embeddings:
        raise ValueError('no enrollment embeddings')
    return unit_vector(np.mean([unit_vector(e) for e in embeddings], axis=0))


def similarity(vector, reference):
    """Return cosine similarity, not a probability or percent identity confidence."""
    return float(np.clip(np.dot(unit_vector(vector), unit_vector(reference)), -1, 1))


def load_profile(path, model_sha256, require_development=False):
    """Validate a JSON template; profiles are immutable for the life of a node."""
    data = json.loads(Path(path).expanduser().read_text())
    if (data.get('schema') != 1 or data.get('backend') != 'resemblyzer-0.1.4'
            or data.get('model_sha256') != model_sha256 or data.get('sample_rate') != 16000):
        raise ValueError('speaker profile model or format mismatch')
    unit_vector(data['embedding'])
    threshold = data['threshold']
    if (type(threshold) not in (int, float) or not math.isfinite(threshold)
            or not -1 <= threshold <= 1):
        raise ValueError('invalid speaker threshold')
    if data.get('min_speech_seconds') != 0.6:
        raise ValueError('speaker profile preprocessing mismatch')
    if type(data.get('calibrated')) is not bool:
        raise ValueError('missing calibration state')
    hashes = data.get('enrollment_hashes', [])
    if (not isinstance(hashes, list) or len(hashes) < 20
            or any(not isinstance(h, str) or len(h) != 64 for h in hashes)
            or len(set(hashes)) != len(hashes)):
        raise ValueError('profile needs at least 20 distinct enrollment clips')
    if data['calibrated'] or require_development:
        seen = set(hashes)
        evidence_names = ('development', 'validation') if data['calibrated'] else ('development',)
        for name in evidence_names:
            evidence = data.get(name, {})
            genuine = evidence.get('genuine_count', 0)
            other = evidence.get('other_count', 0)
            scores_a = evidence.get('genuine_scores', [])
            scores_b = evidence.get('other_scores', [])
            clips = evidence.get('hashes', [])
            if (type(genuine) is not int or type(other) is not int
                    or genuine < 10 or other < 10
                    or len(scores_a) != genuine or len(scores_b) != other
                    or len(clips) != genuine + other or len(set(clips)) != len(clips)
                    or seen.intersection(clips) or evidence.get('threshold') != threshold):
                raise ValueError('missing or overlapping speaker validation evidence')
            if any(x is not None and (type(x) not in (float, int)
                   or not math.isfinite(x) or not -1 <= x <= 1) for x in scores_a + scores_b):
                raise ValueError('invalid speaker validation score')
            if (sum(x is None or x < threshold for x in scores_a) / genuine > .1
                    or any(x is not None and x >= threshold for x in scores_b)):
                raise ValueError('speaker validation does not meet acceptance criteria')
            seen.update(clips)
    return data


def choose_threshold(genuine, impostor, max_frr=0.1):
    """Choose a development threshold; disjoint validation is still required."""
    if len(genuine) < 10 or len(impostor) < 10:
        raise ValueError('need at least 10 genuine and 10 consented impostor development clips')
    if not all(math.isfinite(x) and -1 <= x <= 1 for x in genuine + impostor):
        raise ValueError('invalid cosine scores')
    threshold = math.nextafter(max(impostor), math.inf)
    frr = sum(x < threshold for x in genuine) / len(genuine)
    far = sum(x >= threshold for x in impostor) / len(impostor)
    if threshold > 1 or frr > max_frr:
        raise ValueError('development scores overlap too much; do not enable enforcement')
    return {'threshold': threshold, 'development_frr': frr, 'development_far': far}
