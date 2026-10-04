"""Remote command regressions using native signatures, PTYs and local MQTT."""
from __future__ import annotations

import inspect
import json
import os
import struct
import sys
import threading
import time

import pytest

from auth_token import base64url_encode
from ed25519_orlp import ed25519_sign
from bridge.auth_provider import MeshCoreAuthProvider
from bridge.remote_serial import cleanup_old_nonces, handle_serial_command, subscribe_serial_commands
from bridge.state import BridgeState
from tests import test_auth_token as token_fixtures
from tests.test_broker_backpressure import LocalMqttReceiver, connect_client, close_client
from tests.test_serial_streaming import PtyRadio, wait_until


PUBLIC_KEY = token_fixtures.TestAuthToken.public_key.upper()
PRIVATE_KEY = token_fixtures.TestAuthToken.private_key
pytestmark = pytest.mark.skipif(os.name != 'posix', reason='requires POSIX PTYs')


def remote_state(device=None, **options):
    state = BridgeState({
        'general': {'iata': 'GLOBAL'},
        'serial': {'max_line_bytes': 4096},
        'remote_serial': {'enabled': True, 'allowed_companions': [PUBLIC_KEY],
                          'command_timeout': 0.5, **options},
        'broker': [{'name': 'first', 'topics': {'iata': 'FIRST'}},
                   {'name': 'second', 'topics': {'iata': 'SECOND'}}],
    })
    state.auth = MeshCoreAuthProvider()
    state.device = device
    state.repeater_pub_key = PUBLIC_KEY
    state.repeater_priv_key = PRIVATE_KEY
    return state


def token_payload(**claims):
    return {'publicKey': PUBLIC_KEY, 'command': 'ver', 'target': PUBLIC_KEY,
            'nonce': 'unique', 'exp': int(time.time()) + 3600, **claims}


def sign_payload(payload):
    """Sign actual wire payloads, including malformed JSON claim types."""
    header = base64url_encode(b'{"alg":"Ed25519","typ":"JWT"}')
    body = base64url_encode(json.dumps(payload, separators=(',', ':')).encode())
    message = f'{header}.{body}'
    signature = ed25519_sign(message.encode(), bytes.fromhex(PUBLIC_KEY),
                             bytes.fromhex(PRIVATE_KEY)).hex().upper()
    return f'{message}.{signature}'


def test_same_signed_command_on_two_workers_executes_once():
    # Trace real verification returns to force both MQTT workers through
    # signature verification before either can claim the nonce. No mock of
    # verification, serial, time, or the nonce lock is involved.
    barrier = threading.Barrier(2)
    errors = []
    with PtyRadio({'ver': b'  -> v1\r\n'}) as radio:
        state = remote_state(radio.connection)
        token = sign_payload(token_payload())

        def trace(frame, event, arg):
            if frame.f_code is MeshCoreAuthProvider.verify_token.__code__ and event == 'return':
                barrier.wait(3)
            return trace

        def worker(broker_idx):
            sys.settrace(trace)
            try:
                handle_serial_command(state, token, broker_idx)
            except Exception as exc:
                errors.append(exc)
            finally:
                sys.settrace(None)

        workers = [threading.Thread(target=worker, args=(index,)) for index in range(2)]
        for worker_thread in workers:
            worker_thread.start()
        for worker_thread in workers:
            worker_thread.join(5)
            assert not worker_thread.is_alive()
        assert errors == []
        assert radio.commands == ['ver']
        assert len(state.remote_serial_nonces) == 1


def test_nonce_survives_ttl_for_entire_signed_token_lifetime():
    with PtyRadio({'ver': b'  -> v1\r\n'}) as radio:
        state = remote_state(radio.connection, nonce_ttl=120)
        payload = token_payload()
        token = sign_payload(payload)
        handle_serial_command(state, token, 0)
        assert state.remote_serial_nonces['unique'] >= payload['exp']
        cleanup_old_nonces(state, now=time.time() + 121)
        assert 'unique' in state.remote_serial_nonces
        handle_serial_command(state, token, 1)
        assert radio.commands == ['ver']
        cleanup_old_nonces(state, now=payload['exp'] + 1)
        assert state.remote_serial_nonces == {}


def test_replay_cache_saturation_never_evicts_a_live_nonce():
    with PtyRadio({'ver': b'  -> v1\r\n'}) as radio:
        state = remote_state(radio.connection, max_pending_nonces=1)
        token = sign_payload(token_payload())
        handle_serial_command(state, token, 0)
        handle_serial_command(state, sign_payload(token_payload(nonce='second')), 1)
        handle_serial_command(state, token, 1)
        assert radio.commands == ['ver']
        assert set(state.remote_serial_nonces) == {'unique'}


def test_nonce_minimum_retention_outlasts_short_token():
    with PtyRadio({'ver': b'  -> v1\r\n'}) as radio:
        state = remote_state(radio.connection, nonce_ttl=120)
        before = time.time()
        handle_serial_command(state, sign_payload(token_payload(exp=int(before) + 30)), 0)
        assert state.remote_serial_nonces['unique'] >= before + 120


@pytest.mark.parametrize('payload', [
    [], None, 'text',
    token_payload(publicKey=42), token_payload(target=[]),
    token_payload(nonce=[]), token_payload(nonce='x' * 257),
    token_payload(command=[]), token_payload(command='ver\nerase'),
    token_payload(command='ver\rerase'), token_payload(command='ver\x00'),
    token_payload(command='x' * 4095), token_payload(command='\u00e9' * 2048),
    token_payload(exp=None), token_payload(exp=True), token_payload(exp='future'),
    token_payload(exp=float('nan')), token_payload(exp=float('inf')),
    token_payload(exp=int(time.time()) - 1),
    {key: value for key, value in token_payload().items() if key != 'exp'},
])
def test_malformed_signed_payload_is_rejected_before_serial_or_nonce(payload):
    with PtyRadio({'ver': b'  -> v1\r\n'}) as radio:
        state = remote_state(radio.connection)
        handle_serial_command(state, sign_payload(payload), 0)
        assert radio.commands == []
        assert state.remote_serial_nonces == {}


def test_invalid_signature_cannot_reserve_a_nonce():
    with PtyRadio({'ver': b'  -> v1\r\n'}) as radio:
        state = remote_state(radio.connection)
        token = sign_payload(token_payload())
        handle_serial_command(state, token.rsplit('.', 1)[0] + '.' + '00' * 64, 0)
        assert state.remote_serial_nonces == {}
        handle_serial_command(state, token, 0)
        assert radio.commands == ['ver']


def test_disconnect_between_device_check_and_execution_uses_captured_session():
    source, first_line = inspect.getsourcelines(handle_serial_command)
    execution_line = first_line + next(index for index, line in enumerate(source)
                                       if 'success, response = device.execute_command(' in line)
    with PtyRadio({'ver': b'  -> v1\r\n'}) as radio:
        state = remote_state(radio.connection)

        def trace(frame, event, arg):
            if (frame.f_code is handle_serial_command.__code__ and event == 'line'
                    and frame.f_lineno == execution_line):
                state.device = None
            return trace

        previous = sys.gettrace()
        sys.settrace(trace)
        try:
            handle_serial_command(state, sign_payload(token_payload()), 0)
        finally:
            sys.settrace(previous)
        assert state.device is None
        assert radio.commands == ['ver']
        assert len(state.remote_serial_nonces) == 1


class SerialResponseReceiver(LocalMqttReceiver):
    """Inspect actual incoming MQTT packets on a real loopback connection."""

    def __init__(self):
        self.received = []
        self.subscriptions = []
        super().__init__()

    def _packet(self):
        header, data = super()._packet()
        if header & 0xF0 == 0x30:
            size = struct.unpack('!H', data[:2])[0]
            topic = data[2:2 + size].decode()
            offset = 2 + size + (2 if (header >> 1) & 3 else 0)
            self.received.append((topic, data[offset:].decode()))
        elif header & 0xF0 == 0x80:
            size = struct.unpack('!H', data[2:4])[0]
            self.subscriptions.append(data[4:4 + size].decode())
        return header, data


@pytest.mark.parametrize('source_idx', [0, 1])
def test_signed_response_and_subscription_use_only_source_broker_and_iata(source_idx):
    receivers = [SerialResponseReceiver(), SerialResponseReceiver()]
    clients = []
    try:
        for receiver in receivers:
            clients.append(connect_client(receiver, threaded=True))
        with PtyRadio({'ver': b'  -> v1\r\n'}) as radio:
            state = remote_state(radio.connection)
            state.mqtt_connected = True
            state.mqtt_clients = [{'broker_idx': index, 'client': client, 'connected': True}
                                  for index, client in enumerate(clients)]
            subscribe_serial_commands(state, clients[source_idx], source_idx)
            expected_iata = ('FIRST', 'SECOND')[source_idx]
            wait_until(lambda: receivers[source_idx].subscriptions)
            assert receivers[source_idx].subscriptions == [
                f'meshcore/{expected_iata}/{PUBLIC_KEY}/serial/commands']
            handle_serial_command(state, sign_payload(token_payload()), source_idx)
            wait_until(lambda: receivers[source_idx].received)
            # A TCP round trip on the other connection is a delivery barrier;
            # this avoids assuming an arbitrary sleep proves no broadcast.
            assert clients[1 - source_idx].publish_confirmed('barrier', '{}', qos=1, timeout=1)
            responses = [items for items in receivers[source_idx].received
                         if items[0].endswith('/serial/responses')]
            assert len(responses) == 1
            assert not any(topic.endswith('/serial/responses')
                           for topic, _ in receivers[1 - source_idx].received)
            topic, token = responses[0]
            assert topic == f'meshcore/{expected_iata}/{PUBLIC_KEY}/serial/responses'
            payload = state.auth.verify_token(token, PUBLIC_KEY)
            assert payload['success'] is True
            assert payload['request_id'] == 'unique'
            assert 'v1' in payload['response']
    finally:
        for client, receiver in zip(clients, receivers):
            close_client(client, receiver)
        for receiver in receivers[len(clients):]:
            receiver.close()


@pytest.mark.parametrize('name, value', [('nonce_ttl', 0), ('nonce_ttl', True),
                                        ('max_pending_nonces', -1),
                                        ('max_pending_nonces', 1.5)])
def test_invalid_replay_cache_settings_fail_clearly(name, value):
    with pytest.raises(ValueError, match=f'remote_serial.{name}'):
        remote_state(**{name: value})
