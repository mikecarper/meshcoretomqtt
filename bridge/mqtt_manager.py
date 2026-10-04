"""MQTT connection manager with one bounded connection supervisor."""
from __future__ import annotations

import json
import logging
import random
import threading
import time
from typing import Any, Callable, TYPE_CHECKING

from . import topics
from . import remote_serial
from .broker_client import BrokerClient, PahoBrokerClient
from .mqtt_publish import build_status_message

if TYPE_CHECKING:
    from .state import BridgeState

logger = logging.getLogger(__name__)


class MqttManager:
    """Orchestrates multiple MQTT broker connections."""

    def __init__(
        self,
        state: BridgeState,
        *,
        clock: Callable[[], float] = time.monotonic,
        jitter: Callable[[float, float], float] = random.uniform,
        client_factory: Callable[..., BrokerClient] = PahoBrokerClient,
    ) -> None:
        self.state = state
        self._clock = clock
        self._jitter = jitter
        self._client_factory = client_factory
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.last_progress_monotonic = self._clock()
        self.connection_grace = 10.0
        self.stable_connection_seconds = 120.0

    # ------------------------------------------------------------------
    # Connection lifecycle
    # ------------------------------------------------------------------

    def _ensure_slots(self) -> None:
        """Allocate exactly one persistent slot per enabled broker."""
        with self._lock:
            existing = {info['broker_idx'] for info in self.state.mqtt_clients}
            for index, broker in enumerate(self.state.config.get('broker', [])):
                if not broker.get('enabled', False) or index in existing:
                    continue
                self.state.connection_events[index] = threading.Event()
                self.state.mqtt_clients.append({
                    'client': None,
                    'broker_idx': index,
                    'server': broker.get('server', ''),
                    'port': broker.get('port', 1883),
                    'connected': False,
                    'connecting_since': None,
                    'connect_time': None,
                    'reconnect_at': 0.0,
                    'reconnect_delay': 1.0,
                    'failed_attempts': 0,
                    'generation': 0,
                    'attempt_failed': False,
                    'stability_reset': False,
                })

    def start(self) -> None:
        """Run all potentially blocking connection work off the USB reader."""
        with self._lock:
            if self._stop.is_set():
                raise RuntimeError('MQTT manager cannot restart after stop')
            if self._thread and self._thread.is_alive():
                return
            self._ensure_slots()
            self.last_progress_monotonic = self._clock()
            self._thread = threading.Thread(
                target=self._supervise, name='MQTT-Supervisor', daemon=True,
            )
            self._thread.start()

    def is_healthy(self, max_stall: float = 120.0) -> bool:
        """Liveness, not broker availability; DNS itself is not cancellable."""
        thread = self._thread
        if thread is None:
            return not self._stop.is_set()
        return bool(thread and thread.is_alive()
                    and self._clock() - self.last_progress_monotonic <= max_stall)

    def _supervise(self) -> None:
        try:
            if not self.state.mqtt_clients:
                logger.error('[MQTT] No enabled brokers configured')
                self.state.should_exit = True
                return
            while not self._stop.is_set():
                if self.state.should_exit:
                    # The runner confirms offline status before stop(). Do
                    # not reconnect or close its transports during that wait.
                    self._stop.wait(0.1)
                    continue
                self.last_progress_monotonic = self._clock()
                self.reconnect_disconnected_brokers()
                self.last_progress_monotonic = self._clock()
                self._stop.wait(0.1)
        except Exception:
            logger.exception('[MQTT] Connection supervisor failed')
            self.state.should_exit = True
        finally:
            self._close_all_clients()

    def stop(self, timeout: float = 5.0) -> bool:
        """Invalidate callbacks and wait at most timeout seconds for cleanup."""
        self._stop.set()
        with self._lock:
            for info in self.state.mqtt_clients:
                info['generation'] = info.get('generation', 0) + 1
                info['connected'] = False
            self.state.mqtt_connected = False
            thread = self._thread
            if thread is None:
                # The synchronous compatibility API also gets bounded cleanup.
                thread = threading.Thread(
                    target=self._close_all_clients,
                    name='MQTT-Cleanup', daemon=True,
                )
                self._thread = thread
                thread.start()
        if thread is not threading.current_thread():
            thread.join(timeout=max(0.0, timeout))
        return not thread.is_alive()

    def begin_shutdown(self) -> None:
        """Finish any online initialization before the final offline update.

        A successful CONNACK callback may already be publishing its retained
        online status when a signal requests exit. Serialize that publication
        with cleanup so it cannot overwrite the subsequently confirmed offline
        status, and reject any new CONNACK side effects after this point.
        """
        with self._lock:
            self.state.should_exit = True

    @staticmethod
    def _close_client(
        client: BrokerClient | None, *, preserve_lwt: bool = False,
        graceful_timeout: float | None = None,
    ) -> None:
        if client is None:
            return
        abort = getattr(client, 'abort', None)
        aborted = False
        if preserve_lwt and abort is not None:
            try:
                # A cancelled CONNECT may have reached the broker even before
                # its CONNACK was processed. Close first so DISCONNECT cannot
                # suppress the offline will after cleanup skipped that slot.
                abort()
                aborted = True
            except Exception:
                logger.debug('[MQTT] Error preserving retired client will', exc_info=True)
        try:
            # Disconnect first: loop_stop alone does not close the socket.
            graceful = getattr(client, 'disconnect_gracefully', None)
            if graceful_timeout is not None and graceful is not None:
                graceful(timeout=graceful_timeout)
            else:
                client.disconnect()
        except Exception:
            logger.debug('[MQTT] Error disconnecting retired client', exc_info=True)
        try:
            # A non-reading peer can leave DISCONNECT behind queued QoS 0 data.
            # Force the transport closed before loop_stop joins Paho's worker.
            if abort is not None and not aborted:
                abort()
        except Exception:
            logger.debug('[MQTT] Error aborting retired transport', exc_info=True)
        try:
            client.loop_stop()
        except Exception:
            logger.debug('[MQTT] Error stopping retired client', exc_info=True)

    def _close_all_clients(self) -> None:
        with self._lock:
            clients = []
            for info in self.state.mqtt_clients:
                info['generation'] = info.get('generation', 0) + 1
                info['connected'] = False
                clients.append((info.get('client'), info.get('shutdown_confirmed', False)))
                info['client'] = None
            self.state.mqtt_connected = False
        # Share one short DISCONNECT window across all confirmed brokers. An
        # unconfirmed session must preserve its will without a graceful send.
        graceful_deadline = time.monotonic() + 1.0
        for client, confirmed in clients:
            self._close_client(
                client, preserve_lwt=not confirmed,
                graceful_timeout=max(0.0, graceful_deadline - time.monotonic())
                if confirmed else None,
            )

    def connect_all_brokers(self) -> bool:
        """Synchronous compatibility entry point without duplicate clients."""
        self._ensure_slots()
        self.reconnect_disconnected_brokers()
        deadline = time.monotonic() + self.connection_grace
        while not self._stop.is_set() and not self.state.should_exit:
            events = list(self.state.connection_events.values())
            if not events or all(event.is_set() for event in events):
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            self._stop.wait(min(0.05, remaining))
        if not self.state.mqtt_connected:
            with self._lock:
                for info in self.state.mqtt_clients:
                    if info.get('connecting_since') is not None:
                        self._record_failure(info, self._clock(), 'CONNACK timeout')
            self._close_all_clients()
        return self.state.mqtt_connected

    def _reset_if_stable(self, info: dict[str, Any], now: float) -> bool:
        since = info.get('connect_time')
        if (info.get('connected') and since is not None
                and now - since >= self.stable_connection_seconds):
            if not info.get('stability_reset', False):
                info['failed_attempts'] = 0
                info['reconnect_delay'] = 1.0
                info['stability_reset'] = True
            return True
        return False

    def _record_failure(self, info: dict[str, Any], now: float, reason: str) -> None:
        """Count a failed generation once, including reject then disconnect."""
        if info.get('attempt_failed', False):
            return
        self._reset_if_stable(info, now)
        info['attempt_failed'] = True
        info['connected'] = False
        info['connecting_since'] = None
        info['failed_attempts'] = info.get('failed_attempts', 0) + 1
        delay = info.get('reconnect_delay', 1.0)
        info['reconnect_at'] = now + max(0.0, delay + self._jitter(-0.5, 0.5))
        info['reconnect_delay'] = min(delay * self.state.reconnect_backoff,
                                      self.state.max_reconnect_delay)
        self.state.mqtt_connected = any(
            candidate.get('connected', False) for candidate in self.state.mqtt_clients
        )
        event = self.state.connection_events.get(info['broker_idx'])
        if event:
            event.set()
        broker = topics.get_broker_config(self.state, info['broker_idx'])
        logger.warning('[%s] %s (failure %s/%s)', broker.get('name', info['broker_idx']),
                       reason, info['failed_attempts'], self.state.max_reconnect_attempts)
        if info['failed_attempts'] >= self.state.max_reconnect_attempts:
            logger.critical('[MQTT] Failure limit reached; exiting for service restart')
            self.state.should_exit = True

    def reconnect_disconnected_brokers(self) -> None:
        """Supervise slots; callers must keep this off the USB reader thread."""
        self._ensure_slots()
        state = self.state
        for info in list(state.mqtt_clients):
            self.last_progress_monotonic = self._clock()
            if self._stop.is_set() or state.should_exit:
                return
            with self._lock:
                now = self._clock()
                if info.get('connected', False):
                    self._reset_if_stable(info, now)
                    client = info.get('client')
                    if client and getattr(client, 'publish_stalled', False):
                        self._record_failure(info, now, 'publish progress timeout')
                    else:
                        continue
                since = info.get('connecting_since')
                if since is not None:
                    if now - since < self.connection_grace:
                        continue
                    self._record_failure(info, now, 'CONNACK timeout')
                retry_due = not state.should_exit and now >= info.get('reconnect_at', 0)
                if not retry_due and not info.get('attempt_failed', False):
                    continue
                old_client = info.get('client')
                info['client'] = None
                if old_client is not None or retry_due:
                    info['generation'] = info.get('generation', 0) + 1
                if retry_due:
                    state.token_cache.pop(info['broker_idx'], None)
            # A failed session is no longer usable, including during backoff.
            # Abort before DISCONNECT so a publish/CONNACK timeout cannot leave
            # its retained online status behind by suppressing the offline will.
            self._close_client(old_client, preserve_lwt=True)
            if not retry_due:
                continue
            fresh = self._create_and_connect_broker(info['broker_idx'])
            if fresh:
                with self._lock:
                    if self._stop.is_set() or state.should_exit:
                        continue
                    fresh['client'].loop_start()

    # ------------------------------------------------------------------
    # MQTT callbacks
    # ------------------------------------------------------------------

    def on_mqtt_connect(self, client: Any, userdata: dict[str, Any] | None, flags: Any, rc: int, properties: Any = None) -> None:
        state = self.state
        broker_name = userdata.get('name', 'unknown') if userdata else 'unknown'
        with self._lock:
            info = self._current_callback(client, userdata)
            if (info is None or info.get('attempt_failed', False)
                    or state.should_exit):
                return
            broker_idx = info['broker_idx']
            if rc != 0:
                self._record_failure(info, self._clock(), f'CONNACK rejected: {rc}')
                return
            if info.get('connected', False):
                return
            info['connected'] = True
            info['connecting_since'] = None
            info['connect_time'] = self._clock()
            info['stability_reset'] = False
            state.mqtt_connected = True
            state.connection_events[broker_idx].set()
            broker_client = info['client']
            logger.info('[%s] Connected to broker', broker_name)
            broker = topics.get_broker_config(state, broker_idx)
            try:
                broker_client.publish(
                    topics.get_topic(state, 'status', broker_idx),
                    json.dumps(build_status_message(state, 'online')),
                    qos=broker.get('qos', 0), retain=broker.get('retain', True),
                )
                remote_serial.subscribe_serial_commands(state, broker_client, broker_idx)
            except Exception:
                logger.exception('[%s] Failed to initialize broker status/subscription', broker_name)

    def on_mqtt_disconnect(self, client: Any, userdata: dict[str, Any] | None, disconnect_flags: Any, reason_code: Any, properties: Any) -> None:
        with self._lock:
            info = self._current_callback(client, userdata)
            if info is None:
                return
            if self.state.should_exit:
                info['connected'] = False
                return
            was_connected = info.get('connected', False)
            self._record_failure(info, self._clock(), f'disconnected: {reason_code}')
            if was_connected:
                timestamps = self.state.stats['reconnects'].setdefault(info['broker_idx'], [])
                timestamps.append(time.time())

    def on_mqtt_message(self, client: Any, userdata: dict[str, Any] | None, msg: Any) -> None:
        """Handle incoming MQTT messages (for remote serial commands)."""
        state = self.state
        with self._lock:
            info = self._current_callback(client, userdata)
            if (info is None or not info.get('connected', False)
                    or state.should_exit):
                return
            broker_idx = info['broker_idx']
        topic = msg.topic

        if topic != remote_serial.get_serial_commands_topic(state, broker_idx):
            return

        broker = topics.get_broker_config(state, broker_idx) if broker_idx is not None else {}
        broker_name = broker.get('name', f'broker-{broker_idx}')
        logger.debug(f"[{broker_name}] Received message on {topic}")

        try:
            jwt_token = msg.payload.decode('utf-8').strip()
            remote_serial.handle_serial_command(state, jwt_token, broker_idx)
        except Exception as e:
            logger.error(f"[SERIAL] Failed to handle command: {e}")

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _current_callback(self, client: Any, userdata: dict[str, Any] | None) -> dict[str, Any] | None:
        """Old client callbacks cannot change a replacement generation."""
        if self._stop.is_set() or not userdata:
            return None
        for info in self.state.mqtt_clients:
            if (info['broker_idx'] != userdata.get('broker_idx')
                    or info.get('generation') != userdata.get('generation')):
                continue
            wrapper = info.get('client')
            expected = getattr(wrapper, 'raw_client', wrapper)
            if wrapper is not None and client is expected:
                return info
        return None

    def _generate_auth_credentials(self, broker_idx: int, force_refresh: bool = False) -> tuple[str | None, str | None]:
        """Generate authentication credentials for a broker on-demand."""
        state = self.state
        broker = topics.get_broker_config(state, broker_idx)
        auth = broker.get('auth', {})
        auth_method = auth.get('method', 'none')

        if auth_method == 'token':
            if not state.repeater_priv_key:
                logger.error(f"[{broker.get('name', broker_idx)}] Private key not available from device for auth token")
                return None, None

            current_time = time.time()
            if not force_refresh and broker_idx in state.token_cache:
                cached_token, created_at = state.token_cache[broker_idx]
                age = current_time - created_at
                if age < (state.token_ttl - 300):
                    logger.debug(f"[{broker.get('name', broker_idx)}] Using cached auth token (age: {age:.0f}s)")
                    username = f"v1_{state.repeater_pub_key.upper()}"
                    return username, cached_token

            try:
                username = f"v1_{state.repeater_pub_key.upper()}"
                audience = auth.get('audience', '')

                tls_cfg = broker.get('tls', {})
                use_tls = tls_cfg.get('enabled', False)
                tls_verify = tls_cfg.get('verify', True)
                secure_connection = use_tls and tls_verify

                owner = auth.get('owner', '')
                email = auth.get('email', '')

                claims: dict[str, Any] = {}
                if audience:
                    claims['aud'] = audience

                if secure_connection:
                    if owner:
                        claims['owner'] = owner
                    if email:
                        claims['email'] = email.lower()
                else:
                    if owner or email:
                        logger.debug(f"[{broker.get('name', broker_idx)}] Skipping email/owner in JWT - TLS and TLS verify must both be enabled")

                claims['client'] = state.client_version

                password = state.auth.create_token(state.repeater_pub_key, state.repeater_priv_key, expiry_seconds=state.token_ttl, **claims)
                state.token_cache[broker_idx] = (password, current_time)
                logger.debug(f"[{broker.get('name', broker_idx)}] Generated fresh auth token (1h expiry)")
                return username, password
            except Exception as e:
                logger.error(f"[{broker.get('name', broker_idx)}] Failed to generate auth token: {e}")
                return None, None
        elif auth_method == 'password':
            username = auth.get('username', '')
            password = auth.get('password', '')
            return username, password
        else:
            return '', ''

    def _create_broker_client(self, broker_idx: int) -> BrokerClient | None:
        """Create and configure a BrokerClient (doesn't connect)."""
        state = self.state
        broker = topics.get_broker_config(state, broker_idx)
        broker_name = broker.get('name', f'broker-{broker_idx}')

        # Build client ID
        prefix = broker.get('client_id_prefix', 'meshcore_')
        suffix = f"_{broker_idx}" if broker_idx > 0 else ""
        client_id = topics.sanitize_client_id(state.repeater_pub_key, prefix, suffix=suffix)

        transport = broker.get('transport', 'tcp')

        # Get credentials
        username, password = self._generate_auth_credentials(broker_idx)
        if username is None:
            return None

        # Build LWT
        lwt_topic = topics.get_topic(state, "status", broker_idx)
        lwt_payload = json.dumps(build_status_message(state, "offline", include_stats=False))
        lwt_qos = broker.get('qos', 0)
        lwt_retain = broker.get('retain', True)

        # TLS config
        tls_cfg = broker.get('tls', {})
        tls_enabled = tls_cfg.get('enabled', False)
        tls_verify = tls_cfg.get('verify', True)
        if tls_enabled and not tls_verify:
            logger.warning(f"[{broker_name}] TLS verification disabled")

        info = next(info for info in state.mqtt_clients if info['broker_idx'] == broker_idx)
        broker_client = self._client_factory(
            client_id=client_id,
            transport=transport,
            username=username if username else None,
            password=password,
            lwt_topic=lwt_topic,
            lwt_payload=lwt_payload,
            lwt_qos=lwt_qos,
            lwt_retain=lwt_retain,
            tls_enabled=tls_enabled,
            tls_verify=tls_verify,
            on_connect=self.on_mqtt_connect,
            on_disconnect=self.on_mqtt_disconnect,
            on_message=self.on_mqtt_message,
            userdata={'name': broker_name, 'broker_idx': broker_idx,
                      'generation': info['generation']},
            max_pending_messages=broker.get('max_pending_messages', 256),
            max_pending_bytes=broker.get('max_pending_bytes', 262144),
            connect_timeout=broker.get('connect_timeout', 30.0),
            publish_timeout=broker.get('publish_timeout', 120.0),
        )

        return broker_client

    def _create_and_connect_broker(self, broker_idx: int) -> dict[str, Any] | None:
        """Create a fresh generation without resetting its retry history."""
        state = self.state
        broker = topics.get_broker_config(state, broker_idx)
        broker_name = broker.get('name', f'broker-{broker_idx}')

        if not broker.get('enabled', False):
            logger.debug(f"[{broker_name}] Disabled, skipping")
            return None

        server = broker.get('server', '')
        port = broker.get('port', 1883)
        transport = broker.get('transport', 'tcp')
        keepalive = broker.get('keepalive', 60)
        tls_cfg = broker.get('tls', {})
        use_tls = tls_cfg.get('enabled', False)

        self._ensure_slots()
        with self._lock:
            if self._stop.is_set() or state.should_exit:
                return None
            info = next(info for info in state.mqtt_clients if info['broker_idx'] == broker_idx)
            generation = info.get('generation', 0) + 1
            info.update(generation=generation, connected=False,
                        connecting_since=self._clock(), connect_time=None,
                        attempt_failed=False, stability_reset=False)
            state.connection_events.setdefault(broker_idx, threading.Event()).clear()

        broker_client = None
        try:
            if not state.repeater_name:
                raise ValueError('repeater name unavailable')
            if not server:
                raise ValueError('broker server unavailable')
            broker_client = self._create_broker_client(broker_idx)
            if broker_client is None:
                raise RuntimeError('broker credentials unavailable')
            with self._lock:
                aborted = self._stop.is_set() or state.should_exit
                if not aborted:
                    info['client'] = broker_client
            if aborted:
                self._close_client(broker_client, preserve_lwt=True)
                return None
            broker_client.connect(server, port, keepalive=keepalive)
            with self._lock:
                aborted = (self._stop.is_set() or state.should_exit
                           or info['generation'] != generation)
                if not aborted:
                    # The CONNACK deadline starts after transport connection.
                    info['connecting_since'] = self._clock()
                elif info.get('client') is broker_client:
                    info['client'] = None
            if aborted:
                self._close_client(broker_client, preserve_lwt=True)
                return None
            logger.info('[%s] Connecting to %s:%s (transport=%s, tls=%s, keepalive=%ss)',
                        broker_name, server, port, transport, use_tls, keepalive)
            return info
        except Exception as error:
            with self._lock:
                if info.get('client') is broker_client:
                    info['client'] = None
                if info['generation'] == generation and not self._stop.is_set():
                    self._record_failure(info, self._clock(), f'transport connect failed: {error}')
            self._close_client(broker_client, preserve_lwt=True)
            return None
