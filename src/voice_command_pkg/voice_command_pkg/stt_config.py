"""Validated Whisper decoding options, shared by runtime and offline experiments."""

from dataclasses import dataclass
import math


DEVELOPMENT_HINT = '콜라, 휴지, 바세린, 에어팟, 멈춰, 정지, 스톱.'


@dataclass(frozen=True)
class SttConfig:
    """Keep baseline decoding unless an experiment explicitly changes it."""

    beam_size: int = 5
    initial_prompt: str = ''
    hotwords: str = ''
    temperatures: tuple[float, ...] = (0.0, 0.2, 0.4, 0.6, 0.8, 1.0)

    def __post_init__(self) -> None:
        if type(self.beam_size) is not int or self.beam_size < 1:
            raise ValueError('whisper.beam_size must be a positive integer')
        for name in ('initial_prompt', 'hotwords'):
            if not isinstance(getattr(self, name), str):
                raise ValueError(f'whisper.{name} must be a string')
        if not isinstance(self.temperatures, (tuple, list)) or not self.temperatures:
            raise ValueError('whisper.temperatures must be a non-empty list of numbers')
        if any(type(v) not in (float, int) or not math.isfinite(v) or not 0 <= v <= 1
               for v in self.temperatures):
            raise ValueError('whisper.temperatures must contain finite numbers in [0, 1]')
        object.__setattr__(self, 'temperatures', tuple(float(v) for v in self.temperatures))

    def options(self) -> dict:
        """Convert empty ROS strings to the decoder's disabled value."""
        return {
            'beam_size': self.beam_size,
            'initial_prompt': self.initial_prompt.strip() or None,
            'hotwords': self.hotwords.strip() or None,
            'temperature': self.temperatures,
        }


# Selected on the 2026-11-11 development audio; keep baseline available above.
DEFAULT_RUNTIME_STT = SttConfig(temperatures=(0.0,))
