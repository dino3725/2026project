"""Recheck completed VAD audio independently of the Whisper/LLM consumer."""

import threading
import time

from voice_command_pkg.audio_capture import FRAME_BYTES
from voice_command_pkg.audio_stop import AudioStopEvent
from voice_command_pkg.utterance_buffer import UtteranceBuffer


def recheck_audio(backend, pcm, cancelled=lambda: False):
    """Decode a fresh stream, without synthetic silence or transcription hints."""
    if cancelled():
        return None
    backend.reset()
    for offset in range(0, len(pcm), FRAME_BYTES):
        if cancelled():
            return None
        words = backend.accept(pcm[offset:offset + FRAME_BYTES])
        if cancelled():
            return None
        if words:
            return words[0], min(offset + FRAME_BYTES, len(pcm)) / 32000
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
                hit = recheck_audio(self.backend, utterance.audio, cancelled)
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
