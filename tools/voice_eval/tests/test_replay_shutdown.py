"""Do not report successful offline completion while native model workers remain."""

import sys
import threading
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from replay_audio_stop_pipeline import join_replay_workers  # noqa: E402


def test_replay_waits_for_model_cleanup():
    done = threading.Event()
    worker = threading.Thread(target=done.wait, name='native_cleanup')
    worker.start()
    instance = SimpleNamespace(capture_worker=None, speaker=SimpleNamespace(worker=worker))
    try:
        assert join_replay_workers(instance, [], timeout=.01) == ['native_cleanup']
        done.set()
        assert join_replay_workers(instance, [], timeout=1) == []
        assert not worker.is_alive()
    finally:
        done.set()
        worker.join(timeout=1)


def test_replay_handles_disabled_models_and_duplicate_workers():
    worker = threading.Thread(target=lambda: None)
    worker.start()
    instance = SimpleNamespace(capture_worker=worker, speaker=SimpleNamespace(worker=None))
    assert join_replay_workers(instance, [worker], timeout=1) == []
    assert join_replay_workers(None, [], timeout=0) == []
