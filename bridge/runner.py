"""Main run loop and startup orchestration."""
from __future__ import annotations

import json
import logging
import os
import subprocess
import threading
import time
from time import sleep
from typing import Any, TYPE_CHECKING

from config_loader import log_config_sources

from . import serial_connection
from . import message_parser
from . import background
from .auth_provider import MeshCoreAuthProvider
from .mqtt_publish import publish_shutdown_status
from .service_health import ServiceHealth

if TYPE_CHECKING:
    from .state import BridgeState

logger = logging.getLogger(__name__)


def load_client_version(version: str) -> str:
    """Load client version from provided version string, optionally append git hash."""
    try:
        script_dir = os.path.dirname(os.path.abspath(__file__))
        parent_dir = os.path.dirname(script_dir)  # bridge/ → project root
        version_file = os.path.join(parent_dir, '.version_info')
        if os.path.exists(version_file):
            with open(version_file, 'r') as f:
                version_data = json.load(f)
                git_hash = version_data.get('git_hash', '')
                if git_hash and git_hash != 'unknown':
                    return f"meshcoretomqtt/{version}-{git_hash}"
    except Exception as e:
        logger.debug(f"Could not load version info: {e}")
    return f"meshcoretomqtt/{version}"


def handle_signal(state: BridgeState, signum: int, frame: Any) -> None:
    """Signal handler to trigger graceful shutdown."""
    logger.info(f"Received signal {signum}, shutting down...")
    state.should_exit = True


def wait_for_system_time_sync(state: BridgeState) -> bool:
    """Wait up to 60 seconds for system clock synchronization via timedatectl."""
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline and not state.should_exit:
        try:
            result = subprocess.run(
                ['timedatectl', 'status'],
                capture_output=True,
                text=True,
                timeout=max(0.1, min(10, deadline - time.monotonic()))
            )
        except FileNotFoundError:
            logger.warning("timedatectl not found — skipping sync check and continuing.")
            return True
        except Exception as e:
            logger.warning("Error checking time sync (%s). Continuing.", e)
            return True

        if "System clock synchronized: yes" in result.stdout:
            return True
        logger.warning("System clock is not synchronized: %s",
                       result.stderr.strip() or result.stdout.strip())
        time.sleep(min(1, max(0, deadline - time.monotonic())))

    logger.warning("Timed out waiting for system clock sync — continuing anyway.")
    return True


def _initialize_device(state: BridgeState) -> bool:
    """Initialize identity before starting network workers."""
    log_config_sources(state.config)

    # Connect serial
    if state.device is None:
        state.device = serial_connection.connect(state.config)
    if not state.device:
        return False

    # Set up auth provider
    if state.auth is None:
        state.auth = MeshCoreAuthProvider()

    # Time sync
    if state.sync_time_at_start:
        wait_for_system_time_sync(state)
        if state.should_exit:
            return False
        state.device.set_time()

    # Query device info
    state.repeater_name = state.device.get_name()
    if not state.repeater_name:
        logger.error("Failed to get repeater name")
        return False

    state.repeater_pub_key = state.device.get_pubkey()
    if not state.repeater_pub_key:
        logger.error("Failed to get the repeater id (public key)")
        return False

    state.repeater_priv_key = state.device.get_privkey()
    if not state.repeater_priv_key:
        logger.warning("Failed to get repeater private key - auth token authentication will not be available")

    state.radio_info = state.device.get_radio_info()
    if not state.radio_info:
        logger.error("Failed to get radio info")
        return False

    state.firmware_version = state.device.get_firmware_version()
    if not state.firmware_version:
        logger.warning("Failed to get firmware version - will continue without it")

    state.model = state.device.get_board_type()
    if not state.model:
        logger.warning("Failed to get board type - will continue without it")

    # Get initial device stats
    device_stats = state.device.get_device_stats()
    if device_stats:
        state.stats['device'] = device_stats
        state.stats['device_prev'] = device_stats.copy()
        logger.info(f"Device stats: {device_stats}")
    else:
        logger.debug("Device stats not available (firmware may not support stats commands)")

    logger.info(f"Client version: {state.client_version}")

    # Log remote serial configuration
    if state.remote_serial_enabled:
        if state.remote_serial_allowed_companions:
            logger.info(f"Remote serial: ENABLED ({len(state.remote_serial_allowed_companions)} companion(s) allowed)")
            for pubkey in sorted(state.remote_serial_allowed_companions):
                logger.debug(f"  Allowed companion: {pubkey[:16]}...")
        else:
            logger.warning("Remote serial: ENABLED but no companions configured (will reject all commands)")
        if state.remote_serial_disallowed_commands:
            logger.info(f"Remote serial blocked commands: {state.remote_serial_disallowed_commands}")
    else:
        logger.info("Remote serial: DISABLED")

    return not state.should_exit


def run(state: BridgeState) -> None:
    """Keep USB consumption independent of broker setup and recovery."""
    health = ServiceHealth()
    stats_thread = None
    try:
        if not _initialize_device(state):
            return
        state.mqtt_manager.start()

        # Start stats logging thread
        stats_thread = threading.Thread(
            target=background.stats_logging_loop,
            args=(state,),
            daemon=True,
            name="Stats-Logger"
        )
        stats_thread.start()
        health.ready()
        _run_main_loop(state, health)
    except KeyboardInterrupt:
        logger.info("Exiting...")
    except Exception:
        logger.exception("Unhandled error in bridge")
    finally:
        health.stopping()
        _cleanup(state, stats_thread)


def _run_main_loop(state: BridgeState, health: ServiceHealth, *,
                   connector=serial_connection.connect, pause=sleep,
                   clock=time.monotonic) -> None:
    """Drain buffered USB data; no synchronous MQTT/DNS operations here."""

    # Serial watchdog: force reconnect if no data received for this many seconds
    serial_cfg = state.config.get('serial', {})
    watchdog_timeout = float(serial_cfg.get('watchdog_timeout', 900))
    if not 0 <= watchdog_timeout < float('inf'):
        raise ValueError('serial.watchdog_timeout must be finite and non-negative')
    watchdog_logged = False
    last_reconnect_attempt = 0.0
    reconnect_interval = 5  # seconds between retry attempts

    while not state.should_exit:
        received = False
        try:
            if state.device:
                if not state.device.is_open:
                    raise OSError('Serial connection is closed')
                # Bound each batch so shutdown/watchdog work still gets time.
                for _ in range(64):
                    line = state.device.read_line()
                    if not line:
                        break
                    received = True
                    message_parser.parse_and_publish(state, line)
                    watchdog_logged = False

                # A disabled watchdog must not reconnect a quiet serial port.
                if (not received and watchdog_timeout > 0
                        and state.device.seconds_since_activity() > watchdog_timeout):
                    if not watchdog_logged:
                        logger.warning(
                            f"Serial watchdog: no data received for "
                            f"{int(state.device.seconds_since_activity())}s "
                            f"(threshold: {watchdog_timeout}s), forcing reconnect"
                        )
                    state.device.close()
                    state.last_raw = None
                    state.last_raw_stamp = None
                    state.device = _reconnect_device(state, connector)
                    if state.device:
                        logger.info("Serial watchdog: reconnected successfully")
                        watchdog_logged = False
                    else:
                        watchdog_logged = True
            else:
                now = clock()
                if now - last_reconnect_attempt >= reconnect_interval:
                    last_reconnect_attempt = now
                    state.device = _reconnect_device(state, connector)
                    if state.device:
                        logger.info("Serial reconnected successfully")
                        watchdog_logged = False
                    elif not watchdog_logged:
                        logger.warning("Serial device unavailable, retrying every %ds", reconnect_interval)
                        watchdog_logged = True

        except OSError:
            logger.warning("Serial connection unavailable, trying to reconnect")
            if state.device:
                state.device.close()
            state.last_raw = None
            state.last_raw_stamp = None
            state.device = None
            last_reconnect_attempt = clock() - reconnect_interval

        health.tick(state.mqtt_manager.is_healthy())
        pause(0.001 if received else 0.01)


def _reconnect_device(state: BridgeState, connector):
    """Never attribute a different radio's packets to the startup identity."""
    device = connector(state.config, expected_public_key=state.repeater_pub_key)
    if device is None:
        return None
    if state.repeater_pub_key:
        try:
            public_key = device.get_pubkey()
        except Exception:
            # This new session is not yet stored in state; outer cleanup only
            # knows the old device. Release its reader/exclusive FD here.
            device.close()
            raise
        if public_key != state.repeater_pub_key:
            logger.error('Serial reconnect identity changed or could not be verified; refusing capture')
            device.close()
            return None
    return device


def _cleanup(state: BridgeState, stats_thread: threading.Thread | None) -> None:
    """Shut down background threads, publish offline status, and close connections."""
    logger.info("Cleaning up...")
    state.should_exit = True
    if state.mqtt_manager:
        begin_shutdown = getattr(state.mqtt_manager, 'begin_shutdown', None)
        if begin_shutdown is not None:
            begin_shutdown()

    # Wait for stats thread to finish
    if stats_thread is not None and stats_thread.is_alive():
        stats_thread.join(timeout=5)

    # Bound the combined PUBACK waits even with many brokers. Unconfirmed
    # transports abort before manager.stop(), preserving the broker's LWT.
    deadline = time.monotonic() + 5.0
    for mqtt_info in list(state.mqtt_clients):
        client = mqtt_info.get('client')
        if client is not None:
            # A pending CONNACK can establish a broker-side session even when
            # our slot is not connected. Abort it without DISCONNECT so its
            # offline will is not suppressed during cleanup.
            timeout = (min(2.0, max(0.0, deadline - time.monotonic()))
                       if mqtt_info.get('connected') else 0.0)
            mqtt_info['shutdown_confirmed'] = publish_shutdown_status(
                state, client, mqtt_info['broker_idx'], timeout=timeout)

    # The lifecycle owner retires clients and invalidates stale callbacks.
    if state.mqtt_manager:
        state.mqtt_manager.stop()

    # Close serial connection
    if state.device:
        state.device.close()
