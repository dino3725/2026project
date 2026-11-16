"""Bounded background verification; errors close fetch admission, never stop."""

from concurrent.futures import Future, TimeoutError
from dataclasses import dataclass
import queue
import threading
import time

from voice_command_pkg.speaker_backend import SpeakerEncoder
from voice_command_pkg.speaker_profile import load_profile, similarity


@dataclass(frozen=True)
class SpeakerDecision:
    """An utterance-local decision; score is cosine similarity, not confidence."""

    allowed: bool
    reason: str
    score: float | None = None
    compute_seconds: float = 0.0


class SpeakerRuntime:
    """Load locally in a daemon and permit only one outstanding inference."""

    def __init__(self, config, log, stop_event):
        self.config, self.log, self.stop_event = config, log, stop_event
        self.closed = threading.Event()
        self.lock = threading.Lock()
        self.jobs = queue.Queue(maxsize=1)
        self.ready = False
        self.busy = False
        self.error = 'speaker_preparing'
        self.worker = None
        if config.mode != 'off':
            self.worker = threading.Thread(target=self._run, daemon=True, name='speaker_auth')
            self.worker.start()

    def submit(self, pcm):
        """Start in parallel with STT without waiting for model readiness or a busy worker."""
        ticket = Future()
        with self.lock:
            reason = ''
            if self.config.mode == 'off':
                ticket.set_result(SpeakerDecision(True, 'speaker_off'))
                return ticket
            if self.closed.is_set() or self.stop_event.is_set():
                reason = 'speaker_shutdown'
            elif not self.ready:
                reason = self.error
            elif self.busy:
                reason = 'speaker_busy'
            if reason:
                ticket.set_result(SpeakerDecision(False, reason))
            else:
                self.busy = True
                self.jobs.put_nowait((ticket, pcm))
        return ticket

    def finish(self, ticket, deadline):
        """Bound any extra wait; shadow never delays classification for verification."""
        if self.config.mode == 'shadow':
            deadline = time.perf_counter()
        else:
            deadline = min(deadline, time.perf_counter() + self.config.wait_seconds)
        while not self.closed.is_set() and not self.stop_event.is_set():
            if ticket.done():
                if ticket.cancelled():
                    return SpeakerDecision(False, 'speaker_cancelled')
                return ticket.result()
            remaining = deadline - time.perf_counter()
            if remaining <= 0:
                ticket.cancel()
                return SpeakerDecision(False, 'speaker_timeout')
            try:
                return ticket.result(timeout=min(.02, remaining))
            except TimeoutError:
                pass
        ticket.cancel()
        return SpeakerDecision(False, 'speaker_shutdown')

    def close(self):
        """Do not wait for model inference to finish during stop/shutdown."""
        self.closed.set()

    def _run(self):
        try:
            development_override = (
                self.config.mode == 'enforce' and self.config.allow_development_profile
            )
            profile = load_profile(self.config.profile_path, None, development_override)
            if self.config.min_speech_seconds != profile['min_speech_seconds']:
                raise ValueError('speaker runtime preprocessing differs from enrollment')
            if (self.config.mode == 'enforce' and not profile['calibrated']
                    and not development_override):
                raise ValueError('speaker profile needs disjoint validation before enforcement')
            encoder = SpeakerEncoder(self.config.min_speech_seconds, profile['backend'])
            with self.lock:
                if self.closed.is_set() or self.stop_event.is_set():
                    return
                self.ready = True
            self.log(f'[SPEAKER READY] mode={self.config.mode} calibrated={profile["calibrated"]} '
                     f'development_override={development_override and not profile["calibrated"]} '
                     f'backend={profile["backend"]} threshold={profile["threshold"]:.6f}')
        except Exception as exc:
            with self.lock:
                self.error = 'speaker_unavailable'
            self.log(f'[SPEAKER ERROR] fetch verification unavailable; stop retained: {exc}')
            return
        while not self.closed.is_set() and not self.stop_event.is_set():
            try:
                ticket, pcm = self.jobs.get(timeout=.1)
            except queue.Empty:
                continue
            try:
                if not ticket.set_running_or_notify_cancel():
                    continue
                started = time.perf_counter()
                try:
                    score = similarity(encoder.encode(pcm), profile['embedding'])
                    allowed = score >= profile['threshold']
                    decision = SpeakerDecision(allowed, 'speaker_verified' if allowed else
                                               'speaker_rejected', score,
                                               time.perf_counter() - started)
                except Exception as exc:
                    decision = SpeakerDecision(False, 'speaker_error',
                                               compute_seconds=time.perf_counter() - started)
                    self.log(f'[SPEAKER ERROR] utterance rejected; stop retained: {exc}')
                ticket.set_result(decision)
            finally:
                with self.lock:
                    self.busy = False
