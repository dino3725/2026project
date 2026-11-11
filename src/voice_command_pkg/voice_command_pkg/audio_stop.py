"""Bounded background keyword inference, fed by the existing microphone stream."""

from collections import deque
from dataclasses import dataclass
import threading
import time

from voice_command_pkg.audio_capture import FRAME_BYTES
from voice_command_pkg.audio_stop_model import AudioStopConfig, STOP_WORDS


@dataclass(frozen=True)
class AudioStopEvent:
    """A detection's input position and local read time, not physical latency."""

    keyword: str
    audio_end: float
    received_at: float


class AudioStopWorker:
    """Never run model inference or publication on the capture thread."""

    def __init__(self, config: AudioStopConfig, backend, on_stop, log):
        self.config = config
        self.backend = backend
        self.on_stop = on_stop
        self.log = log
        self._condition = threading.Condition()
        self._frames = deque()
        self._generation = 0
        self._closed = False
        self.dropped_frames = 0
        self.worker = threading.Thread(target=self._run, name='audio_stop', daemon=True)
        self.worker.start()

    def submit(self, pcm: bytes, audio_end: float, received_at: float):
        """Enqueue a frame without waiting for inference, I/O or callbacks."""
        if len(pcm) != FRAME_BYTES:
            raise ValueError('audio stop requires a complete 30 ms PCM frame')
        with self._condition:
            if self._closed:
                return
            if len(self._frames) >= self.config.queue_frames:
                self.dropped_frames += len(self._frames)
                self._frames.clear()
                self._generation += 1
            self._frames.append((pcm, audio_end, received_at, self._generation))
            self._condition.notify()

    def close(self):
        """Discard pending frames; a late inference cannot dispatch a new stop."""
        with self._condition:
            self._closed = True
            self._frames.clear()
            self._generation += 1
            self._condition.notify_all()
        if threading.current_thread() is not self.worker:
            self.worker.join(timeout=1.0)

    def _run(self):
        generation = 0
        try:
            while True:
                with self._condition:
                    self._condition.wait_for(lambda: self._closed or self._frames)
                    if self._closed:
                        return
                    pcm, audio_end, received_at, current = self._frames.popleft()
                    if time.perf_counter() - received_at > self.config.max_lag_seconds:
                        self.dropped_frames += 1 + len(self._frames)
                        self._frames.clear()
                        self._generation += 1
                        continue
                if current != generation:
                    self.backend.reset()
                    generation = current
                    self.log(f'[AUDIO STOP RESET] dropped_frames={self.dropped_frames}')
                words = self.backend.accept(pcm)
                with self._condition:
                    usable = (
                        not self._closed and current == self._generation
                        and time.perf_counter() - received_at <= self.config.max_lag_seconds
                    )
                if usable:
                    for word in words:
                        if word not in STOP_WORDS:
                            raise ValueError(f'unexpected audio stop keyword: {word}')
                        self.on_stop(AudioStopEvent(word, audio_end, received_at))
        except Exception as exc:
            # Model/callback failure disables only this optional path. Capture,
            # Whisper and the deterministic transcript stop rule remain alive.
            self.close()
            self.log(f'[AUDIO STOP ERROR] disabled; transcript fallback retained: {exc}')
