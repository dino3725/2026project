"""Recheck completed VAD audio independently of the Whisper/LLM consumer."""

import threading
import time

from voice_command_pkg.audio_capture import FRAME_BYTES
from voice_command_pkg.audio_samples import amplify_pcm
from voice_command_pkg.audio_stop import AudioStopEvent
from voice_command_pkg.utterance_buffer import UtteranceBuffer


RECHECK_PREFIXES_MS = (0, 120, 300, 240, 180, 60)


def recheck_audio(backend, pcm, cancelled=lambda: False, *, phase_shifts=False, gain=1.0):
    """Retain the original first pass, then optionally shift/amplify a KWS copy."""
    # Fixed development candidates, not a search or a change to the audio/STT.
    # All attempts share the caller's deadline. No synthetic tail is appended.
    prefixes = RECHECK_PREFIXES_MS if phase_shifts else (0,)
    attempts = [(False, 0)] + ([(True, p) for p in prefixes] if gain > 1 else [
        (False, p) for p in prefixes[1:]
    ])
    amplified = None
    for use_gain, prefix_ms in attempts:
        if cancelled():
            return None
        if use_gain and amplified is None:
            amplified = amplify_pcm(pcm, gain)
        backend.reset()
        prefix_bytes = prefix_ms * 32
        candidate = bytes(prefix_bytes) + (amplified if use_gain else pcm)
        for offset in range(0, len(candidate), FRAME_BYTES):
            if cancelled():
                return None
            words = backend.accept(candidate[offset:offset + FRAME_BYTES])
            if cancelled():
                return None
            original_end = min(offset + FRAME_BYTES, len(candidate)) - prefix_bytes
            # Never assign a synthetic-prefix hit to the real utterance.
            if words and original_end > 0:
                return words[0], original_end / 32000
    return None


class AudioStopRecheckWorker:
    """Keep at most one pending utterance; never block the microphone on decoding."""

    def __init__(self, config, backend, on_stop, log):
        self.config, self.backend = config, backend
        self.on_stop, self.log = on_stop, log
        self.buffer = UtteranceBuffer(1)
        self.closed = threading.Event()
        self.worker = threading.Thread(target=self._run, name='audio_stop_recheck', daemon=True)
        self.worker.start()

    def submit(self, utterance):
        """Offer complete PCM with its original VAD timestamps."""
        dropped = self.buffer.put(utterance)
        if dropped is not None and not self.closed.is_set():
            self.log(f'[AUDIO STOP RECHECK DROP] sequence={dropped.sequence} reason=queue_full')

    def close(self):
        """Cancel decoding and discard queued utterances."""
        self.closed.set()
        self.buffer.close()
        if threading.current_thread() is not self.worker:
            self.worker.join(timeout=1.0)

    def _run(self):
        try:
            while not self.closed.is_set():
                utterance = self.buffer.get()
                if utterance is None:
                    continue
                capture = utterance.capture
                received = float(capture['voice_end_wall'])
                deadline = received + self.config.max_lag_seconds

                def cancelled():
                    return self.closed.is_set() or time.perf_counter() > deadline

                started = time.perf_counter()
                hit = recheck_audio(
                    self.backend, utterance.audio, cancelled,
                    phase_shifts=self.config.recheck_phase_shifts,
                    gain=self.config.recheck_gain,
                )
                if cancelled():
                    if not self.closed.is_set():
                        self.log(f'[AUDIO STOP RECHECK DROP] sequence={utterance.sequence} '
                                 'reason=expired')
                    continue
                if hit:
                    keyword, offset = hit
                    end = float(capture['voice_end_audio'])
                    start = end - len(utterance.audio) / 32000
                    self.on_stop(AudioStopEvent(
                        keyword, start + offset, received,
                        source='utterance_recheck', window=(start, end),
                    ))
                self.log(f'[AUDIO STOP RECHECK DONE] sequence={utterance.sequence} '
                         f'hit={bool(hit)} compute={time.perf_counter() - started:.3f}s')
        except Exception as exc:
            self.close()
            self.log(f'[AUDIO STOP RECHECK ERROR] disabled; other stop paths retained: {exc}')
