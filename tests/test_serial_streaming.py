"""Real PTY coverage for USB draining, bounded records, and CLI demultiplexing."""
from __future__ import annotations

import os
import select
import threading
import time

import pytest
import serial

from bridge.serial_connection import RealSerialConnection, connect


pytestmark = pytest.mark.skipif(os.name != "posix", reason="requires POSIX PTYs")


def wait_until(predicate, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.005)
    assert predicate(), "condition did not become true before deadline"


class PtyRadio:
    """A real local terminal pair; no external device, broker, or I/O mock."""

    def __init__(self, replies=None, **options):
        import pty

        self.master, slave = pty.openpty()
        self.path = os.ttyname(slave)
        self.port = serial.Serial(self.path, timeout=0.02, write_timeout=0.2,
                                  exclusive=True)
        os.close(slave)
        self.connection = RealSerialConnection(
            self.port, reader_timeout=0.02, command_timeout=0.25, **options)
        self.commands = []
        self.errors = []
        self.stop = threading.Event()
        self.worker = None
        if replies is not None:
            self.worker = threading.Thread(target=self._respond, args=(replies,),
                                           daemon=True)
            self.worker.start()

    def send(self, value: bytes):
        remaining = memoryview(value)
        while remaining:
            count = os.write(self.master, remaining)
            remaining = remaining[count:]

    def _respond(self, replies):
        pending = bytearray()
        try:
            while not self.stop.is_set():
                ready, _, _ = select.select([self.master], [], [], 0.05)
                if not ready:
                    continue
                pending.extend(os.read(self.master, 4096))
                while b"\n" in pending:
                    command, _, tail = pending.partition(b"\n")
                    pending = bytearray(tail)
                    text = command.decode().strip()
                    if not text:
                        continue
                    self.commands.append(text)
                    reply = replies(text) if callable(replies) else replies.get(text, b"")
                    if reply:
                        self.send(reply)
        except OSError as exc:
            if not self.stop.is_set() and self.connection.is_open:
                self.errors.append(exc)

    def close(self):
        self.stop.set()
        self.connection.close()
        if self.worker:
            self.worker.join(timeout=0.2)
        os.close(self.master)
        assert not self.connection._reader.is_alive()
        assert not self.errors

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


def queued_lines(connection):
    result = []
    while (line := connection.read_line()) is not None:
        result.append(line)
    return result


def test_reader_drains_during_absent_consumer_and_bounds_backlog():
    with PtyRadio(max_pending_lines=8) as radio:
        radio.send(b"".join(f"DEBUG: record {i}\r\n".encode() for i in range(2000)))
        wait_until(lambda: radio.connection._lines and
                   radio.connection._lines[-1] == "DEBUG: record 1999")
        assert len(radio.connection._lines) == 8
        assert radio.connection.seconds_since_activity() < 0.5
        assert queued_lines(radio.connection) == ["DROP:1992"] + [
            f"DEBUG: record {i}" for i in range(1992, 2000)]


def test_overlong_missing_newline_is_discarded_once_until_lf():
    with PtyRadio(max_line_bytes=64) as radio:
        radio.send(b"x" * 65536 + b"\nDEBUG: recovered\r\n")
        wait_until(lambda: len(radio.connection._lines) == 2)
        assert queued_lines(radio.connection) == ["DROP:1", "DEBUG: recovered"]


def test_malformed_line_gap_marker_precedes_subsequent_packet_summary():
    with PtyRadio(max_line_bytes=64) as radio:
        radio.send(b"RAW: AABB\n" + b"x" * 128 + b"\nRX, len=2\n")
        wait_until(lambda: len(radio.connection._lines) == 3)
        assert queued_lines(radio.connection) == ["RAW: AABB", "DROP:1", "RX, len=2"]


def test_fragmented_line_deadline_discards_tail_instead_of_publishing_it():
    with PtyRadio(line_timeout=0.06) as radio:
        radio.send(b"DEBUG: unfinished")
        wait_until(lambda: list(radio.connection._lines) == ["DROP:1"])
        radio.send(b" late suffix\nDEBUG: next\n")
        wait_until(lambda: len(radio.connection._lines) == 2)
        assert queued_lines(radio.connection) == ["DROP:1", "DEBUG: next"]


def test_fragmented_crlf_records_are_kept_whole():
    with PtyRadio() as radio:
        for fragment in (b"DEBUG: frag", b"mented\r", b"\nDEBUG: second\r\n"):
            radio.send(fragment)
        wait_until(lambda: len(radio.connection._lines) == 2)
        assert queued_lines(radio.connection) == ["DEBUG: fragmented", "DEBUG: second"]


def test_query_preserves_packet_logs_and_does_not_wait_for_log_consumer():
    raw = "19:00:00 - 3/10/2026 U RAW: 01020304"
    rx = "19:00:00 - 3/10/2026 U: RX, len=4 (type=0, route=F, payload_len=2)"

    def reply(command):
        return f"{raw}\r\n{command}\r\n{rx}\r\n  -> >NodeA\r\n".encode()

    with PtyRadio(reply) as radio:
        assert radio.connection.get_name() == "NodeA"
        assert queued_lines(radio.connection) == [raw, rx]


def test_full_companion_bare_response_and_unframed_prompt():
    with PtyRadio({"get name": b"> get name\r\n  > FullNode\r\n> "}) as radio:
        assert radio.connection.get_name() == "FullNode"
        assert queued_lines(radio.connection) == []


@pytest.mark.parametrize("name", [
    "DEBUGNode", "BLE: relay", "MQTT: relay", "WiFi: relay",
    "RX, len=2", "19:00:00 - 3/10/2026 node", "get name",
])
@pytest.mark.parametrize("echo", [True, False])
def test_indented_companion_getter_value_is_not_classified_as_log(name, echo):
    reply = ("get name\r\n" if echo else "") + f"  > {name}\r\n> "
    with PtyRadio({"get name": reply.encode()}) as radio:
        assert radio.connection.get_name() == name
        assert radio.connection.is_open
        assert queued_lines(radio.connection) == []


@pytest.mark.parametrize("name", ["RAW: relay", "RX, len=2", "TX, len=2"])
def test_legacy_arrow_getter_value_is_not_classified_as_log(name):
    log = "19:00:00 - 3/10/2026 U RAW: AABB"
    reply = f"get name\r\n{log}\r\n  -> >{name}\r\n"
    with PtyRadio({"get name": reply.encode()}) as radio:
        assert radio.connection.get_name() == name
        assert radio.connection.is_open
        assert queued_lines(radio.connection) == [log]


@pytest.mark.parametrize("method, command, value", [
    ("get_name", "get name", "FullNode"),
    ("get_name", "get name", "DEBUGNode"),
    ("get_pubkey", "get public.key", "AA" * 32),
])
def test_fragmented_getter_prefix_survives_multiple_quiet_reader_polls(method, command, value):
    def reply(request):
        radio.send(f"{request}\r\n  > ".encode())
        time.sleep(0.08)  # Four quiet polls; still before the command deadline.
        return f"{value}\r\n> ".encode()

    with PtyRadio(reply) as radio:
        assert getattr(radio.connection, method)() == value
        assert radio.connection.is_open
        assert queued_lines(radio.connection) == []


def test_prompt_prefixed_log_is_demultiplexed_from_bare_command_reply():
    raw = "19:00:00 - 3/10/2026 U RAW: AABB"
    with PtyRadio({"board": f"> {raw}\nboard\n  Station G2\n> ".encode()}) as radio:
        assert radio.connection.get_board_type() == "Station G2"
        assert queued_lines(radio.connection) == [raw]


@pytest.mark.parametrize("log", [
    "DEBUG: synthetic log",
    "19:00:00 - 3/10/2026 U RAW: AABB",
    "19:00:00 - 3/10/2026 U: RX, len=2 (type=0, route=F, payload_len=0)",
    "[USB watchdog] heartbeat",
])
def test_full_companion_reply_completes_when_prompt_and_log_share_line(log):
    with PtyRadio({"get name": f"get name\r\n  > FullNode\r\n> {log}\r\n".encode()}) as radio:
        assert radio.connection.get_name() == "FullNode"
        assert radio.connection.is_open
        assert queued_lines(radio.connection) == [log]


def test_fragmented_prompt_log_preserves_reply_and_following_packet_pair():
    raw = "19:00:00 - 3/10/2026 U RAW: AABB"
    summary = "19:00:00 - 3/10/2026 U: RX, len=2 (type=0, route=F, payload_len=0)"

    def reply(command):
        radio.send(f"{command}\r\n  > FullNode\r\n> 19:00:00 - ".encode())
        wait_until(lambda: bool(radio.connection._response_lines))
        return f"3/10/2026 U RAW: AABB\r\n{summary}\r\n".encode()

    with PtyRadio(reply) as radio:
        assert radio.connection.get_name() == "FullNode"
        assert radio.connection.is_open
        wait_until(lambda: len(radio.connection._lines) == 2)
        assert queued_lines(radio.connection) == [raw, summary]


def test_prompt_prefixed_log_before_first_reply_does_not_complete_command():
    log = "DEBUG: before reply"
    with PtyRadio({"get name": f"> {log}\r\nget name\r\n  > FullNode\r\n> ".encode()}) as radio:
        assert radio.connection.get_name() == "FullNode"
        assert radio.connection.is_open
        assert queued_lines(radio.connection) == [log]


def test_bare_cli_response_starting_with_time_is_not_mistaken_for_log():
    with PtyRadio({"board": b"board\n  19:00:00 test board\n> "}) as radio:
        assert radio.connection.get_board_type() == "19:00:00 test board"
        assert queued_lines(radio.connection) == []


def test_command_timeout_closes_session_so_late_reply_cannot_match_next_query():
    with PtyRadio({}) as radio:
        started = time.monotonic()
        ok, message = radio.connection.execute_command("ver", timeout=0.08)
        assert not ok and "timed out" in message
        assert time.monotonic() - started < 0.5
        assert not radio.connection.is_open
        with pytest.raises(OSError, match="closed"):
            radio.connection.read_line()
        ok, message = radio.connection.execute_command("board", timeout=0.1)
        assert not ok and "closed" in message
        assert radio.commands == ["ver"]


def test_command_deadline_includes_waiting_for_ownership():
    with PtyRadio({}) as radio:
        radio.connection._lock.acquire()
        try:
            started = time.monotonic()
            ok, message = radio.connection.execute_command("ver", timeout=0.05)
            assert not ok and "ownership" in message
            assert time.monotonic() - started < 0.3
            assert radio.commands == []
            assert radio.connection.is_open
        finally:
            radio.connection._lock.release()


def test_close_wakes_command_wait_and_stops_reader_without_command_lock():
    with PtyRadio({}) as radio:
        results = []
        workers = [threading.Thread(
            target=lambda: results.append(radio.connection.execute_command("ver", timeout=10)))
            for _ in range(2)]
        for worker in workers:
            worker.start()
        wait_until(lambda: radio.commands == ["ver"])
        started = time.monotonic()
        radio.connection.close()
        for worker in workers:
            worker.join(timeout=0.5)
            assert not worker.is_alive()
        assert time.monotonic() - started < 0.8
        assert len(results) == 2 and all(not ok for ok, _ in results)
        assert not radio.connection._reader.is_alive()


def test_real_pty_write_backpressure_is_bounded_by_command_deadline():
    # Leave the PTY master unread. Fill its finite receive capacity, then issue
    # a command. Some kernels admit the tiny write after the oversized one
    # times out; either write or reply must hit the bounded command deadline.
    with PtyRadio(write_timeout=0.05) as radio:
        radio.port.write_timeout = 0.05
        with pytest.raises(serial.SerialTimeoutException):
            radio.port.write(b"x" * 1024 * 1024)
        started = time.monotonic()
        ok, message = radio.connection.execute_command("ver", timeout=0.08)
        assert not ok and any(text in message.lower() for text in ("timeout", "timed out"))
        assert time.monotonic() - started < 0.5
        assert not radio.connection.is_open


def test_bare_multiline_response_is_bounded_without_prompt():
    with PtyRadio({"help": b"help\n" + b"  commands available\n" * 6000}) as radio:
        started = time.monotonic()
        ok, message = radio.connection.execute_command("help", timeout=2)
        assert not ok and "65536" in message
        assert time.monotonic() - started < 1
        assert not radio.connection.is_open
        assert radio.connection._response_size == 0


def test_invalid_command_is_not_written_and_does_not_close_healthy_connection():
    with PtyRadio({}) as radio:
        for command in ("", "ver\nboard", "x" * 4096):
            ok, _ = radio.connection.execute_command(command)
            assert not ok
        assert radio.commands == []
        assert radio.connection.is_open


def test_idle_reader_uses_blocking_poll_without_busy_spin():
    with PtyRadio() as radio:
        assert radio.port.timeout == 0.02
        started_cpu = time.process_time()
        time.sleep(0.25)
        assert time.process_time() - started_cpu < 0.1
        assert radio.connection.read_line() is None


def test_real_usb_capture_and_runner_continue_during_blocked_mqtt_connection():
    from bridge.mqtt_manager import MqttManager
    from bridge.runner import _run_main_loop
    from bridge.service_health import ServiceHealth
    from tests.fakes import FakeAuthProvider, make_config, make_test_state
    from test_mqtt_lifecycle import GatedFactory

    raw = "12:34:56 - 1/15/2025 U RAW: AABB0011CCDD"
    summary = "12:34:56 - 1/15/2025 U: RX, len=6 (type=1, route=D, payload_len=2)"
    pair = f"{raw}\n{summary}\n".encode()
    with PtyRadio(max_pending_lines=8) as radio:
        state = make_test_state(
            config=make_config(serial={"watchdog_timeout": 0}),
            device=radio.connection, auth=FakeAuthProvider(),
            repeater_name="TestNode", repeater_pub_key="AA" * 32)
        factory = GatedFactory()
        manager = MqttManager(state, client_factory=factory)
        state.mqtt_manager = manager
        worker = None
        manager.start()
        try:
            assert factory.entered.wait(0.5)
            # MQTT transport setup is blocked and the main consumer has not
            # started. Actual USB receives still progress and remain bounded.
            radio.send(pair * 1000)
            wait_until(lambda: radio.connection._overflow_dropped == 1992)
            assert len(radio.connection._lines) == 8
            assert radio.connection.seconds_since_activity() < 0.5
            worker = threading.Thread(
                target=_run_main_loop,
                args=(state, ServiceHealth(environment={})), daemon=True)
            worker.start()
            wait_until(lambda: state.stats["packets_rx"] >= 4)
            radio.send(pair)
            wait_until(lambda: state.stats["packets_rx"] >= 5)
            assert not state.mqtt_connected
            assert manager._thread.is_alive() and not factory.release.is_set()
            assert radio.connection.is_open
            started = time.monotonic()
            assert not manager.stop(timeout=0.02)
            assert time.monotonic() - started < 0.5
        finally:
            state.should_exit = True
            if worker:
                worker.join(timeout=0.5)
                assert not worker.is_alive()
            factory.release.set()
            assert manager.stop(timeout=0.5)


def test_device_disappearance_is_reported_to_main_loop_and_releases_port():
    radio = PtyRadio()
    try:
        os.close(radio.master)
        radio.master = os.open(os.devnull, os.O_RDONLY)
        wait_until(lambda: not radio.connection.is_open)
        with pytest.raises(OSError):
            radio.connection.read_line()
        wait_until(lambda: not radio.port.is_open)
    finally:
        radio.close()


def test_connect_respects_persistent_path_config_and_exclusive_open(tmp_path):
    with PtyRadio() as radio:
        stable = tmp_path / "radio-by-id"
        stable.symlink_to(radio.path)
        config = {"serial": {"ports": [str(stable)], "timeout": 2,
                             "reader_timeout": 0.02, "write_timeout": 0.3,
                             "command_timeout": 0.2, "line_timeout": 0.5,
                             "max_line_bytes": 128, "max_pending_lines": 4}}
        assert connect(config) is None  # another bridge already owns this port
        radio.connection.close()
        connection = connect(config)
        assert connection is not None
        try:
            assert connection._max_line_bytes == 128
            assert connection._max_pending_lines == 4
            assert connection._port.write_timeout == 0.3
            radio.send(b"DEBUG: connected\n")
            wait_until(lambda: bool(connection._lines))
            assert connection.read_line() == "DEBUG: connected"
        finally:
            connection.close()


@pytest.mark.parametrize("wrong_reply", [
    "  -> >" + "BB" * 32 + "\r\n",
    "  -> >ABCD\r\n",
    "",  # An openable logging-only endpoint does not answer CLI queries.
])
def test_reconnect_scans_past_wrong_or_unverifiable_port(wrong_reply):
    from bridge.runner import _reconnect_device
    from tests.fakes import make_test_state

    with PtyRadio({"get public.key": wrong_reply.encode()}) as wrong, PtyRadio({
        "get public.key": ("  -> >" + "AA" * 32 + "\r\n").encode(),
    }) as right:
        # Keep the real slave terminals present while releasing the fixture's
        # initial exclusive sessions. Otherwise a PTY responder receives EIO
        # between sessions, unlike an attached USB radio.
        hold_wrong = serial.Serial(wrong.path)
        hold_right = serial.Serial(right.path)
        connection = None
        try:
            wrong.connection.close()
            right.connection.close()
            state = make_test_state(config={"serial": {
                "ports": [wrong.path, right.path], "command_timeout": 0.15,
            }}, repeater_pub_key="AA" * 32)
            connection = _reconnect_device(state, connect)
            assert connection is not None
            assert connection._port.port == right.path
            assert wrong.commands == ["get public.key"]
            assert right.commands == ["get public.key", "get public.key"]
            assert connection.is_open
            # Rejected candidate ownership is released before returning.
            probe = serial.Serial(wrong.path, exclusive=True)
            probe.close()
        finally:
            if connection is not None:
                connection.close()
            hold_wrong.close()
            hold_right.close()


def test_identity_aware_factory_normalizes_expected_key_and_releases_mismatch():
    with PtyRadio({"get public.key": ("  -> >" + "AA" * 32 + "\r\n").encode()}) as radio:
        hold = serial.Serial(radio.path)
        connection = None
        try:
            radio.connection.close()
            config = {"serial": {"ports": [radio.path], "command_timeout": 0.15}}
            assert connect(config, expected_public_key="BB" * 32) is None
            probe = serial.Serial(radio.path, exclusive=True)
            probe.close()
            connection = connect(config, expected_public_key="aa" * 32)
            assert connection is not None and connection.is_open
        finally:
            if connection is not None:
                connection.close()
            hold.close()


@pytest.mark.parametrize("invalid_key", ["", "AA", "GG" * 32, 42])
def test_identity_aware_factory_rejects_invalid_expected_key_without_opening(invalid_key):
    assert connect({"serial": {"ports": ["/dev/nonexistent"]}},
                   expected_public_key=invalid_key) is None


def test_connect_accepts_legacy_nonblocking_timeout_without_busy_poll():
    with PtyRadio() as radio:
        radio.connection.close()
        connection = connect({"serial": {"ports": [radio.path], "timeout": 0}})
        assert connection is not None
        try:
            assert connection._port.timeout == 0.1
        finally:
            connection.close()


@pytest.mark.parametrize("options", [
    {"reader_timeout": "0.1"}, {"reader_timeout": False},
    {"write_timeout": "2"}, {"line_timeout": []},
    {"max_line_bytes": "4096"},
])
def test_factory_rejects_bad_limits_without_leaking_exclusive_port(options):
    with PtyRadio() as radio:
        radio.connection.close()
        assert connect({"serial": {"ports": [radio.path], **options}}) is None
        probe = serial.Serial(radio.path, exclusive=True)
        probe.close()


@pytest.mark.parametrize("options", [
    {"max_line_bytes": 0}, {"max_pending_lines": 0},
    {"line_timeout": 0}, {"reader_timeout": float("nan")},
    {"write_timeout": float("inf")}, {"command_timeout": -1},
    {"reader_timeout": "0.1"}, {"write_timeout": False},
])
def test_invalid_limits_are_rejected_before_reader_starts(options):
    port = serial.serial_for_url("loop://", timeout=0.1)
    try:
        with pytest.raises(ValueError):
            RealSerialConnection(port, **options)
    finally:
        port.close()
