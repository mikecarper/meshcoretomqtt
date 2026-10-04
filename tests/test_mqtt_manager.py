"""Focused MQTT manager API tests; lifecycle regressions live next door."""
from __future__ import annotations

import pytest

from tests.fakes import FakeAuthProvider, FakeSerialConnection, make_config
from tests.test_mqtt_lifecycle import make_manager


def test_callbacks_for_unknown_generation_are_ignored():
    state, manager, clock, factory = make_manager()
    manager._ensure_slots()
    manager.on_mqtt_connect(None, {"broker_idx": 0, "generation": -1}, None, 0)
    manager.on_mqtt_disconnect(None, {"broker_idx": 0, "generation": -1}, None, 7, None)
    assert not state.mqtt_connected
    assert state.mqtt_clients[0]["failed_attempts"] == 0


def test_non_serial_mqtt_message_does_not_execute_command():
    state, manager, clock, factory = make_manager()
    manager.reconnect_disconnected_brokers()
    client = factory.clients[0]
    message = type("Message", (), {"topic": "other/topic", "payload": b"test"})()
    manager.on_mqtt_message(client, client.userdata, message)
    assert state.mqtt_clients[0]["connected"]


def test_repeated_slot_setup_does_not_duplicate_enabled_brokers():
    state, manager, clock, factory = make_manager()
    for _ in range(20):
        manager._ensure_slots()
    assert len(state.mqtt_clients) == 1
    assert len(state.connection_events) == 1


class RecordingAuthProvider(FakeAuthProvider):
    def __init__(self, **options):
        super().__init__(**options)
        self.payload_reads = 0

    def decode_payload(self, token):
        self.payload_reads += 1
        return super().decode_payload(token)


@pytest.mark.parametrize('topic_case', ['expected', 'other_iata', 'other_node', 'extra_suffix', 'shutdown'])
def test_remote_messages_require_current_exact_subscription_topic(topic_case):
    companion = 'CC' * 32
    config = make_config(remote_serial={'enabled': True, 'allowed_companions': [companion]})
    config['broker'][0]['topics'] = {'iata': 'LOCAL'}
    state, manager, clock, factory = make_manager(config=config)
    state.device = FakeSerialConnection()
    state.auth = RecordingAuthProvider(valid_keys={companion})
    state.repeater_priv_key = 'BB' * 64
    manager.reconnect_disconnected_brokers()
    client = factory.clients[0]
    expected = f'meshcore/LOCAL/{state.repeater_pub_key}/serial/commands'
    assert client.subscribed == [expected]
    topic = expected
    if topic_case == 'other_iata':
        topic = topic.replace('/LOCAL/', '/GLOBAL/')
    elif topic_case == 'other_node':
        topic = topic.replace(state.repeater_pub_key, 'DD' * 32)
    elif topic_case == 'extra_suffix':
        topic += '/extra'
    elif topic_case == 'shutdown':
        state.should_exit = True
    token = state.auth.create_token(companion, '', command='ver',
                                    target=state.repeater_pub_key, nonce='topic-test')
    message = type('Message', (), {'topic': topic, 'payload': token.encode()})()
    manager.on_mqtt_message(client, client.userdata, message)
    if topic_case == 'expected':
        assert state.device.commands_executed == ['ver']
        assert state.auth.payload_reads > 0
        assert 'topic-test' in state.remote_serial_nonces
        assert len(client.published) == 2
    else:
        assert state.device.commands_executed == []
        assert state.auth.payload_reads == 0
        assert state.remote_serial_nonces == {}
        assert len(client.published) == 1
