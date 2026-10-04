"""Real MQTT wire coverage for retained shutdown and unique client IDs."""
from __future__ import annotations

import json
import os
import struct
import threading
import time

import pytest

from bridge.broker_client import PahoBrokerClient
from bridge.mqtt_manager import MqttManager
from bridge.mqtt_publish import publish_shutdown_status
from bridge.runner import _cleanup
from tests.fakes import make_config, make_test_state
from tests.test_broker_backpressure import LocalMqttReceiver
from tests.test_broker_lifecycle_integration import wait_until
from tests.test_mqtt_lifecycle import LifecycleClient, ManualClock, no_jitter
from tests.test_serial_streaming import PtyRadio


class StatusReceiver(LocalMqttReceiver):
    """A real local broker retaining statuses and honoring DISCONNECT/LWT."""

    def __init__(self, *, acknowledge=True, ack_gate=None, connack_gate=None):
        self.client_id = None
        self.will = None
        self.retained = {}
        self.publications = []
        self.wire_events = []
        self.online = threading.Event()
        self.offline = threading.Event()
        self.finished = threading.Event()
        self.graceful = False
        self.ack_gate = ack_gate
        self.connack_gate = connack_gate
        self.connect_seen = threading.Event()
        super().__init__(acknowledge=acknowledge)

    @staticmethod
    def _string(data, offset):
        length = struct.unpack('!H', data[offset:offset + 2])[0]
        end = offset + 2 + length
        assert end <= len(data)
        return data[offset + 2:end].decode('utf-8'), end

    def _run(self):
        try:
            self.connection, _ = self.listener.accept()
            self.connection.settimeout(0.1)
            header, data = self._packet()
            assert header == 0x10
            assert data[:7] == b'\x00\x04MQTT\x04'
            flags = data[7]
            self.client_id, offset = self._string(data, 10)
            if flags & 4:
                topic, offset = self._string(data, offset)
                payload, offset = self._string(data, offset)
                self.will = (topic, json.loads(payload), bool(flags & 32))
            self.connect_seen.set()
            if self.connack_gate is not None:
                assert self.connack_gate.wait(2), 'test did not release CONNACK'
            self.connection.sendall(b'\x20\x02\x00\x00')
            while not self.stopped.is_set():
                header, data = self._packet()
                command = header & 0xF0
                if command == 0x30:
                    topic, offset = self._string(data, 0)
                    qos = (header >> 1) & 3
                    mid = data[offset:offset + 2] if qos else None
                    offset += 2 if qos else 0
                    message = json.loads(data[offset:].decode('utf-8'))
                    retain = bool(header & 1)
                    self.publications.append((topic, message, qos, retain))
                    self.wire_events.append(('publish', message['status']))
                    if retain:
                        self.retained[topic] = message
                    if message['status'] == 'online':
                        self.online.set()
                    elif message['status'] == 'offline':
                        self.offline.set()
                        if self.ack_gate is not None:
                            self.ack_gate.wait(2)
                    if qos and self.acknowledge:
                        prefix = b'\x40\x02' if qos == 1 else b'\x50\x02'
                        self.connection.sendall(prefix + mid)
                        if qos == 1:
                            self.wire_events.append(('puback', message['status']))
                elif command == 0x60 and self.acknowledge:
                    self.connection.sendall(b'\x70\x02' + data[:2])
                elif command == 0xC0:
                    self.connection.sendall(b'\xD0\x00')
                elif command == 0xE0:
                    self.wire_events.append(('disconnect', None))
                    self.graceful = True
                    return
        except (OSError, EOFError):
            pass
        except Exception as exc:
            self.errors.append(exc)
        finally:
            self.wire_events.append(('close', None))
            if not self.graceful and self.will is not None:
                topic, message, retain = self.will
                if retain:
                    self.retained[topic] = message
            self.finished.set()


def start_manager(receiver, *, client_factory=PahoBrokerClient, **broker_options):
    config = make_config()
    config['broker'][0].update(server='127.0.0.1', port=receiver.port, **broker_options)
    state = make_test_state(config=config, repeater_name='ShutdownNode', repeater_pub_key='AA' * 32)
    manager = MqttManager(state, client_factory=client_factory)
    state.mqtt_manager = manager
    manager.start()
    assert receiver.online.wait(3)
    wait_until(lambda: state.mqtt_clients[0]['connected'])
    return state, manager, state.mqtt_clients[0]['client']


class GatedOnlineClient(LifecycleClient):
    """Control startup publication with events while preserving ABC behavior."""

    def __init__(self, entered, release, **options):
        super().__init__(0, **options)
        self.entered = entered
        self.release = release

    def publish(self, topic, payload, qos=0, retain=False):
        if json.loads(payload)['status'] == 'online':
            self.entered.set()
            assert self.release.wait(2), 'test did not release online publication'
        return super().publish(topic, payload, qos, retain)

    def publish_confirmed(self, topic, payload, qos=1, retain=False, *, timeout):
        return self.publish(topic, payload, qos, retain)


def test_cleanup_serializes_offline_after_in_progress_online_publication():
    entered = threading.Event()
    release = threading.Event()
    clients = []

    def factory(**options):
        client = GatedOnlineClient(entered, release, **options)
        clients.append(client)
        return client

    state = make_test_state(repeater_name='RaceNode', repeater_pub_key='AA' * 32)
    manager = MqttManager(state, clock=ManualClock(), jitter=no_jitter,
                          client_factory=factory)
    state.mqtt_manager = manager
    connect_worker = threading.Thread(target=manager.reconnect_disconnected_brokers,
                                      daemon=True)
    cleanup_entered = threading.Event()
    errors = []

    def cleanup():
        cleanup_entered.set()
        try:
            _cleanup(state, None)
        except Exception as exc:
            errors.append(exc)

    cleanup_worker = threading.Thread(target=cleanup, daemon=True)
    try:
        connect_worker.start()
        assert entered.wait(1)
        cleanup_worker.start()
        assert cleanup_entered.wait(1)
        # Cleanup must wait for the retained online publication to finish;
        # otherwise it could confirm offline and then overwrite it online.
        cleanup_worker.join(timeout=0.05)
        assert cleanup_worker.is_alive()
        assert not clients[0].published
        release.set()
        connect_worker.join(timeout=2)
        cleanup_worker.join(timeout=2)
        assert not connect_worker.is_alive()
        assert not cleanup_worker.is_alive()
        assert not errors
        assert [json.loads(payload)['status'] for _, payload, _, _ in
                clients[0].published] == ['online', 'offline']
    finally:
        release.set()
        if connect_worker.ident is not None:
            connect_worker.join(timeout=2)
        if cleanup_worker.ident is not None:
            cleanup_worker.join(timeout=2)
        manager.stop(timeout=2)


def test_cleanup_aborts_transport_waiting_for_connack_to_preserve_lwt():
    gate = threading.Event()
    receiver = StatusReceiver(connack_gate=gate)
    config = make_config()
    config['broker'][0].update(server='127.0.0.1', port=receiver.port)
    state = make_test_state(config=config, repeater_name='ConnectingNode',
                            repeater_pub_key='AA' * 32)
    manager = MqttManager(state)
    state.mqtt_manager = manager
    try:
        manager.start()
        assert receiver.connect_seen.wait(1)
        assert not state.mqtt_clients[0]['connected']
        _cleanup(state, None)
        gate.set()
        assert receiver.finished.wait(2)
        assert not receiver.graceful
        assert not receiver.publications
        assert next(iter(receiver.retained.values()))['status'] == 'offline'
    finally:
        gate.set()
        manager.stop(timeout=2)
        receiver.close()


def test_shutdown_aborts_connect_returning_after_exit_to_preserve_lwt():
    connected = threading.Event()
    release = threading.Event()
    receiver = StatusReceiver()
    clients = []

    class GatedConnectClient(PahoBrokerClient):
        def connect(self, server, port, keepalive=60):
            super().connect(server, port, keepalive=keepalive)
            connected.set()
            assert release.wait(2), 'test did not release transport setup'

    def factory(**options):
        client = GatedConnectClient(**options)
        clients.append(client)
        return client

    config = make_config()
    config['broker'][0].update(server='127.0.0.1', port=receiver.port)
    state = make_test_state(config=config, repeater_name='LateConnectNode',
                            repeater_pub_key='AA' * 32)
    manager = MqttManager(state, client_factory=factory)
    state.mqtt_manager = manager
    try:
        manager.start()
        assert connected.wait(1)
        assert receiver.connect_seen.wait(1)
        # CONNECT is on the wire, but the supervisor has not yet returned
        # from transport setup or started processing the broker's CONNACK.
        state.should_exit = True
        release.set()
        assert receiver.finished.wait(2)
        assert not receiver.graceful
        assert not receiver.publications
        assert next(iter(receiver.retained.values()))['status'] == 'offline'
        wait_until(lambda: state.mqtt_clients[0]['client'] is None)
        _cleanup(state, None)
        assert clients[0].raw_client.socket() is None
    finally:
        release.set()
        manager.stop(timeout=2)
        receiver.close()


@pytest.mark.parametrize('qos', [0, 1, 2])
@pytest.mark.parametrize('retain', [False, True])
def test_offline_confirmed_before_actual_graceful_disconnect(qos, retain):
    receiver = StatusReceiver()
    state, manager, client = start_manager(receiver, qos=qos, retain=retain)
    try:
        state.should_exit = True
        assert publish_shutdown_status(state, client, 0, timeout=1)
        client.disconnect()
        assert receiver.finished.wait(2)
        assert receiver.graceful
        online, offline = receiver.publications
        assert online[1]['status'] == 'online'
        assert offline[1]['status'] == 'offline'
        assert online[3] == offline[3] == retain
        assert offline[2] == max(1, qos)
        if retain:
            assert receiver.retained[offline[0]]['status'] == 'offline'
        else:
            assert receiver.retained == {}
    finally:
        manager.stop(timeout=2)
        receiver.close()


def test_exit_flag_keeps_transport_alive_until_offline_acknowledgment():
    gate = threading.Event()
    receiver = StatusReceiver(ack_gate=gate)
    state, manager, client = start_manager(receiver)
    result = []
    worker = threading.Thread(target=lambda: result.append(
        publish_shutdown_status(state, client, 0, timeout=1)), daemon=True)
    try:
        state.should_exit = True
        time.sleep(0.15)  # More than one supervisor sweep after the signal flag.
        assert client.is_connected
        assert not receiver.finished.is_set()
        worker.start()
        assert receiver.offline.wait(1)
        assert worker.is_alive()  # Receipt is not complete before broker PUBACK.
        assert client.is_connected
        gate.set()
        worker.join(timeout=2)
        assert not worker.is_alive()
        assert result == [True]
        assert manager.stop(timeout=2)
        assert receiver.finished.wait(2)
        assert next(iter(receiver.retained.values()))['status'] == 'offline'
    finally:
        gate.set()
        if worker.ident is not None:
            worker.join(timeout=2)
        manager.stop(timeout=2)
        receiver.close()


@pytest.mark.parametrize('timeout', [0, 0.05])
def test_unconfirmed_offline_aborts_instead_of_suppressing_lwt(timeout):
    receiver = StatusReceiver(acknowledge=False)
    state, manager, client = start_manager(receiver)
    try:
        state.should_exit = True
        started = time.monotonic()
        assert not publish_shutdown_status(state, client, 0, timeout=timeout)
        assert time.monotonic() - started < 0.5
        assert manager.stop(timeout=2)
        assert receiver.finished.wait(2)
        assert not receiver.graceful
        assert next(iter(receiver.retained.values()))['status'] == 'offline'
        if timeout == 0:
            assert not receiver.offline.is_set()
    finally:
        manager.stop(timeout=2)
        receiver.close()


def test_full_budget_refuses_shutdown_publish_and_preserves_lwt():
    receiver = StatusReceiver(acknowledge=False)
    state, manager, client = start_manager(receiver, max_pending_messages=1)
    try:
        wait_until(lambda: client.pending_messages == 0)
        assert client.publish('held', json.dumps({'status': 'held'}), qos=1)
        assert client.pending_messages == 1
        state.should_exit = True
        assert not publish_shutdown_status(state, client, 0, timeout=0.1)
        assert manager.stop(timeout=2)
        assert receiver.finished.wait(2)
        assert not receiver.graceful
        assert next(iter(receiver.retained.values()))['status'] == 'offline'
    finally:
        manager.stop(timeout=2)
        receiver.close()


def test_publish_timeout_retires_transport_during_backoff_and_preserves_lwt():
    receiver = StatusReceiver(acknowledge=False)
    config = make_config()
    config['broker'][0].update(server='127.0.0.1', port=receiver.port,
                                qos=1, publish_timeout=0.03)
    state = make_test_state(config=config, repeater_name='StalledNode',
                            repeater_pub_key='AA' * 32)
    clock = ManualClock()
    manager = MqttManager(state, clock=clock, jitter=no_jitter)
    try:
        manager.reconnect_disconnected_brokers()
        assert receiver.online.wait(2)
        client = state.mqtt_clients[0]['client']
        wait_until(lambda: client.publish_stalled)
        manager.reconnect_disconnected_brokers()
        info = state.mqtt_clients[0]
        assert info['failed_attempts'] == 1
        assert info['reconnect_at'] > clock.now
        # The stalled session must go offline now, even while its next
        # connection attempt is delayed. DISCONNECT would suppress its will.
        assert receiver.finished.wait(1)
        assert info['client'] is None
        assert not receiver.graceful
        assert next(iter(receiver.retained.values()))['status'] == 'offline'
        assert not client.is_connected
    finally:
        manager.stop(timeout=2)
        receiver.close()


def test_explicit_manager_stop_without_confirmed_offline_preserves_lwt():
    receiver = StatusReceiver()

    class ObservedDisconnectClient(PahoBrokerClient):
        def disconnect(self):
            super().disconnect()
            # Let the real MQTT loop and server process DISCONNECT before
            # returning, rather than relying on a favorable abort/send race.
            assert receiver.finished.wait(1)

    state, manager, client = start_manager(receiver, client_factory=ObservedDisconnectClient)
    try:
        assert not state.should_exit
        assert manager.stop(timeout=2)
        assert receiver.finished.wait(1)
        assert not receiver.graceful
        assert next(iter(receiver.retained.values()))['status'] == 'offline'
    finally:
        manager.stop(timeout=2)
        receiver.close()


def test_wire_client_ids_use_current_broker_prefix_and_complete_node_identity():
    receivers = [StatusReceiver() for _ in range(4)]
    managers = []
    try:
        for node in range(2):
            config = make_config()
            template = config['broker'][0]
            config['broker'] = [dict(template), dict(template)]
            for broker_idx, prefix in enumerate(('first_really_long_site_', 'second_really_long_site_')):
                config['broker'][broker_idx].update(
                    name=f'broker-{broker_idx}', client_id_prefix=prefix,
                    server='127.0.0.1', port=receivers[node * 2 + broker_idx].port,
                )
            state = make_test_state(config=config, repeater_name='Node',
                                    repeater_pub_key='AA' * 31 + ('AA' if node == 0 else 'BB'))
            manager = MqttManager(state)
            managers.append(manager)
            manager.start()
        for receiver in receivers:
            assert receiver.online.wait(3)
        ids = [receiver.client_id for receiver in receivers]
        assert len(set(ids)) == 4
        assert all(1 <= len(client_id) <= 23 for client_id in ids)
        assert ids[0].startswith('first_')
        assert ids[1].startswith('second_')
        assert ids[2].startswith('first_')
        assert ids[3].startswith('second_')
    finally:
        for manager in managers:
            manager.stop(timeout=2)
        for receiver in receivers:
            receiver.close()


@pytest.mark.skipif(os.name != 'posix', reason='requires POSIX PTYs')
def test_runner_cleanup_waits_for_offline_puback_then_closes_real_serial():
    gate = threading.Event()
    receiver = StatusReceiver(ack_gate=gate)
    state, manager, client = start_manager(receiver)
    state.stats['device'] = {'battery_mv': 4111}
    errors = []

    def cleanup():
        try:
            _cleanup(state, None)
        except Exception as exc:
            errors.append(exc)

    worker = threading.Thread(target=cleanup, daemon=True)
    try:
        with PtyRadio() as radio:
            state.device = radio.connection
            radio.send(b'DEBUG: captured before cleanup\r\n')
            wait_until(lambda: radio.connection.read_line() == 'DEBUG: captured before cleanup')
            worker.start()
            assert receiver.offline.wait(1)
            assert worker.is_alive()
            assert radio.connection.is_open
            assert client.is_connected
            gate.set()
            worker.join(timeout=3)
            assert not worker.is_alive()
            assert not errors
            assert state.should_exit
            assert not radio.connection.is_open
            assert not manager.is_healthy()
            assert client.publish_stats['completed'] == 2  # Online handoff and offline PUBACK.
            assert receiver.finished.wait(2)
            final_status = next(iter(receiver.retained.values()))
            assert final_status['status'] == 'offline'
            assert final_status['stats'] == {'battery_mv': 4111}
            assert receiver.graceful
            events = receiver.wire_events
            assert events.index(('publish', 'offline')) < events.index(('puback', 'offline'))
            assert events.index(('puback', 'offline')) < events.index(('close', None))
            assert events.index(('puback', 'offline')) < events.index(('disconnect', None))
    finally:
        gate.set()
        if worker.ident is not None:
            worker.join(timeout=3)
        manager.stop(timeout=2)
        receiver.close()


@pytest.mark.skipif(os.name != 'posix', reason='requires POSIX PTYs')
def test_runner_cleanup_unacknowledged_offline_aborts_and_closes_real_serial():
    receiver = StatusReceiver(acknowledge=False)
    state, manager, client = start_manager(receiver)
    try:
        with PtyRadio() as radio:
            state.device = radio.connection
            started = time.monotonic()
            _cleanup(state, None)
            assert time.monotonic() - started < 4
            assert not radio.connection.is_open
            assert not manager.is_healthy()
            assert receiver.finished.wait(2)
            assert receiver.offline.is_set()
            assert client.publish_stats['completed'] == 1  # Only the initial QoS 0 online.
            assert ('puback', 'offline') not in receiver.wire_events
            assert not receiver.graceful
            assert next(iter(receiver.retained.values()))['status'] == 'offline'
    finally:
        manager.stop(timeout=2)
        receiver.close()


def test_runner_cleanup_uses_one_shared_ack_deadline_for_multiple_brokers():
    receivers = [StatusReceiver(acknowledge=False) for _ in range(4)]
    config = make_config()
    template = config['broker'][0]
    config['broker'] = [dict(template, name=f'broker-{index}',
                             server='127.0.0.1', port=receiver.port)
                        for index, receiver in enumerate(receivers)]
    state = make_test_state(config=config, repeater_name='DeadlineNode', repeater_pub_key='AA' * 32)
    manager = MqttManager(state)
    state.mqtt_manager = manager
    manager.start()
    try:
        for receiver in receivers:
            assert receiver.online.wait(3)
        started = time.monotonic()
        _cleanup(state, None)
        elapsed = time.monotonic() - started
        # Four independent 2s waits would require at least 8s. A shared 5s
        # budget also covers the remaining transports' immediate LWT fallback.
        assert elapsed < 7
        assert not manager.is_healthy()
        for receiver in receivers:
            assert receiver.finished.wait(2)
            assert not receiver.graceful
            assert ('puback', 'offline') not in receiver.wire_events
            assert next(iter(receiver.retained.values()))['status'] == 'offline'
    finally:
        manager.stop(timeout=2)
        for receiver in receivers:
            receiver.close()
