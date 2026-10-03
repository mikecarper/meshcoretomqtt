"""Serial getter parsing exercised through a real local pseudo terminal."""
from __future__ import annotations

import json
import os
import time

import pytest
import serial

from bridge.serial_connection import RealSerialConnection, connect
from bridge.background import _log_device_stats
from tests.fakes import make_test_state
from test_serial_streaming import PtyRadio, queued_lines, wait_until


pytestmark = pytest.mark.skipif(os.name != "posix", reason="requires POSIX PTYs")


@pytest.mark.parametrize("method, reply, expected", [
    ("get_name", "  -> >MyRepeater\r\n", "MyRepeater"),
    ("get_name", "  -> nonsense\r\n", None),
    ("get_name", "  -> >  TestNode \r\n", "TestNode"),
    ("get_name", "  -> >NodeA\nNodeB\n", "NodeA"),
    ("get_pubkey", "  -> >" + "aa" * 32 + "\r\n", "AA" * 32),
    ("get_pubkey", "  -> >ABCD\r\n", None),
    ("get_pubkey", "  -> >" + "GG" * 32 + "\r\n", None),
    ("get_pubkey", "  -> >" + "ab" * 32 + "\r\n", "AB" * 32),
    ("get_privkey", "  -> >" + "cc" * 64 + "\r\n", "cc" * 64),
    ("get_privkey", "  -> >AABB\r\n", None),
    ("get_privkey", "  -> >" + "ZZ" * 64 + "\r\n", None),
    ("get_radio_info", "  -> >LoRa 915MHz SF10 BW250\r\n", "LoRa 915MHz SF10 BW250"),
    ("get_radio_info", "  -> unsupported\r\n", None),
    ("get_firmware_version", "  -> 1.8.2-dev-834c700 (Build: 04-Sep-2025)\n",
     "1.8.2-dev-834c700 (Build: 04-Sep-2025)"),
    ("get_firmware_version", "", None),
    ("get_board_type", "  -> Station G2\r\n", "Station G2"),
    ("get_board_type", "  -> Unknown command\r\n", "unknown"),
    ("get_board_type", "", None),
])
def test_getter_parsing(method, reply, expected):
    def respond(command):
        return (command + "\r\n" + reply).encode()

    with PtyRadio(respond) as radio:
        assert getattr(radio.connection, method)() == expected


@pytest.mark.parametrize("core, radio_stats, packets, expected", [
    ('{"battery_mv":4200,"uptime_secs":3600,"errors":0,"queue_len":5}',
     "Unknown command", "Unknown command",
     {"battery_mv": 4200, "uptime_secs": 3600, "debug_flags": 0, "queue_len": 5}),
    ("Unknown command", '{"noise_floor":-100,"tx_air_secs":10,"rx_air_secs":20}',
     "Unknown command", {"noise_floor": -100, "tx_air_secs": 10, "rx_air_secs": 20}),
    ("Unknown command", "Unknown command", '{"recv_errors":42}', {"recv_errors": 42}),
    ('{"battery_mv":3800}', "Unknown command", "Unknown command", {"battery_mv": 3800}),
    ("{invalid json}", '{"noise_floor":-90}', "Unknown command", {"noise_floor": -90}),
])
def test_device_stats_parsing(core, radio_stats, packets, expected):
    replies = dict(zip(("stats-core", "stats-radio", "stats-packets"),
                       (core, radio_stats, packets)))
    with PtyRadio(lambda command: f"{command}\r\n  -> {replies[command]}\r\n".encode()) as radio:
        assert radio.connection.get_device_stats() == expected
        assert radio.commands == ["stats-core", "stats-radio", "stats-packets"]


@pytest.mark.parametrize("command, ignored_field", [
    ("stats-core", "battery_mv"),
    ("stats-radio", "noise_floor"),
    ("stats-packets", "recv_errors"),
])
@pytest.mark.parametrize("invalid_root", [
    "null", "42", "1.5", "true", "false", '"plain text"',
    '"battery_mv noise_floor recv_errors"', "[]",
    '["battery_mv", "noise_floor", "recv_errors"]',
])
def test_non_object_stats_reply_is_ignored_without_losing_other_stats(command, ignored_field, invalid_root):
    replies = {
        "stats-core": '{"battery_mv":4200}',
        "stats-radio": '{"noise_floor":-105}',
        "stats-packets": '{"recv_errors":3}',
    }
    replies[command] = invalid_root
    expected = {"battery_mv": 4200, "noise_floor": -105, "recv_errors": 3}
    del expected[ignored_field]

    with PtyRadio(lambda request: f"{request}\r\n  -> {replies[request]}\r\n".encode()) as radio:
        assert radio.connection.get_device_stats() == expected
        assert radio.commands == ["stats-core", "stats-radio", "stats-packets"]
        assert radio.connection.is_open


STATS_FIELDS = (
    ("stats-core", "battery_mv", "battery_mv"),
    ("stats-core", "uptime_secs", "uptime_secs"),
    ("stats-core", "errors", "debug_flags"),
    ("stats-core", "queue_len", "queue_len"),
    ("stats-radio", "noise_floor", "noise_floor"),
    ("stats-radio", "tx_air_secs", "tx_air_secs"),
    ("stats-radio", "rx_air_secs", "rx_air_secs"),
    ("stats-packets", "recv_errors", "recv_errors"),
)


def numeric_stats_replies():
    return {
        "stats-core": {"battery_mv": 4200, "uptime_secs": 3600,
                       "errors": 0, "queue_len": 2},
        "stats-radio": {"noise_floor": -105, "tx_air_secs": 2.5,
                        "rx_air_secs": 5},
        "stats-packets": {"recv_errors": 3},
    }


@pytest.mark.parametrize("command, field, output_field", STATS_FIELDS)
@pytest.mark.parametrize("invalid_value", [
    None, "12", [], {}, True, False,
    float("nan"), float("inf"), float("-inf"), 10 ** 400,
])
def test_malformed_numeric_stat_is_ignored_and_formatter_stays_safe(command, field, output_field, invalid_value):
    replies = numeric_stats_replies()
    expected = {output: replies[cmd][source] for cmd, source, output in STATS_FIELDS}
    replies[command][field] = invalid_value
    del expected[output_field]

    with PtyRadio(lambda request: f"{request}\r\n  -> {json.dumps(replies[request])}\r\n".encode()) as radio:
        stats = radio.connection.get_device_stats()
        assert stats == expected
        assert radio.connection.is_open
        state = make_test_state()
        state.stats["device"] = stats
        state.stats["device_prev"] = stats.copy()
        _log_device_stats(state, 300)


@pytest.mark.parametrize("command, field, output_field", [
    fields for fields in STATS_FIELDS if fields[1] != "noise_floor"
])
def test_negative_counter_stat_is_ignored(command, field, output_field):
    replies = numeric_stats_replies()
    replies[command][field] = -1
    with PtyRadio(lambda request: f"{request}\r\n  -> {json.dumps(replies[request])}\r\n".encode()) as radio:
        stats = radio.connection.get_device_stats()
        assert output_field not in stats
        assert stats["noise_floor"] == -105


def test_valid_integer_and_float_stats_are_retained_without_coercion():
    replies = numeric_stats_replies()
    replies["stats-core"]["battery_mv"] = 4200
    replies["stats-core"]["queue_len"] = 2.0
    replies["stats-radio"]["noise_floor"] = -105.5
    with PtyRadio(lambda request: f"{request}\r\n  -> {json.dumps(replies[request])}\r\n".encode()) as radio:
        stats = radio.connection.get_device_stats()
        assert stats == {output: replies[cmd][source] for cmd, source, output in STATS_FIELDS}
        assert type(stats["battery_mv"]) is int
        assert type(stats["queue_len"]) is float
        assert type(stats["noise_floor"]) is float
        state = make_test_state()
        state.stats["device"] = stats
        state.stats["device_prev"] = {}
        _log_device_stats(state, 300)
        assert radio.connection.is_open


@pytest.mark.parametrize("command, reply, expected", [
    ("ver", "1.8.2", "1.8.2"),
    ("get name", ">TestNode", "TestNode"),
    ("get name", "> value ending >", "value ending >"),
])
def test_execute_command_strips_only_cli_prefix(command, reply, expected):
    with PtyRadio({command: f"{command}\r\n  -> {reply}\r\n> ".encode()}) as radio:
        assert radio.connection.execute_command(command) == (True, expected)


def test_read_line_is_nonblocking_and_activity_tracks_receive_not_consumption():
    with PtyRadio() as radio:
        started = time.monotonic()
        assert radio.connection.read_line() is None
        assert time.monotonic() - started < 0.05
        radio.connection._last_activity = time.monotonic() - 100
        assert radio.connection.seconds_since_activity() >= 99
        radio.send(b"test line\n")
        wait_until(lambda: bool(radio.connection._lines))
        assert radio.connection.seconds_since_activity() < 1
        assert queued_lines(radio.connection) == ["test line"]


def test_stats_responses_are_activity_even_before_log_consumer_reads():
    with PtyRadio(lambda command: f'{command}\n  -> {{"battery_mv":4200}}\n'.encode()) as radio:
        radio.connection._last_activity = time.monotonic() - 1000
        assert radio.connection.get_device_stats() == {"battery_mv": 4200}
        assert radio.connection.seconds_since_activity() < 1


def test_no_received_bytes_do_not_reset_activity():
    with PtyRadio({}) as radio:
        radio.connection._last_activity = time.monotonic() - 1000
        assert radio.connection.get_device_stats() == {}
        assert radio.connection.seconds_since_activity() >= 999


def test_close_is_idempotent_even_with_initially_closed_port():
    port = serial.serial_for_url("loop://", do_not_open=True)
    connection = RealSerialConnection(port)
    connection.close()
    connection.close()
    assert not connection.is_open
    assert not connection._reader.is_alive()


def test_connect_returns_none_when_all_ports_fail():
    assert connect({"serial": {"ports": ["/dev/nonexistent1", "/dev/nonexistent2"],
                               "baud_rate": 115200, "timeout": 2}}) is None
