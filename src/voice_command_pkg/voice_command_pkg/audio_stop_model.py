"""Pinned Korean keyword model; optional, offline-only runtime loading."""

from dataclasses import dataclass
import hashlib
import math
from pathlib import Path
import tempfile


MODEL_REPO = 'kairess/ko-kws-zipformer-tiny'
MODEL_REVISION = 'c2430dd975019802a68077e0fc4dd0521ae193e9'
DEFAULT_MODEL_DIR = f'~/.cache/eated/ko-kws-zipformer-tiny/{MODEL_REVISION}'
MODEL_HASHES = {
    'encoder.int8.onnx': 'af27dc0c6513d84a310b300cd8757d95b24911e2374f8dc83af5162b4f28b331',
    'decoder.int8.onnx': '2869dbec007411fbc19f0106e2995033d7de068fffdee8880ba479a12130c4ed',
    'joiner.int8.onnx': '3c8d9607af03973f0ecf78ab7f152d8a8b6e9764cd2014f4273e8f14fa53e196',
    'tokens.txt': '5ba64ac2c01404eb94393c844f8b009dff396dc286c79390cd9557b927f4e2ba',
}
# Tokenized with the pinned model's bpe.model; no tokenizer dependency at runtime.
KEYWORD_LINES = '▁ 멈 춰 @멈춰\n▁정 지 @정지\n▁스 톱 @스톱\n'
STOP_WORDS = frozenset({'멈춰', '정지', '스톱'})


@dataclass(frozen=True)
class AudioStopConfig:
    """Keep the experimental detector disabled until live validation."""

    mode: str = 'off'
    model_dir: str = DEFAULT_MODEL_DIR
    threshold: float = 0.01
    score: float = 1.0
    queue_frames: int = 128
    max_lag_seconds: float = 0.5

    def __post_init__(self):
        if self.mode not in ('off', 'shadow', 'active'):
            raise ValueError('audio_stop.mode must be off, shadow or active')
        if not isinstance(self.model_dir, str) or not self.model_dir.strip():
            raise ValueError('audio_stop.model_dir must be a nonempty string')
        for name in ('threshold', 'score', 'max_lag_seconds'):
            value = getattr(self, name)
            if isinstance(value, bool) or not math.isfinite(value) or value <= 0:
                raise ValueError(f'audio_stop.{name} must be finite and positive')
        if self.threshold > 1:
            raise ValueError('audio_stop.threshold must be <= 1')
        if type(self.queue_frames) is not int or self.queue_frames < 1:
            raise ValueError('audio_stop.queue_frames must be a positive integer')


def verify_model(directory: Path) -> None:
    """Reject missing/changed model files before calling native inference code."""
    for name, expected in MODEL_HASHES.items():
        path = directory / name
        if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise ValueError(f'audio stop model SHA256 mismatch: {path}')


class KoreanStopSpotter:
    """Decode PCM incrementally on one CPU thread, independently of Whisper."""

    def __init__(self, config: AudioStopConfig):
        directory = Path(config.model_dir).expanduser()
        verify_model(directory)
        import sherpa_onnx

        # sherpa reads the keyword file during construction. Never download or
        # rewrite the user's model cache from the live microphone process.
        with tempfile.TemporaryDirectory(prefix='eated_stop_') as temporary:
            keywords = Path(temporary) / 'keywords.txt'
            keywords.write_text(KEYWORD_LINES, encoding='utf-8')
            self.spotter = sherpa_onnx.KeywordSpotter(
                tokens=str(directory / 'tokens.txt'),
                encoder=str(directory / 'encoder.int8.onnx'),
                decoder=str(directory / 'decoder.int8.onnx'),
                joiner=str(directory / 'joiner.int8.onnx'),
                keywords_file=str(keywords), num_threads=1, provider='cpu',
                keywords_threshold=config.threshold, keywords_score=config.score,
                max_active_paths=4, num_trailing_blanks=1,
            )
        self.reset()

    def reset(self):
        """Discard all acoustic context after a gap in input frames."""
        self.stream = self.spotter.create_stream()

    def accept(self, pcm: bytes) -> list[str]:
        """Return newly detected keywords; input is 16 kHz mono PCM16."""
        from voice_command_pkg.audio_samples import pcm_to_float32

        self.stream.accept_waveform(16000, pcm_to_float32(pcm))
        words = []
        while self.spotter.is_ready(self.stream):
            self.spotter.decode_stream(self.stream)
            word = self.spotter.get_result(self.stream)
            if word:
                self.spotter.reset_stream(self.stream)
                if word not in STOP_WORDS:
                    raise ValueError(f'unexpected audio stop keyword: {word}')
                words.append(word)
        return words
