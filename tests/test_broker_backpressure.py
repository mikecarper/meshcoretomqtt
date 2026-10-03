"""Bounded outbound publishing with real loopback MQTT and production budgets."""
from __future__ import annotations

import socket
import struct
import threading
import time

import pytest

from bridge.broker_client import PahoBrokerClient, _PendingPublishes


class LocalMqttReceiver:
    """Small real MQTT 3.1.1 receiver; can withhold reads or acknowledgments."""

    def __init__(self, *, read_packets: bool = True, acknowledge: bool = True) -> None:
        self.read_packets = read_packets
        self.acknowledge = acknowledge
        self.stopped = threading.Event()
        self.listener = socket.socket()
        self.listener.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
        self.listener.bind(('127.0.0.1', 0))
        self.listener.listen(1)
        self.port = self.listener.getsockname()[1]
        self.connection: socket.socket | None = None
        self.packets = 0
        self.errors: list[Exception] = []
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _receive(self, size: int) -> bytes:
        data = bytearray()
        while len(data) < size and not self.stopped.is_set():
            try:
                chunk = self.connection.recv(size - len(data))
            except socket.timeout:
                continue
            if not chunk:
                raise EOFError
            data.extend(chunk)
        if len(data) != size:
            raise EOFError
        return bytes(data)

    def _packet(self) -> tuple[int, bytes]:
        header = self._receive(1)[0]
        remaining = 0
        multiplier = 1
        for _ in range(4):
            digit = self._receive(1)[0]
            remaining += (digit & 127) * multiplier
            if not digit & 128:
                return header, self._receive(remaining)
            multiplier *= 128
        raise ValueError('Invalid MQTT remaining length')

    def _run(self) -> None:
        try:
            self.connection, _ = self.listener.accept()
            self.connection.settimeout(0.1)
            header, _ = self._packet()
            assert header == 0x10  # CONNECT
            self.connection.sendall(b'\x20\x02\x00\x00')
            if not self.read_packets:
                self.stopped.wait(10)
                return
            while not self.stopped.is_set():
                header, data = self._packet()
                command = header & 0xF0
                if command == 0x30:
                    self.packets += 1
                    qos = (header >> 1) & 3
                    if qos and self.acknowledge:
                        topic_length = struct.unpack('!H', data[:2])[0]
                        mid = data[2 + topic_length:4 + topic_length]
                        prefix = b'\x40\x02' if qos == 1 else b'\x50\x02'
                        self.connection.sendall(prefix + mid)
                elif command == 0x60 and self.acknowledge:
                    self.connection.sendall(b'\x70\x02' + data[:2])
                elif command == 0xC0:
                    self.connection.sendall(b'\xD0\x00')
                elif command == 0xE0:
                    return
        except (OSError, EOFError):
            pass
        except Exception as exc:
            self.errors.append(exc)

    def close(self) -> None:
        self.stopped.set()
        if self.connection is not None:
            try:
                self.connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            self.connection.close()
        self.listener.close()
        self.thread.join(timeout=2)
        assert not self.thread.is_alive()
        assert not self.errors


def connect_client(receiver: LocalMqttReceiver, *, threaded=False, **kwargs) -> PahoBrokerClient:
    client = PahoBrokerClient('bounded-test', **kwargs)
    client.connect('127.0.0.1', receiver.port)
    if threaded:
        client.loop_start()
    deadline = time.monotonic() + 2
    while not client.is_connected and time.monotonic() < deadline:
        if threaded:
            time.sleep(0.005)
        else:
            client.raw_client.loop(timeout=0.05)
    assert client.is_connected
    return client


def close_client(client: PahoBrokerClient, receiver: LocalMqttReceiver) -> None:
    client.disconnect()
    receiver.close()
    client.loop_stop()


class TestPublishBudget:
    def test_message_and_byte_limits_include_reservations(self):
        budget = _PendingPublishes(2, 100, 1)
        first = budget.reserve(60)
        assert first is not None
        assert budget.reserve(41) is None
        second = budget.reserve(40)
        assert second is not None
        assert budget.reserve(0) is None
        assert budget.snapshot()['pending_messages'] == 2
        assert budget.snapshot()['pending_bytes'] == 100
        budget.finish(first, False)
        budget.finish(second, False)
        assert budget.snapshot()['pending_bytes'] == 0

    def test_completion_before_publish_return_does_not_lose_credit(self):
        budget = _PendingPublishes(1, 100, 1)
        reservation = budget.reserve(50)
        completion = threading.Event()
        completion.set()
        budget.complete()  # Real callbacks may run before finish has a receipt.
        budget.finish(reservation, True, completion.is_set)
        assert budget.snapshot() == {
            'pending_messages': 0, 'pending_bytes': 0,
            'accepted': 1, 'completed': 1, 'rejected': 0, 'errors': 0,
        }

    def test_completion_set_after_callback_is_reconciled(self):
        budget = _PendingPublishes(1, 100, 1)
        reservation = budget.reserve(50)
        completion = threading.Event()
        budget.finish(reservation, True, completion.is_set)
        budget.complete()  # Paho marks the public receipt after this callback.
        assert budget.snapshot()['pending_messages'] == 1
        completion.set()
        assert budget.reserve(100) is not None
        assert budget.snapshot()['completed'] == 1

    def test_receipts_not_mid_order_control_credit(self):
        budget = _PendingPublishes(2, 100, 1)
        first = budget.reserve(70)
        second = budget.reserve(30)
        first_completed = threading.Event()
        second_completed = threading.Event()
        budget.finish(first, True, first_completed.is_set)
        budget.finish(second, True, second_completed.is_set)
        second_completed.set()
        budget.complete()
        assert budget.snapshot()['pending_bytes'] == 70
        assert budget.reserve(31) is None
        first_completed.set()
        assert budget.snapshot()['pending_messages'] == 0

    def test_failed_publish_that_may_queue_keeps_its_budget(self):
        budget = _PendingPublishes(1, 100, 0.02)
        reservation = budget.reserve(80)
        budget.finish(reservation, False, may_be_queued=True)
        assert budget.reserve(1) is None
        assert budget.snapshot()['accepted'] == 0
        assert budget.snapshot()['errors'] == 1
        time.sleep(0.03)
        assert budget.stalled

    def test_concurrent_reservations_cannot_exceed_limits(self):
        budget = _PendingPublishes(3, 90, 1)
        ready = threading.Barrier(13)
        reservations = []
        lock = threading.Lock()

        def reserve():
            ready.wait()
            result = budget.reserve(30)
            if result is not None:
                with lock:
                    reservations.append(result)

        threads = [threading.Thread(target=reserve) for _ in range(12)]
        for thread in threads:
            thread.start()
        ready.wait()
        for thread in threads:
            thread.join(timeout=1)
            assert not thread.is_alive()
        assert len(reservations) == 3
        assert budget.snapshot()['pending_bytes'] == 90


class TestPahoBoundedPublishing:
    @pytest.mark.parametrize('kwargs', [
        {'max_pending_messages': 0}, {'max_pending_messages': True},
        {'max_pending_messages': 65536}, {'max_pending_bytes': 0},
        {'max_pending_bytes': 1.5}, {'connect_timeout': 0},
        {'connect_timeout': float('nan')}, {'publish_timeout': float('inf')},
    ])
    def test_rejects_unbounded_or_invalid_configuration(self, kwargs):
        with pytest.raises(ValueError):
            PahoBrokerClient('invalid-test', **kwargs)

    def test_disconnected_publish_does_not_queue(self):
        client = PahoBrokerClient('offline-test')
        assert not client.is_connected
        for _ in range(1000):
            assert not client.publish('test', 'payload', qos=2)
        assert client.pending_messages == 0
        assert client.pending_bytes == 0
        assert client.publish_stats['rejected'] == 1000

    def test_qos_zero_synchronous_callback_releases_single_credit(self):
        receiver = LocalMqttReceiver()
        client = connect_client(receiver, max_pending_messages=1, max_pending_bytes=128)
        try:
            # No loop thread: real Paho invokes on_publish within publish().
            for _ in range(200):
                assert client.publish('test', 'payload')
            assert client.pending_messages == 0
            assert client.publish_stats['accepted'] == 200
            assert client.publish_stats['completed'] == 200
        finally:
            close_client(client, receiver)

    @pytest.mark.parametrize('qos', [1, 2])
    def test_unacknowledged_messages_stop_at_cap_and_expire(self, qos):
        receiver = LocalMqttReceiver(acknowledge=False)
        client = connect_client(
            receiver, max_pending_messages=2, max_pending_bytes=128,
            publish_timeout=0.03, threaded=True,
        )
        try:
            assert client.publish('test', 'payload', qos=qos)
            assert client.publish('test', 'payload', qos=qos)
            for _ in range(50):
                assert not client.publish('test', 'payload', qos=qos)
            assert client.pending_messages == 2
            assert client.pending_bytes == 2 * (4 + 7 + 32)
            assert client.publish_stats['accepted'] == 2
            assert client.publish_stats['completed'] == 0
            time.sleep(0.04)
            assert client.publish_stalled
        finally:
            close_client(client, receiver)

    @pytest.mark.parametrize('qos', [1, 2])
    def test_acknowledgments_return_credit(self, qos):
        receiver = LocalMqttReceiver()
        client = connect_client(receiver, max_pending_messages=1, threaded=True)
        try:
            assert client.publish('test', 'payload', qos=qos)
            deadline = time.monotonic() + 2
            while client.pending_messages and time.monotonic() < deadline:
                time.sleep(0.005)
            assert client.pending_messages == 0
            assert client.publish_stats['completed'] == 1
            assert client.publish('test', 'second', qos=qos)
        finally:
            close_client(client, receiver)

    def test_nonreading_peer_bounds_qos_zero_queue(self):
        receiver = LocalMqttReceiver(read_packets=False)
        client = connect_client(
            receiver, max_pending_messages=2, max_pending_bytes=70000,
            publish_timeout=0.05, threaded=True,
        )
        client.raw_client.socket().setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4096)
        payload = 'X' * 32768
        try:
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                client.publish('test', payload)
                if client.publish_stalled:
                    break
                time.sleep(0.001)
            # A real peer that does not drain its TCP window leaves QoS 0
            # outstanding beyond the deadline, not just for a burst callback.
            assert client.publish_stalled
            for _ in range(100):
                client.publish('test', payload)
                assert client.pending_messages <= 2
                assert client.pending_bytes <= 70000
            assert client.pending_messages > 0
        finally:
            close_client(client, receiver)

    def test_connection_loss_refuses_offline_qos_two_queue(self):
        receiver = LocalMqttReceiver()
        client = connect_client(receiver, threaded=True)
        try:
            receiver.close()
            deadline = time.monotonic() + 2
            while client.is_connected and time.monotonic() < deadline:
                time.sleep(0.005)
            assert not client.is_connected
            for _ in range(100):
                assert not client.publish('test', 'payload', qos=2)
            assert client.pending_messages == 0
        finally:
            client.disconnect()
            client.loop_stop()

    def test_utf8_byte_limit_and_publish_error_release(self):
        receiver = LocalMqttReceiver()
        client = connect_client(receiver, max_pending_messages=1, max_pending_bytes=40)
        try:
            assert not client.publish('test', '\u00e9' * 3)  # 4 + 6 + 32 exceeds 40.
            assert client.pending_messages == 0
            with pytest.raises(ValueError):
                client.publish('#', 'x')
            assert client.pending_messages == 0
            assert client.publish('test', 'ok')
        finally:
            close_client(client, receiver)
