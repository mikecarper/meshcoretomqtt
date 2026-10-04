"""Host recovery checks using real notification sockets and boundary fakes."""
from __future__ import annotations

import configparser
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import textwrap
import time

import pytest

from bridge.message_parser import parse_and_publish
from bridge.mqtt_publish import safe_publish
from bridge.background import _refresh_device_stats
from bridge.runner import _reconnect_device, _run_main_loop, run
from bridge.service_health import ServiceHealth
from bridge.state import BridgeState
from installer import InstallerContext
from installer.system import _docker_registry_image, docker_run_command
from tests.fakes import FakeAuthProvider, FakeBrokerClient, FakeSerialConnection, make_test_state
from tests.test_serial_streaming import PtyRadio


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


@pytest.mark.skipif(not hasattr(socket, 'AF_UNIX'), reason='Unix notifications unavailable')
def test_notify_readiness_and_progress_only(tmp_path):
    address = str(tmp_path / 'notify.sock')
    clock = Clock()
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as receiver:
        receiver.bind(address)
        receiver.settimeout(0.1)
        health = ServiceHealth(environment={
            'NOTIFY_SOCKET': address,
            'WATCHDOG_USEC': '3000000',
            'WATCHDOG_PID': str(os.getpid()),
        }, clock=clock)
        assert health.ready()
        assert receiver.recv(128) == b'READY=1'
        assert health.tick()
        assert receiver.recv(128) == b'WATCHDOG=1'
        clock.now = 0.5
        assert not health.tick()
        clock.now = 2
        assert not health.tick(healthy=False)
        with pytest.raises(TimeoutError):
            receiver.recv(128)
        assert health.tick(healthy=True)
        assert receiver.recv(128) == b'WATCHDOG=1'
        assert health.stopping()
        assert receiver.recv(128) == b'STOPPING=1'


def test_notify_without_systemd_is_optional():
    health = ServiceHealth(environment={})
    assert not health.ready()
    assert not health.tick()
    assert not health.stopping()


@pytest.mark.skipif(os.name != 'posix', reason='requires POSIX PTYs')
def test_statistics_query_keeps_its_session_during_reconnect():
    state = BridgeState({})

    def reply(command):
        if command == 'stats-core':
            state.device = None
        return b'-> {"battery_mv": 4200}\r\n'

    with PtyRadio(reply) as radio:
        state.device = radio.connection
        state.stats['device'] = {'battery_mv': 4100}
        _refresh_device_stats(state)
        assert radio.commands == ['stats-core', 'stats-radio', 'stats-packets']
        assert state.device is None
        assert state.stats['device'] == {'battery_mv': 4100}


@pytest.mark.skipif(os.name != 'posix', reason='requires POSIX PTYs')
def test_statistics_query_ignores_closed_or_absent_session():
    state = BridgeState({})
    _refresh_device_stats(state)
    with PtyRadio() as radio:
        radio.connection.close()
        state.device = radio.connection
        _refresh_device_stats(state)
        assert state.stats['device'] == {}


@pytest.mark.skipif(not hasattr(socket, 'AF_UNIX'), reason='Unix notifications unavailable')
def test_nixos_health_fixture_uses_production_notifier(tmp_path):
    root = Path(__file__).parent.parent
    source = (root / 'nix/nixos-test.nix').read_text()
    match = re.search(r'healthTest = pkgs.writeText "[^"]+" \'\'\n(.*?)\n    \'\';',
                      source, re.DOTALL)
    assert match is not None
    script = textwrap.dedent(match.group(1))
    environment = os.environ.copy()
    environment.pop('WATCHDOG_PID', None)
    environment.update({
        'PYTHONPATH': str(root / 'bridge'),
        'NOTIFY_SOCKET': str(tmp_path / 'nix-notify.sock'),
        'WATCHDOG_USEC': '300000',
    })
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as receiver:
        receiver.bind(environment['NOTIFY_SOCKET'])
        receiver.settimeout(3)
        child = subprocess.Popen([sys.executable, '-c', script], env=environment)
        try:
            assert receiver.recv(128) == b'READY=1'
            assert receiver.recv(128) == b'WATCHDOG=1'
            assert receiver.recv(128) == b'WATCHDOG=1'
            assert child.poll() is None
        finally:
            child.terminate()
            try:
                child.wait(timeout=3)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=3)


@pytest.mark.parametrize('value', ['0', '-1', 'nan', 'invalid'])
def test_invalid_watchdog_environment_does_not_enable_it(value):
    health = ServiceHealth(environment={'WATCHDOG_USEC': value})
    assert not health.tick()


def test_publish_skips_disconnected_broker_and_empty_slot():
    healthy = FakeBrokerClient()
    disconnected = FakeBrokerClient()
    state = make_test_state(broker_clients=[
        {'client': healthy, 'broker_idx': 0, 'connected': True},
        {'client': disconnected, 'broker_idx': 1, 'connected': False},
        {'client': None, 'broker_idx': 2, 'connected': False},
    ], repeater_pub_key='AA' * 32)
    assert safe_publish(state, 'packets', '{}')
    assert len(healthy.published) == 1
    assert disconnected.published == []
    assert state.stats['publish_failures'] == 2


def test_outage_logs_are_rate_limited_but_drops_counted(caplog):
    state = make_test_state()
    for _ in range(100):
        assert not safe_publish(state, 'packets', '{}')
    assert state.stats['publish_failures'] == 100
    assert len([record for record in caplog.records if 'Not connected' in record.message]) == 1


def parser_state():
    broker = FakeBrokerClient()
    state = make_test_state(broker_clients=[
        {'client': broker, 'broker_idx': 0, 'connected': True},
    ], repeater_pub_key='AA' * 32)
    return state, broker


SUMMARY = '12:34:56 - 1/15/2025 U: RX, len=6 (type=1, route=D, payload_len=2)'
RAW = '12:34:56 - 1/15/2025 U RAW: AABB0011CCDD'


def test_raw_is_consumed_once():
    state, broker = parser_state()
    parse_and_publish(state, RAW)
    parse_and_publish(state, SUMMARY)
    parse_and_publish(state, SUMMARY)
    assert json.loads(broker.published[0][1])['raw'] == 'AABB0011CCDD'
    assert json.loads(broker.published[1][1])['raw'] is None


@pytest.mark.parametrize('gap', ['DROP:3', '12:34:56 - 1/15/2025 DROP:1'])
def test_drop_marker_invalidates_raw(gap):
    state, broker = parser_state()
    parse_and_publish(state, RAW)
    parse_and_publish(state, gap)
    parse_and_publish(state, SUMMARY)
    assert json.loads(broker.published[0][1])['raw'] is None


@pytest.mark.parametrize('payload', ['not-hex', 'ABC', 'AA' * 256, ''])
def test_invalid_raw_cannot_replace_a_good_packet(payload):
    state, broker = parser_state()
    parse_and_publish(state, RAW)
    parse_and_publish(state, '12:34:56 - 1/15/2025 U RAW: ' + payload)
    parse_and_publish(state, SUMMARY)
    assert json.loads(broker.published[0][1])['raw'] is None
    assert state.stats['bytes_processed'] == 6


@pytest.mark.parametrize('change', ['length', 'timestamp', 'expired'])
def test_raw_requires_matching_fresh_summary(change):
    state, broker = parser_state()
    parse_and_publish(state, RAW)
    summary = SUMMARY
    if change == 'length':
        summary = summary.replace('len=6 ', 'len=7 ')
    elif change == 'timestamp':
        summary = summary.replace('12:34:56', '12:34:57')
    else:
        state.last_raw_at = time.monotonic() - 10
    parse_and_publish(state, summary)
    assert json.loads(broker.published[0][1])['raw'] is None


class LoopManager:
    def __init__(self, healthy=True):
        self.healthy = healthy
        self.stopped = False

    def is_healthy(self):
        return self.healthy

    def stop(self):
        self.stopped = True


class LoopHealth:
    def __init__(self):
        self.ticks = []

    def tick(self, healthy):
        self.ticks.append(healthy)


def test_disabled_serial_watchdog_does_not_reconnect_quiet_device():
    device = FakeSerialConnection()
    device._last_activity = time.time() - 10000
    state = make_test_state(device=device)
    state.config['serial']['watchdog_timeout'] = 0
    state.mqtt_manager = LoopManager(healthy=False)
    health = LoopHealth()

    def unexpected_connector(config, *, expected_public_key=None):
        raise AssertionError('Disabled watchdog reconnected quiet device')

    def finish_loop(delay):
        state.should_exit = True

    _run_main_loop(state, health, connector=unexpected_connector, pause=finish_loop)
    assert device.is_open
    assert health.ticks == [False]


def test_main_loop_drains_a_bounded_batch_without_network_calls():
    device = FakeSerialConnection(lines=['ordinary log'] * 130)
    state = make_test_state(device=device)
    state.mqtt_manager = LoopManager()
    health = LoopHealth()

    def finish_loop(delay):
        state.should_exit = True

    _run_main_loop(state, health, pause=finish_loop)
    assert len(device._lines) == 66
    assert health.ticks == [True]


def test_startup_failure_closes_serial_and_cleans_up():
    device = FakeSerialConnection(name=None)
    state = make_test_state(device=device, auth=FakeAuthProvider())
    state.mqtt_manager = LoopManager()
    run(state)
    assert not device.is_open
    assert state.mqtt_manager.stopped


def test_closed_serial_reopens_even_with_idle_watchdog_disabled():
    old = FakeSerialConnection()
    old.close()
    new = FakeSerialConnection()
    state = make_test_state(device=old, repeater_pub_key='AA' * 32)
    state.config['serial']['watchdog_timeout'] = 0
    state.mqtt_manager = LoopManager()
    health = LoopHealth()
    clock = Clock()
    connections = []
    ticks = []

    def connector(config, *, expected_public_key=None):
        assert expected_public_key == state.repeater_pub_key
        connections.append(config)
        return new

    def pause(delay):
        ticks.append(delay)
        if len(ticks) == 2:
            state.should_exit = True

    _run_main_loop(state, health, connector=connector, pause=pause, clock=clock)
    assert len(connections) == 1
    assert state.device is new


@pytest.mark.parametrize('public_key', ['CC' * 32, None])
def test_reconnect_rejects_wrong_or_unverifiable_radio(public_key):
    device = FakeSerialConnection(pubkey=public_key)
    state = make_test_state(repeater_pub_key='AA' * 32)
    assert _reconnect_device(state, lambda config, **kwargs: device) is None
    assert not device.is_open


def test_reconnect_verification_exception_closes_new_session():
    class FailingDevice(FakeSerialConnection):
        def get_pubkey(self):
            raise OSError('Disconnected during identity verification')

    device = FailingDevice()
    state = make_test_state(repeater_pub_key='AA' * 32)
    with pytest.raises(OSError):
        _reconnect_device(state, lambda config, **kwargs: device)
    assert not device.is_open


def test_service_has_real_watchdog_and_resource_bounds():
    config = configparser.ConfigParser(interpolation=None)
    config.read(Path(__file__).parent.parent / 'mctomqtt.service')
    service = config['Service']
    assert service['Type'] == 'notify'
    assert service['WatchdogSec'] == '180'
    assert service['MemoryMax'] == '256M'
    assert service['MemorySwapMax'] == '0'
    assert service['TasksMax'] == '64'


def test_docker_command_limits_resources_and_rotates_logs():
    parts = docker_run_command('/etc/custom config', 'mctomqtt:test')
    assert '--memory=256m' in parts
    assert '--memory-swap=256m' in parts
    assert '--pids-limit=64' in parts
    assert '--log-driver=json-file' in parts
    assert '--log-opt=max-size=10m' in parts
    assert '--log-opt=max-file=3' in parts
    assert parts[-1] == 'mctomqtt:test'
    assert parts[-2] == '/etc/custom config:/etc/mctomqtt:ro'


@pytest.mark.parametrize('repo,branch,local_install,image', [
    ('Cisien/meshcoretomqtt', 'main', '', 'ghcr.io/cisien/meshcoretomqtt:latest'),
    ('cisien/meshcoretomqtt', 'main', '', 'ghcr.io/cisien/meshcoretomqtt:latest'),
    ('mikecarper/meshcoretomqtt', 'main', '', None),
    ('Cisien/meshcoretomqtt', 'fix/usb-host-resilience', '', None),
    ('Cisien/meshcoretomqtt', 'main', '/local/checkout', None),
])
def test_docker_registry_image_cannot_replace_selected_sources(repo, branch, local_install, image):
    context = InstallerContext(repo=repo, branch=branch, local_install=local_install)
    assert _docker_registry_image(context) == image
