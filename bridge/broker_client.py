"""MQTT broker client abstraction."""
from __future__ import annotations

import logging
import math
import ssl
import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Callable

import paho.mqtt.client as mqtt

logger = logging.getLogger(__name__)


@dataclass(eq=False)
class _PublishReservation:
    size: int
    started_at: float
    completion: Callable[[], bool] | None = None


class _PendingPublishes:
    """A bounded budget shared by publishers and Paho's completion callback.

    Reserve before calling Paho, without holding this lock across publish().
    QoS 0 can complete inside publish(), before its receipt has been returned.
    Reconcile public receipt state instead of relying on callback/MID order:
    Paho sets is_published() after on_publish returns, and MIDs can be reused.
    """

    def __init__(self, max_messages: int, max_bytes: int, timeout: float) -> None:
        self.max_messages = max_messages
        self.max_bytes = max_bytes
        self.timeout = timeout
        self._lock = threading.Lock()
        self._reservations: set[_PublishReservation] = set()
        self._pending: set[_PublishReservation] = set()
        self._count = 0
        self._bytes = 0
        self._accepted = 0
        self._completed = 0
        self._rejected = 0
        self._errors = 0

    def reserve(self, size: int) -> _PublishReservation | None:
        with self._lock:
            self._reap_completed()
            if self._count >= self.max_messages or size > self.max_bytes - self._bytes:
                self._rejected += 1
                return None
            reservation = _PublishReservation(size, time.monotonic())
            self._reservations.add(reservation)
            self._count += 1
            self._bytes += size
            return reservation

    def reject(self) -> None:
        with self._lock:
            self._rejected += 1

    def _release(self, reservation: _PublishReservation) -> None:
        self._count -= 1
        self._bytes -= reservation.size

    def _reap_completed(self) -> None:
        for reservation in tuple(self._pending):
            try:
                completed = reservation.completion is not None and reservation.completion()
            except (ValueError, RuntimeError):
                # A failed Paho publish may retain QoS > 0 data despite its
                # error result. Keep the conservative budget until retirement.
                completed = False
            if completed:
                self._pending.remove(reservation)
                self._release(reservation)
                self._completed += 1

    def finish(
        self,
        reservation: _PublishReservation,
        accepted: bool,
        completion: Callable[[], bool] | None = None,
        may_be_queued: bool = False,
    ) -> None:
        with self._lock:
            self._reservations.remove(reservation)
            if accepted:
                self._accepted += 1
            else:
                self._errors += 1
            if accepted or may_be_queued:
                reservation.completion = completion
                self._pending.add(reservation)
                self._reap_completed()
            else:
                self._release(reservation)

    def complete(self) -> None:
        with self._lock:
            self._reap_completed()

    @property
    def stalled(self) -> bool:
        with self._lock:
            self._reap_completed()
            now = time.monotonic()
            return any(now - item.started_at >= self.timeout for item in self._reservations | self._pending)

    def snapshot(self) -> dict[str, int]:
        with self._lock:
            self._reap_completed()
            return {
                'pending_messages': self._count,
                'pending_bytes': self._bytes,
                'accepted': self._accepted,
                'completed': self._completed,
                'rejected': self._rejected,
                'errors': self._errors,
            }


class BrokerClient(ABC):
    """Abstract interface for a single MQTT broker connection."""

    @abstractmethod
    def connect(self, server: str, port: int, keepalive: int = 60) -> None: ...

    @abstractmethod
    def disconnect(self) -> None: ...

    @abstractmethod
    def publish(self, topic: str, payload: str, qos: int = 0, retain: bool = False) -> bool: ...

    @abstractmethod
    def subscribe(self, topic: str, qos: int = 0) -> None: ...

    @abstractmethod
    def loop_start(self) -> None: ...

    @abstractmethod
    def loop_stop(self) -> None: ...

    @property
    @abstractmethod
    def is_connected(self) -> bool: ...


class PahoBrokerClient(BrokerClient):
    """Concrete implementation wrapping paho.mqtt.client.Client."""

    def __init__(
        self,
        client_id: str,
        transport: str = 'tcp',
        username: str | None = None,
        password: str | None = None,
        lwt_topic: str | None = None,
        lwt_payload: str | None = None,
        lwt_qos: int = 0,
        lwt_retain: bool = True,
        tls_enabled: bool = False,
        tls_verify: bool = True,
        on_connect: Callable[..., Any] | None = None,
        on_disconnect: Callable[..., Any] | None = None,
        on_message: Callable[..., Any] | None = None,
        userdata: dict[str, Any] | None = None,
        max_pending_messages: int = 256,
        max_pending_bytes: int = 262144,
        connect_timeout: float = 30.0,
        publish_timeout: float = 120.0,
    ) -> None:
        if (
            isinstance(max_pending_messages, bool)
            or not isinstance(max_pending_messages, int)
            or not 1 <= max_pending_messages <= 65535
        ):
            raise ValueError("max_pending_messages must be an integer from 1 to 65535")
        if (
            isinstance(max_pending_bytes, bool)
            or not isinstance(max_pending_bytes, int)
            or max_pending_bytes <= 0
        ):
            raise ValueError("max_pending_bytes must be a positive integer")
        for name, value in (("connect_timeout", connect_timeout), ("publish_timeout", publish_timeout)):
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or value <= 0
            ):
                raise ValueError(f"{name} must be a finite positive number")
        self._pending = _PendingPublishes(max_pending_messages, max_pending_bytes, publish_timeout)
        self._client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id=client_id,
            clean_session=True,
            transport=transport,
            reconnect_on_failure=False,
        )
        self._client.connect_timeout = connect_timeout
        self._client.max_queued_messages_set(max_pending_messages)
        self._client.max_inflight_messages_set(min(20, max_pending_messages))
        self._client.on_publish = self._on_publish

        if userdata:
            self._client.user_data_set(userdata)

        if username:
            self._client.username_pw_set(username, password)

        if lwt_topic:
            self._client.will_set(lwt_topic, lwt_payload, qos=lwt_qos, retain=lwt_retain)

        if on_connect:
            self._client.on_connect = on_connect
        if on_disconnect:
            self._client.on_disconnect = on_disconnect
        if on_message:
            self._client.on_message = on_message

        if tls_enabled:
            if tls_verify:
                self._client.tls_set(cert_reqs=ssl.CERT_REQUIRED)
                self._client.tls_insecure_set(False)
            else:
                self._client.tls_set(cert_reqs=ssl.CERT_NONE)
                self._client.tls_insecure_set(True)

        if transport == "websockets":
            self._client.ws_set_options(path="/", headers=None)

    def connect(self, server: str, port: int, keepalive: int = 60) -> None:
        self._client.connect(server, port, keepalive=keepalive)

    def disconnect(self) -> None:
        self._client.disconnect()

    def disconnect_gracefully(self, *, timeout: float = 1.0) -> bool:
        """Give the network loop a bounded chance to send DISCONNECT.

        Paho queues DISCONNECT when its background loop is running. Closing
        the socket immediately after disconnect() would trigger the broker's
        will and overwrite a confirmed final status. Socket closure is public
        lifecycle evidence; an expired wait still permits forced retirement.
        """
        if (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
                or not math.isfinite(timeout) or timeout < 0):
            raise ValueError('timeout must be a finite nonnegative number')
        self.disconnect()
        deadline = time.monotonic() + timeout
        while self._client.socket() is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            time.sleep(min(0.005, remaining))
        return True

    def abort(self) -> None:
        """Close a retired transport without waiting behind queued publishes.

        Close before DISCONNECT when preserving the broker's will, or after
        a graceful attempt expires. Paho queues DISCONNECT behind outstanding
        QoS 0 data, so loop_stop() can otherwise wait for a nonreading peer's
        keepalive timeout. TCP, TLS and WebSocket transports expose close()
        through Paho's public socket() API. Retired logs are best effort.
        """
        transport = self._client.socket()
        if transport is not None:
            try:
                transport.close()
            except OSError:
                logger.debug('Error closing retired MQTT transport', exc_info=True)

    def publish(self, topic: str, payload: str, qos: int = 0, retain: bool = False) -> bool:
        """Return acceptance, not delivery; refuse offline or over-budget data.

        Budget UTF-8 topic and payload bytes plus conservative wire overhead.
        This also limits QoS 0, which Paho's max_queued_messages_set does not.
        Completion means socket transmission for QoS 0 and acknowledgment for
        QoS 1/2. The supervisor retires connections with expired publications.
        """
        return self._publish_receipt(topic, payload, qos, retain) is not None

    def _publish_receipt(self, topic: str, payload: str, qos: int, retain: bool) -> mqtt.MQTTMessageInfo | None:
        """Reserve the shared budget and return only an accepted public receipt."""
        if not self.is_connected:
            self._pending.reject()
            return None
        size = len(topic.encode('utf-8')) + len(payload.encode('utf-8')) + 32
        reservation = self._pending.reserve(size)
        if reservation is None:
            return None
        try:
            result = self._client.publish(topic, payload, qos=qos, retain=retain)
        except (ValueError, TypeError):
            self._pending.finish(reservation, False)
            raise
        except Exception:
            # Do not under-account a transport failure after Paho might have
            # enqueued the data. An opaque reservation expires normally.
            self._pending.finish(reservation, False, may_be_queued=True)
            raise
        accepted = result.rc == mqtt.MQTT_ERR_SUCCESS
        self._pending.finish(
            reservation,
            accepted,
            result.is_published,
            may_be_queued=result.rc != mqtt.MQTT_ERR_QUEUE_SIZE,
        )
        return result if accepted else None

    def publish_confirmed(
        self, topic: str, payload: str, qos: int = 1, retain: bool = False,
        *, timeout: float = 2.0,
    ) -> bool:
        """Wait a bounded time for this receipt, never from a Paho callback.

        QoS 1/2 requires broker acknowledgment; QoS 0 proves socket handoff
        only. Failure must not be treated as permission to suppress the LWT.
        The optional method leaves external BrokerClient implementations valid.
        """
        if isinstance(timeout, bool) or not math.isfinite(timeout) or timeout < 0:
            raise ValueError('timeout must be a finite nonnegative number')
        if timeout == 0:
            return False
        receipt = self._publish_receipt(topic, payload, qos, retain)
        if receipt is None:
            return False
        try:
            receipt.wait_for_publish(timeout=timeout)
            return receipt.is_published()
        except (ValueError, RuntimeError):
            return False
        finally:
            self._pending.complete()

    def _on_publish(self, client: Any, userdata: Any, mid: int, reason_code: Any, properties: Any) -> None:
        self._pending.complete()

    def subscribe(self, topic: str, qos: int = 0) -> None:
        result = self._client.subscribe(topic, qos=qos)
        if result[0] != mqtt.MQTT_ERR_SUCCESS:
            raise Exception(f"Subscribe failed: {mqtt.error_string(result[0])}")

    def loop_start(self) -> None:
        self._client.loop_start()

    def loop_stop(self) -> None:
        self._client.loop_stop()

    @property
    def is_connected(self) -> bool:
        return self._client.is_connected()

    @property
    def pending_messages(self) -> int:
        return self._pending.snapshot()['pending_messages']

    @property
    def pending_bytes(self) -> int:
        return self._pending.snapshot()['pending_bytes']

    @property
    def publish_stalled(self) -> bool:
        return self._pending.stalled

    @property
    def publish_stats(self) -> dict[str, int]:
        return self._pending.snapshot()

    @property
    def raw_client(self) -> mqtt.Client:
        """Access Paho for connection identity and public diagnostic APIs."""
        return self._client
