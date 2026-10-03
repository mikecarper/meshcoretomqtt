"""MQTT lifecycle regressions using controlled ABC clients, not mocks."""
from __future__ import annotations

from collections import deque
import threading
import time

import pytest

from bridge.mqtt_manager import MqttManager
from tests.fakes import FakeBrokerClient, make_config, make_test_state


class ManualClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def no_jitter(low, high):
    return 0.0


class LifecycleClient(FakeBrokerClient):
    """Transport success, MQTT rejection, and callbacks are separate events."""

    def __init__(self, outcome, **options):
        super().__init__()
        self.outcome = outcome
        self.options = options
        self.userdata = options['userdata']
        self.started = False
        self.stops = 0
        self.close_order = []
        self.publish_stalled = False

    @property
    def raw_client(self):
        return self

    def connect(self, server, port, keepalive=60):
        self.connect_calls.append((server, port, keepalive))
        if isinstance(self.outcome, Exception):
            raise self.outcome

    def loop_start(self):
        self.started = True
        if isinstance(self.outcome, int):
            self._connected = self.outcome == 0
            self.options['on_connect'](self, self.userdata, None, self.outcome)
            if self.outcome:
                self.options['on_disconnect'](self, self.userdata, None, self.outcome, None)

    def loop_stop(self):
        self.close_order.append('loop_stop')
        self.started = False
        self.stops += 1

    def disconnect(self):
        self.close_order.append('disconnect')
        super().disconnect()
        if self.started:
            self.options['on_disconnect'](self, self.userdata, None, 0, None)

    def abort(self):
        self.close_order.append('abort')
        self._connected = False

    def lost_connection(self):
        self._connected = False
        self.options['on_disconnect'](self, self.userdata, None, 7, None)


class LifecycleFactory:
    def __init__(self, outcomes=None):
        self.outcomes = {index: deque(values) for index, values in (outcomes or {}).items()}
        self.clients = []

    def __call__(self, **options):
        sequence = self.outcomes.get(options['userdata']['broker_idx'])
        outcome = sequence.popleft() if sequence else 0
        client = LifecycleClient(outcome, **options)
        self.clients.append(client)
        return client


def make_manager(outcomes=None, config=None):
    clock = ManualClock()
    factory = LifecycleFactory(outcomes)
    state = make_test_state(config=config, repeater_name='TestNode', repeater_pub_key='AA' * 32)
    manager = MqttManager(state, clock=clock, jitter=no_jitter, client_factory=factory)
    state.mqtt_manager = manager
    return state, manager, clock, factory


def test_connect_sets_online_without_resetting_history():
    state, manager, clock, factory = make_manager()
    manager._ensure_slots()
    state.mqtt_clients[0].update(failed_attempts=4, reconnect_delay=5.0)
    manager.reconnect_disconnected_brokers()
    info = state.mqtt_clients[0]
    assert info['connected'] and state.mqtt_connected
    assert info['failed_attempts'] == 4
    assert info['reconnect_delay'] == 5.0
    assert state.connection_events[0].is_set()
    assert factory.clients[0].published
    clock.advance(119.9)
    manager.reconnect_disconnected_brokers()
    assert info['failed_attempts'] == 4
    clock.advance(0.1)
    manager.reconnect_disconnected_brokers()
    assert info['failed_attempts'] == 0
    assert info['reconnect_delay'] == 1.0


def test_mqtt_rejection_and_disconnect_count_once():
    state, manager, clock, factory = make_manager({0: [5]})
    manager.reconnect_disconnected_brokers()
    info = state.mqtt_clients[0]
    assert not info['connected'] and not state.mqtt_connected
    assert info['failed_attempts'] == 1
    assert info['reconnect_at'] == 1.0
    assert info['reconnect_delay'] == 1.5
    assert state.connection_events[0].is_set()
    factory.clients[0].lost_connection()
    assert info['failed_attempts'] == 1


def test_transport_connect_does_not_reset_rejection_failures():
    state, manager, clock, factory = make_manager({0: [5] * 12})
    for number in range(12):
        if number:
            clock.now = state.mqtt_clients[0]['reconnect_at']
        manager.reconnect_disconnected_brokers()
        assert state.mqtt_clients[0]['failed_attempts'] == number + 1
    assert state.should_exit
    assert len(state.mqtt_clients) == 1
    assert len(factory.clients) == 12
    assert all(client.stops == 1 for client in factory.clients[:-1])


def test_missing_connack_counts_and_retires_generation():
    state, manager, clock, factory = make_manager({0: ['silent', 0]})
    manager.reconnect_disconnected_brokers()
    info = state.mqtt_clients[0]
    clock.advance(9.99)
    manager.reconnect_disconnected_brokers()
    assert info['failed_attempts'] == 0
    clock.advance(0.01)
    manager.reconnect_disconnected_brokers()
    assert info['failed_attempts'] == 1
    assert not info['connected']
    old = factory.clients[0]
    # A late CONNACK cannot revive a generation already declared failed.
    manager.on_mqtt_connect(old, old.userdata, None, 0)
    assert not info['connected']
    clock.now = info['reconnect_at']
    manager.reconnect_disconnected_brokers()
    assert old.stops == 1
    assert info['connected'] and info['failed_attempts'] == 1


def test_stale_callbacks_cannot_mutate_replacement():
    state, manager, clock, factory = make_manager()
    manager.reconnect_disconnected_brokers()
    old = factory.clients[0]
    old.lost_connection()
    clock.now = state.mqtt_clients[0]['reconnect_at']
    manager.reconnect_disconnected_brokers()
    info = state.mqtt_clients[0]
    generation = info['generation']
    event = state.connection_events[0]
    event.clear()
    manager.on_mqtt_disconnect(old, old.userdata, None, 7, None)
    manager.on_mqtt_connect(old, old.userdata, None, 5)
    assert info['connected']
    assert info['failed_attempts'] == 1
    assert info['generation'] == generation
    assert not event.is_set()


def test_wrong_raw_client_identity_is_rejected_even_with_current_generation():
    state, manager, clock, factory = make_manager()
    manager.reconnect_disconnected_brokers()
    info = state.mqtt_clients[0]
    manager.on_mqtt_disconnect(FakeBrokerClient(), factory.clients[0].userdata, None, 7, None)
    assert info['connected']
    assert info['failed_attempts'] == 0


def test_short_lived_success_preserves_backoff():
    state, manager, clock, factory = make_manager({0: [5, 0, 0]})
    manager.reconnect_disconnected_brokers()
    info = state.mqtt_clients[0]
    clock.now = info['reconnect_at']
    manager.reconnect_disconnected_brokers()
    assert info['failed_attempts'] == 1
    assert info['reconnect_delay'] == 1.5
    clock.advance(119)
    factory.clients[-1].lost_connection()
    assert info['failed_attempts'] == 2
    assert info['reconnect_delay'] == 2.25
    clock.now = info['reconnect_at']
    manager.reconnect_disconnected_brokers()
    clock.advance(120)
    factory.clients[-1].lost_connection()
    # Stable success resets history, then this disconnect starts a new streak.
    assert info['failed_attempts'] == 1
    assert info['reconnect_delay'] == 1.5


def test_skips_connected_brokers_and_clears_token_on_retry():
    state, manager, clock, factory = make_manager()
    state.token_cache[0] = ('cached', 0)
    manager.reconnect_disconnected_brokers()
    assert 0 not in state.token_cache
    manager.reconnect_disconnected_brokers()
    assert len(factory.clients) == 1


def test_backoff_is_independent_per_broker():
    config = make_config()
    config['broker'].append(dict(config['broker'][0], name='second'))
    state, manager, clock, factory = make_manager({0: [OSError('offline')] * 5}, config)
    manager.reconnect_disconnected_brokers()
    for _ in range(4):
        clock.now = state.mqtt_clients[0]['reconnect_at']
        manager.reconnect_disconnected_brokers()
    first, second = state.mqtt_clients
    assert first['reconnect_delay'] == pytest.approx(1.5 ** 5)
    assert second['reconnect_delay'] == 1.0
    assert second['failed_attempts'] == 0
    assert second['connected']


def test_failed_initial_retry_keeps_one_slot_and_closes_each_client():
    state, manager, clock, factory = make_manager({0: [5] * 4})
    for _ in range(4):
        assert not manager.connect_all_brokers()
        assert len(state.mqtt_clients) == 1
        clock.now = state.mqtt_clients[0]['reconnect_at']
    assert state.mqtt_clients[0]['failed_attempts'] == 4
    assert len(factory.clients) == 4
    assert all(client.stops == 1 and client.disconnect_calls == 1 for client in factory.clients)


def test_unavailable_broker_slot_is_retried_after_other_broker_connects():
    config = make_config()
    config['broker'].append(dict(config['broker'][0], name='second'))
    state, manager, clock, factory = make_manager({0: [OSError('offline'), 0]}, config)
    manager.reconnect_disconnected_brokers()
    assert len(state.mqtt_clients) == 2
    assert state.mqtt_connected
    assert not state.mqtt_clients[0]['connected']
    clock.now = state.mqtt_clients[0]['reconnect_at']
    manager.reconnect_disconnected_brokers()
    assert state.mqtt_clients[0]['connected']


def test_publish_stall_retires_client_and_ignores_late_callbacks():
    state, manager, clock, factory = make_manager()
    manager.reconnect_disconnected_brokers()
    old = factory.clients[0]
    old.publish_stalled = True
    manager.reconnect_disconnected_brokers()
    info = state.mqtt_clients[0]
    assert not info['connected']
    assert info['failed_attempts'] == 1
    clock.now = info['reconnect_at']
    manager.reconnect_disconnected_brokers()
    assert old.stops == 1
    assert old.close_order == ['disconnect', 'abort', 'loop_stop']
    old.lost_connection()
    assert info['connected'] and info['failed_attempts'] == 1


def test_broker_resource_limits_are_passed_to_client():
    config = make_config()
    config['broker'][0].update(max_pending_messages=17, max_pending_bytes=8192,
                               connect_timeout=41, publish_timeout=91)
    state, manager, clock, factory = make_manager(config=config)
    manager.reconnect_disconnected_brokers()
    options = factory.clients[0].options
    assert options['max_pending_messages'] == 17
    assert options['max_pending_bytes'] == 8192
    assert options['connect_timeout'] == 41
    assert options['publish_timeout'] == 91


def test_missing_server_is_a_counted_backed_off_failure():
    config = make_config()
    config['broker'][0]['server'] = ''
    state, manager, clock, factory = make_manager(config=config)
    manager.reconnect_disconnected_brokers()
    info = state.mqtt_clients[0]
    assert info['failed_attempts'] == 1
    assert info['reconnect_at'] == 1.0
    assert not factory.clients
    manager.reconnect_disconnected_brokers()
    assert info['failed_attempts'] == 1


class GatedFactory(LifecycleFactory):
    """A real event blocks transport setup while the caller keeps running."""

    def __init__(self):
        super().__init__()
        self.entered = threading.Event()
        self.release = threading.Event()

    def __call__(self, **options):
        client = GatedClient(self.entered, self.release, **options)
        self.clients.append(client)
        return client


class GatedClient(LifecycleClient):
    def __init__(self, entered, release, **options):
        super().__init__(0, **options)
        self.entered = entered
        self.release = release

    def connect(self, server, port, keepalive=60):
        self.entered.set()
        if not self.release.wait(5):
            raise TimeoutError('test transport was not released')
        super().connect(server, port, keepalive)


def test_supervisor_does_not_block_caller_and_stop_is_bounded():
    state = make_test_state(repeater_name='TestNode', repeater_pub_key='AA' * 32)
    clock = ManualClock()
    factory = GatedFactory()
    manager = MqttManager(state, clock=clock, jitter=no_jitter, client_factory=factory)
    manager.start()
    try:
        assert factory.entered.wait(1)
        assert len(state.mqtt_clients) == 1
        assert manager.is_healthy()
        # A stuck DNS/transport operation eventually fails the liveness check.
        clock.advance(121)
        assert not manager.is_healthy()
        started = time.monotonic()
        assert not manager.stop(timeout=0.01)
        assert time.monotonic() - started < 0.5
        assert not state.mqtt_connected
    finally:
        factory.release.set()
        assert manager.stop(timeout=1)
    client = factory.clients[0]
    assert not client.started
    assert client.stops == 1 and client.disconnect_calls == 1
    # A transport that returns after stop cannot publish online or start a loop.
    client.options['on_connect'](client, client.userdata, None, 0)
    assert not state.mqtt_connected
    assert not manager.is_healthy()


def test_websocket_replacements_create_no_manual_ping_workers():
    config = make_config()
    config['broker'][0]['transport'] = 'websockets'
    state, manager, clock, factory = make_manager(config=config)
    before = {thread.ident for thread in threading.enumerate()}
    for _ in range(30):
        manager.reconnect_disconnected_brokers()
        # Stabilize before each new disconnect to avoid the failure-limit exit.
        clock.advance(120)
        factory.clients[-1].lost_connection()
        clock.now = state.mqtt_clients[0]['reconnect_at']
    after = {thread.ident for thread in threading.enumerate()}
    assert after == before
    assert len(state.mqtt_clients) == 1
    assert manager.stop(timeout=1)
    assert all(client.stops == 1 for client in factory.clients)


def test_stopped_manager_cannot_be_restarted():
    state, manager, clock, factory = make_manager()
    assert manager.stop(timeout=1)
    with pytest.raises(RuntimeError, match='cannot restart'):
        manager.start()


class CounterClient(FakeBrokerClient):
    @property
    def publish_stats(self):
        return {'pending_messages': 3, 'pending_bytes': 512, 'rejected': 19}


def test_periodic_publish_pressure_uses_public_bounded_snapshots():
    from bridge.background import _publish_pressure_summary

    state = make_test_state()
    state.mqtt_clients = [{'broker_idx': 0, 'client': CounterClient()},
                          {'broker_idx': 99, 'client': CounterClient()}]
    assert _publish_pressure_summary(state) == 'test-broker:pending=3/512B,rejected=19'
    state.mqtt_clients[0]['client'] = None
    assert _publish_pressure_summary(state) == 'none'
