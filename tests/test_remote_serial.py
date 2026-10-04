"""Tests for remote serial command handling."""
from __future__ import annotations

import time

import pytest

from bridge.remote_serial import (
    handle_serial_command,
    cleanup_old_nonces,
    is_command_allowed,
    subscribe_serial_commands,
)
from bridge.state import BridgeState, parse_allowed_companions
from tests.fakes import (
    FakeAuthProvider,
    FakeBrokerClient,
    FakeSerialConnection,
    make_test_state,
    make_config,
)


def _make_command_token(auth, *, pubkey="CC" * 32, command="ver", target="AA" * 32, nonce="testnonce123"):
    """Create a fake JWT command token."""
    return auth.create_token(
        pubkey, "DD" * 64,
        expiry_seconds=60,
        publicKey=pubkey,
        command=command,
        target=target,
        nonce=nonce,
    )


def _make_remote_state(**kwargs):
    """Create a state configured for remote serial testing."""
    config = make_config(remote_serial={
        'enabled': True,
        'allowed_companions': ["CC" * 32],
        'disallowed_commands': ['get prv.key', 'erase'],
        'nonce_ttl': 120,
        'command_timeout': 10,
    })
    auth = FakeAuthProvider(valid_keys={"CC" * 32})
    device = FakeSerialConnection()
    return make_test_state(
        config=config,
        device=device,
        auth=auth,
        repeater_pub_key="AA" * 32,
        repeater_priv_key="BB" * 64,
        **kwargs,
    )


class TestHandleSerialCommand:
    def test_late_command_after_shutdown_has_no_nonce_or_response_side_effects(self):
        state = _make_remote_state()
        broker = FakeBrokerClient()
        state.mqtt_clients = [{'client': broker, 'broker_idx': 0, 'connected': True}]
        state.should_exit = True

        handle_serial_command(state, _make_command_token(state.auth), broker_idx=0)

        assert state.device.commands_executed == []
        assert state.remote_serial_nonces == {}
        assert broker.published == []

    def test_valid_command(self):
        state = _make_remote_state()
        broker = FakeBrokerClient()
        broker._connected = True
        state.mqtt_clients = [{"client": broker, "broker_idx": 0, "connected": True}]
        state.mqtt_connected = True

        token = _make_command_token(state.auth)
        handle_serial_command(state, token, broker_idx=0)

        # Command should have been executed
        assert "ver" in state.device.commands_executed

    def test_rejects_unknown_companion(self):
        state = _make_remote_state()
        broker = FakeBrokerClient()
        broker._connected = True
        state.mqtt_clients = [{"client": broker, "broker_idx": 0, "connected": True}]
        state.mqtt_connected = True

        # Use a different companion key that's not in the allowlist
        token = _make_command_token(state.auth, pubkey="DD" * 32)
        handle_serial_command(state, token, broker_idx=0)

        # Command should NOT have been executed
        assert len(state.device.commands_executed) == 0

    def test_rejects_wrong_target(self):
        state = _make_remote_state()
        # Token targets a different node
        token = _make_command_token(state.auth, target="FF" * 32)
        handle_serial_command(state, token, broker_idx=0)
        assert len(state.device.commands_executed) == 0

    def test_rejects_expired_token(self):
        state = _make_remote_state()
        broker = FakeBrokerClient()
        broker._connected = True
        state.mqtt_clients = [{"client": broker, "broker_idx": 0, "connected": True}]
        state.mqtt_connected = True

        # Create a token with expired time
        auth = state.auth
        token = auth.create_token(
            "CC" * 32, "DD" * 64,
            expiry_seconds=-10,  # already expired
            publicKey="CC" * 32,
            command="ver",
            target="AA" * 32,
            nonce="expired_nonce",
        )
        handle_serial_command(state, token, broker_idx=0)
        assert len(state.device.commands_executed) == 0

    def test_rejects_replay_nonce(self):
        state = _make_remote_state()
        broker = FakeBrokerClient()
        broker._connected = True
        state.mqtt_clients = [{"client": broker, "broker_idx": 0, "connected": True}]
        state.mqtt_connected = True

        # Pre-record the nonce
        state.remote_serial_nonces["testnonce123"] = int(time.time()) + 120

        token = _make_command_token(state.auth)
        handle_serial_command(state, token, broker_idx=0)
        assert len(state.device.commands_executed) == 0

    def test_rejects_disallowed_command(self):
        state = _make_remote_state()
        broker = FakeBrokerClient()
        broker._connected = True
        state.mqtt_clients = [{"client": broker, "broker_idx": 0, "connected": True}]
        state.mqtt_connected = True

        token = _make_command_token(state.auth, command="get prv.key", nonce="disallowed_nonce")
        handle_serial_command(state, token, broker_idx=0)
        assert len(state.device.commands_executed) == 0

    def test_rejects_invalid_signature(self):
        state = _make_remote_state()
        # Make verify fail
        state.auth = FakeAuthProvider(should_fail_verify=True)

        broker = FakeBrokerClient()
        broker._connected = True
        state.mqtt_clients = [{"client": broker, "broker_idx": 0, "connected": True}]
        state.mqtt_connected = True

        token = _make_command_token(FakeAuthProvider(valid_keys={"CC" * 32}), nonce="sig_nonce")
        handle_serial_command(state, token, broker_idx=0)
        assert len(state.device.commands_executed) == 0


class TestCleanupNonces:
    def test_removes_expired(self):
        state = _make_remote_state()
        old_time = int(time.time()) - 200
        state.remote_serial_nonces = {"old_nonce": old_time}
        cleanup_old_nonces(state)
        assert "old_nonce" not in state.remote_serial_nonces

    def test_keeps_fresh(self):
        state = _make_remote_state()
        fresh_time = int(time.time()) + 120
        state.remote_serial_nonces = {"fresh_nonce": fresh_time}
        cleanup_old_nonces(state)
        assert "fresh_nonce" in state.remote_serial_nonces


class TestParseAllowedCompanions:
    def test_valid_keys(self):
        config = {'allowed_companions': ["AA" * 32, "bb" * 32]}
        result = parse_allowed_companions(config)
        assert len(result) == 2
        assert "AA" * 32 in result
        assert ("bb" * 32).upper() in result

    def test_rejects_invalid(self):
        config = {'allowed_companions': ["too_short", "ZZ" * 32]}
        result = parse_allowed_companions(config)
        assert len(result) == 0

    def test_empty_list(self):
        config = {'allowed_companions': []}
        result = parse_allowed_companions(config)
        assert len(result) == 0


class TestIsCommandAllowed:
    def test_allowed(self):
        state = _make_remote_state()
        allowed, reason = is_command_allowed(state, "ver")
        assert allowed is True
        assert reason is None

    def test_disallowed(self):
        state = _make_remote_state()
        allowed, reason = is_command_allowed(state, "get prv.key")
        assert allowed is False
        assert reason == "get prv.key"

    def test_disallowed_prefix_match(self):
        state = _make_remote_state()
        allowed, reason = is_command_allowed(state, "erase all")
        assert allowed is False
        assert reason == "erase"


class TestSubscribeSerialCommands:
    def test_subscribes_to_correct_topic(self):
        state = _make_remote_state()
        client = FakeBrokerClient()
        subscribe_serial_commands(state, client, broker_idx=0)
        assert len(client.subscribed) == 1
        expected = f"meshcore/TST/{'AA' * 32}/serial/commands"
        assert client.subscribed[0] == expected

    def test_skips_when_disabled(self):
        state = _make_remote_state()
        state.remote_serial_enabled = False
        client = FakeBrokerClient()
        subscribe_serial_commands(state, client, broker_idx=0)
        assert len(client.subscribed) == 0


@pytest.mark.parametrize('name, value', [
    ('enabled', 'false'), ('enabled', 1),
    ('allowed_companions', 'AA' * 32), ('allowed_companions', ['AA' * 32, 42]),
    ('allowed_companions', {}),
    ('disallowed_commands', 'erase'), ('disallowed_commands', ['erase', 42]),
    ('command_timeout', 0), ('command_timeout', -1), ('command_timeout', True),
    ('command_timeout', '10'), ('command_timeout', float('inf')),
    ('command_timeout', float('nan')),
])
def test_invalid_remote_command_config_fails_before_callbacks(name, value):
    with pytest.raises(ValueError, match=f'remote_serial.{name}'):
        BridgeState({'remote_serial': {name: value}})


@pytest.mark.parametrize('timeout', [0.5, 10, 120.0])
def test_remote_command_timeout_accepts_positive_finite_numbers(timeout):
    state = BridgeState({'remote_serial': {'command_timeout': timeout}})
    assert state.remote_serial_command_timeout == timeout


@pytest.mark.parametrize('remote_config', [False, [], 'enabled'])
def test_remote_command_config_requires_a_table(remote_config):
    with pytest.raises(ValueError, match='remote_serial must be a configuration table'):
        BridgeState({'remote_serial': remote_config})
