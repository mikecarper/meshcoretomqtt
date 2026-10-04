"""Exercise real statistics workers and their shutdown lifecycle."""
from __future__ import annotations

import threading
import time

from bridge.background import stats_logging_loop
from bridge.runner import _cleanup
from tests.fakes import FakeSerialConnection, make_test_state


def test_cleanup_wakes_idle_statistics_worker_before_closing_serial():
    device = FakeSerialConnection()
    state = make_test_state(device=device)
    worker = threading.Thread(target=stats_logging_loop, args=(state,), daemon=True)
    worker.start()
    # Let the worker enter its normal five-minute idle wait.
    time.sleep(0.05)
    assert worker.is_alive()

    started = time.monotonic()
    _cleanup(state, worker)

    assert time.monotonic() - started < 1.0
    assert not worker.is_alive()
    assert not device.is_open
