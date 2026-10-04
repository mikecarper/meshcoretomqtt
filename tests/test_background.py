"""Exercise real statistics workers and their shutdown lifecycle."""
from __future__ import annotations

import threading
import time

import pytest

from bridge.background import _log_device_stats, stats_logging_loop
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


@pytest.mark.parametrize('uptime, previous_errors', [(10, 30), (900, 30), (10, 1)])
def test_device_counter_resets_do_not_report_negative_airtime_or_errors(caplog, uptime,
                                                                      previous_errors):
    state = make_test_state()
    state.stats['device_prev'] = {
        'uptime_secs': 600, 'tx_air_secs': 40, 'rx_air_secs': 20,
        'recv_errors': previous_errors,
    }
    state.stats['device'] = {
        'uptime_secs': uptime, 'tx_air_secs': 2, 'rx_air_secs': 1, 'recv_errors': 2,
    }
    caplog.set_level('INFO', logger='bridge.background')

    _log_device_stats(state, 300)

    assert 'Tx -' not in caplog.text
    assert 'Rx -' not in caplog.text
    assert 'Err/min (5m): 0.4' in caplog.text
    assert '(-' not in caplog.text


def test_device_counter_increases_still_report_interval_deltas(caplog):
    state = make_test_state()
    state.stats['device_prev'] = {
        'uptime_secs': 600, 'tx_air_secs': 40, 'rx_air_secs': 20, 'recv_errors': 30,
    }
    state.stats['device'] = {
        'uptime_secs': 900, 'tx_air_secs': 50, 'rx_air_secs': 25, 'recv_errors': 35,
    }
    caplog.set_level('INFO', logger='bridge.background')

    _log_device_stats(state, 300)

    assert 'Tx 10.0s (3.33%), Rx 5.0s (1.67%)' in caplog.text
    assert 'Err/min (5m): 1.0' in caplog.text
