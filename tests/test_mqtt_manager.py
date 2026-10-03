"""Focused MQTT manager API tests; lifecycle regressions live next door."""
from __future__ import annotations

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
