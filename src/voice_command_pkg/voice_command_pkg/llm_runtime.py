"""Keep one Ollama request worker, with asynchronous warmup and bounded waits."""

from dataclasses import dataclass, replace
import threading
import time

from voice_command_pkg.command_classifier import classify_command_with_metrics


class LlmUnavailable(Exception):
    """A request was rejected or failed; it must never be replayed."""

    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


class LlmStopped(Exception):
    """Shutdown interrupted a request wait."""


@dataclass
class _Request:
    text: str
    deadline: float
    done: bool = False
    abandoned: bool = False
    result: object = None
    error: str = ''


class LlmRuntime:
    """Warm and recover independently of STT; never publish from this worker."""

    def __init__(self, config, log, stop_event):
        self.config = config
        self.log = log
        self.stop_event = stop_event
        self._condition = threading.Condition()
        self._closed = False
        self._ready = False
        self._ready_since = float('inf')
        self._request = None
        self.worker = threading.Thread(target=self._run, name='voice_llm', daemon=True)
        self.worker.start()

    def _stopped(self):
        return self._closed or self.stop_event.is_set()

    def _emit(self, message):
        with self._condition:
            if not self._stopped():
                self.log(message)

    def close(self):
        """Invalidate pending results without waiting for a blocked HTTP socket."""
        with self._condition:
            self._closed = True
            self._ready = False
            self._condition.notify_all()

    def drop_reason(self, utterance_start):
        """Reject speech recorded before the latest successful warmup."""
        with self._condition:
            if self._stopped() or not self._ready:
                return 'llm_not_ready'
            if utterance_start <= self._ready_since:
                return 'llm_before_ready'
            if self._request is not None:
                return 'llm_busy'
            return ''

    def classify(self, text, utterance_start, deadline):
        """Wait only to the request/utterance deadline; discard every late result."""
        with self._condition:
            if self._stopped():
                raise LlmStopped()
            reason = self.drop_reason(utterance_start)
            if reason:
                raise LlmUnavailable(reason)
            request = _Request(text, min(
                deadline, time.perf_counter() + self.config.classifier.timeout_seconds
            ))
            if request.deadline <= time.perf_counter():
                raise LlmUnavailable('expired')
            self._request = request
            self._condition.notify_all()
            while not request.done:
                if self._stopped():
                    request.abandoned = True
                    raise LlmStopped()
                remaining = request.deadline - time.perf_counter()
                if remaining <= 0:
                    request.abandoned = True
                    self._ready = False
                    raise LlmUnavailable('llm_timeout')
                self._condition.wait(timeout=min(0.05, remaining))
            if self._stopped():
                raise LlmStopped()
            if request.error:
                raise LlmUnavailable(request.error)
            return request.result

    def _warmup(self):
        self._emit('[LLM WARMUP] ' + self.config.llm_preparing_message)
        started = time.perf_counter()
        try:
            # Use precisely the production prompt/schema/options; discard its command.
            _, metrics = classify_command_with_metrics('콜라 가져다줘', replace(
                self.config.classifier, timeout_seconds=self.config.llm_warmup_timeout_seconds
            ))
            if time.perf_counter() - started > self.config.llm_warmup_timeout_seconds:
                raise TimeoutError('warmup deadline exceeded')
        except Exception as exc:
            self._emit(f'[LLM ERROR] warmup {type(exc).__name__}: {exc}')
            return
        with self._condition:
            if self._stopped():
                return
            self._ready_since = time.perf_counter()
            self._ready = True
            self._emit(
                '[LLM READY] ' + self.config.llm_ready_message + ' '
                + ' '.join(f'{key}={value:.3f}s' for key, value in metrics.items())
            )
            self._condition.notify_all()

    def _execute(self, request):
        result, error = None, ''
        try:
            remaining = request.deadline - time.perf_counter()
            if remaining <= 0 or request.abandoned:
                error = 'llm_timeout'
            else:
                result = classify_command_with_metrics(request.text, replace(
                    self.config.classifier, timeout_seconds=remaining
                ))
        except Exception as exc:
            error = 'llm_error'
            self._emit(f'[LLM ERROR] request {type(exc).__name__}: {exc}')
        with self._condition:
            if request.abandoned or time.perf_counter() > request.deadline:
                error = 'llm_timeout'
            if self._stopped():
                error = 'llm_stopped'
            if error:
                self._ready = False
            request.result = result if not error else None
            request.error = error
            request.done = True
            self._request = None
            self._condition.notify_all()

    def _retry_pause(self):
        self._emit(f'[LLM RETRY] after={self.config.llm_retry_seconds:.1f}s')
        deadline = time.perf_counter() + self.config.llm_retry_seconds
        with self._condition:
            while not self._stopped():
                remaining = deadline - time.perf_counter()
                if remaining <= 0:
                    return
                self._condition.wait(timeout=min(0.1, remaining))

    def _run(self):
        # Exactly one worker owns client HTTP calls. Server work may outlive a
        # disconnected socket; late results never have a publication path.
        while not self._stopped():
            with self._condition:
                if self._stopped():
                    return
                request = self._request
                ready = self._ready
            if request is not None:
                self._execute(request)
                if not self._ready:
                    self._retry_pause()
            elif not ready:
                self._warmup()
                if not self._ready:
                    self._retry_pause()
            else:
                with self._condition:
                    self._condition.wait_for(
                        lambda: self._request is not None or self._stopped(), timeout=0.1
                    )
