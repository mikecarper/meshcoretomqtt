"""Real TCP, TLS and WebSocket retirement under outbound backpressure."""
from __future__ import annotations

import base64
import hashlib
import shutil
import socket
import struct
import subprocess
import threading
import time

import pytest

from bridge.broker_client import PahoBrokerClient
from bridge.mqtt_manager import MqttManager
from tests.fakes import make_config, make_test_state
from tests.test_broker_backpressure import LocalMqttReceiver


class WebSocketStream:
    """Actual server-side binary frames over a real socket, not a Paho mock."""

    def __init__(self, connection: socket.socket) -> None:
        self.connection = connection
        self.pending = bytearray()

    def _exact(self, size):
        data = bytearray()
        while len(data) < size:
            part = self.connection.recv(size - len(data))
            if not part:
                raise EOFError
            data.extend(part)
        return bytes(data)

    def recv(self, size):
        while not self.pending:
            header = self._exact(2)
            length = header[1] & 127
            if length == 126:
                length = struct.unpack('!H', self._exact(2))[0]
            elif length == 127:
                length = struct.unpack('!Q', self._exact(8))[0]
            if length > 65536:
                raise ValueError('Oversized test WebSocket frame')
            assert header[1] & 128, 'Client frames must be masked'
            mask = self._exact(4)
            payload = self._exact(length)
            decoded = bytes(value ^ mask[index % 4] for index, value in enumerate(payload))
            if header[0] & 15 == 8:
                raise EOFError
            assert header[0] & 15 == 2, 'Expected binary MQTT frame'
            self.pending.extend(decoded)
        result = bytes(self.pending[:size])
        del self.pending[:size]
        return result

    def sendall(self, data):
        assert len(data) < 126
        self.connection.sendall(bytes((0x82, len(data))) + data)

    def shutdown(self, how):
        self.connection.shutdown(how)

    def close(self):
        self.connection.close()


class TransportReceiver(LocalMqttReceiver):
    def __init__(self, transport, tls_context=None):
        self.transport = transport
        self.tls_context = tls_context
        super().__init__(read_packets=False)

    def _run(self):
        try:
            connection, _ = self.listener.accept()
            self.connection = connection
            connection.settimeout(1)
            if self.transport == 'tls':
                connection = self.tls_context.wrap_socket(connection, server_side=True)
                self.connection = connection
            if self.transport == 'websockets':
                request = bytearray()
                while not request.endswith(b'\r\n\r\n'):
                    part = connection.recv(1)
                    if not part:
                        raise EOFError
                    request.extend(part)
                    if len(request) > 4096:
                        raise ValueError('Oversized test WebSocket upgrade')
                key_line = next(
                    line for line in bytes(request).split(b'\r\n')
                    if line.lower().startswith(b'sec-websocket-key:')
                )
                key = key_line.split(b':', 1)[1].strip()
                accept = base64.b64encode(hashlib.sha1(
                    key + b'258EAFA5-E914-47DA-95CA-C5AB0DC85B11'
                ).digest())
                connection.sendall(
                    b'HTTP/1.1 101 Switching Protocols\r\n'
                    b'Upgrade: websocket\r\nConnection: Upgrade\r\n'
                    b'Sec-WebSocket-Accept: ' + accept + b'\r\n\r\n'
                )
                self.connection = WebSocketStream(connection)
            header, _ = self._packet()
            assert header == 0x10
            self.connection.sendall(b'\x20\x02\x00\x00')
            self.stopped.wait(10)
        except (OSError, EOFError):
            pass
        except Exception as exc:
            self.errors.append(exc)


class ReplacementReceiver:
    """Accept replacement transports while previous peers remain nonreading."""

    def __init__(self):
        self.stopped = threading.Event()
        self.listener = socket.socket()
        self.listener.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
        self.listener.bind(('127.0.0.1', 0))
        self.listener.listen(2)
        self.listener.settimeout(0.1)
        self.port = self.listener.getsockname()[1]
        self.peers = []
        self.workers = []
        self.errors = []
        self.thread = threading.Thread(target=self._accept, daemon=True)
        self.thread.start()

    def _accept(self):
        try:
            while not self.stopped.is_set():
                try:
                    connection, _ = self.listener.accept()
                except socket.timeout:
                    continue
                connection.settimeout(1)
                self.peers.append(connection)
                if len(self.peers) > 4:
                    raise ValueError('Unexpected repeated transports in bounded test')
                worker = threading.Thread(target=self._serve, args=(connection,), daemon=True)
                self.workers.append(worker)
                worker.start()
        except OSError:
            pass
        except Exception as exc:
            self.errors.append(exc)

    def _serve(self, connection):
        def exact(size):
            data = bytearray()
            while len(data) < size:
                part = connection.recv(size - len(data))
                if not part:
                    raise EOFError
                data.extend(part)
            return bytes(data)

        try:
            assert exact(1) == b'\x10'
            remaining = 0
            multiplier = 1
            for _ in range(4):
                digit = exact(1)[0]
                remaining += (digit & 127) * multiplier
                if not digit & 128:
                    break
                multiplier *= 128
            assert remaining < 4096
            exact(remaining)
            connection.sendall(b'\x20\x02\x00\x00')
            self.stopped.wait(10)
        except (OSError, EOFError):
            pass
        except Exception as exc:
            self.errors.append(exc)

    def close(self):
        self.stopped.set()
        self.listener.close()
        self.thread.join(timeout=2)
        assert not self.thread.is_alive()
        for peer in self.peers:
            try:
                peer.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            peer.close()
        for worker in self.workers:
            worker.join(timeout=2)
            assert not worker.is_alive()
        assert not self.errors


@pytest.fixture
def tls_context(tmp_path):
    import ssl

    openssl = shutil.which('openssl')
    if openssl is None:
        pytest.skip('OpenSSL executable unavailable for ephemeral TLS test certificate')
    certificate = tmp_path / 'cert.pem'
    key = tmp_path / 'key.pem'
    subprocess.run([
        openssl, 'req', '-x509', '-newkey', 'rsa:2048', '-nodes',
        '-keyout', str(key), '-out', str(certificate), '-days', '1',
        '-subj', '/CN=localhost',
    ], check=True, capture_output=True, timeout=10)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certificate, key)
    return context


def wait_until(predicate, timeout=3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.005)
    assert predicate(), 'Timed out waiting for real MQTT lifecycle progress'


def fill_nonreading_transport(client):
    transport = client.raw_client.socket()
    # Use only public file/socket APIs, including Paho's SocketLike wrapper.
    tuner = socket.socket(fileno=transport.fileno())
    try:
        tuner.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4096)
    finally:
        tuner.detach()  # The real Paho transport remains the descriptor owner.
    payload = 'X' * 32768
    deadline = time.monotonic() + 2
    while not client.publish_stalled and time.monotonic() < deadline:
        client.publish('test', payload)
        time.sleep(0.001)
    assert client.publish_stalled
    assert client.pending_messages <= 2
    assert client.pending_bytes <= 70000


@pytest.mark.parametrize('transport', ['tcp', 'tls', 'websockets'])
def test_transport_abort_releases_nonreading_peer_without_waiting_for_keepalive(
    transport, request,
):
    context = request.getfixturevalue('tls_context') if transport == 'tls' else None
    receiver = TransportReceiver(transport, context)
    client = PahoBrokerClient(
        'retirement-test', transport='websockets' if transport == 'websockets' else 'tcp',
        tls_enabled=transport == 'tls', tls_verify=False,
        max_pending_messages=2, max_pending_bytes=70000, publish_timeout=0.04,
    )
    try:
        client.connect('127.0.0.1', receiver.port, keepalive=60)
        client.loop_start()
        wait_until(lambda: client.is_connected)
        fill_nonreading_transport(client)
        started = time.monotonic()
        client.disconnect()
        client.abort()
        client.abort()  # Closing a retired transport is idempotent.
        client.loop_stop()
        assert time.monotonic() - started < 2
        assert not client.is_connected
    finally:
        receiver.close()
        client.disconnect()
        client.abort()
        client.loop_stop()


def test_manager_replaces_stalled_publisher_and_stops_without_peer_assistance():
    receiver = ReplacementReceiver()
    config = make_config()
    config['broker'][0].update(
        server='127.0.0.1', port=receiver.port, keepalive=60,
        publish_timeout=0.04, max_pending_messages=2, max_pending_bytes=70000,
    )
    state = make_test_state(
        config=config, repeater_name='TestNode', repeater_pub_key='AA' * 32,
    )
    manager = MqttManager(state, jitter=lambda low, high: 0)
    state.mqtt_manager = manager
    manager.start()
    try:
        wait_until(lambda: state.mqtt_connected)
        old = state.mqtt_clients[0]['client']
        old_generation = state.mqtt_clients[0]['generation']
        fill_nonreading_transport(old)
        wait_until(lambda: state.mqtt_clients[0]['client'] not in (None, old))
        wait_until(lambda: state.mqtt_clients[0]['connected'])
        info = state.mqtt_clients[0]
        assert info['failed_attempts'] == 1
        assert info['generation'] > old_generation
        assert manager.is_healthy(max_stall=0.5)
        # The old raw client cannot alter the replacement or its counters.
        userdata = {'broker_idx': 0, 'name': 'test-broker', 'generation': old_generation}
        before = (info['generation'], info['failed_attempts'], info['connected'])
        manager.on_mqtt_disconnect(old.raw_client, userdata, None, 7, None)
        manager.on_mqtt_connect(old.raw_client, userdata, None, 0)
        assert (info['generation'], info['failed_attempts'], info['connected']) == before
        started = time.monotonic()
        assert manager.stop(timeout=2)
        assert time.monotonic() - started < 2.1
        assert not manager.is_healthy()
    finally:
        receiver.close()
        manager.stop(timeout=2)
