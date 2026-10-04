"""MQTT publishing helpers."""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from typing import Any, TYPE_CHECKING

from . import topics

if TYPE_CHECKING:
    from .state import BridgeState
    from .broker_client import BrokerClient

logger = logging.getLogger(__name__)


def _warn_dropped(state: BridgeState, broker_idx: int | None, message: str) -> None:
    """Rate-limit outage/backpressure warnings without hiding drop counters."""
    now = time.monotonic()
    previous = state.publish_warning_at.get(broker_idx)
    if previous is None or now - previous >= 30:
        logger.warning(message)
        state.publish_warning_at[broker_idx] = now


def safe_publish(
    state: BridgeState,
    topic_type: str,
    payload: str,
    retain: bool = False,
    client: BrokerClient | None = None,
    broker_idx: int | None = None,
) -> bool:
    """Publish to one or all MQTT brokers."""
    if not state.mqtt_connected:
        _warn_dropped(state, None, f"Not connected - dropping publish to {topic_type}")
        state.stats['publish_failures'] += 1
        return False

    success = False

    if client:
        clients_to_publish = [info for info in list(state.mqtt_clients) if info.get('client') is client]
    else:
        clients_to_publish = list(state.mqtt_clients)

    for mqtt_client_info in clients_to_publish:
        bidx = mqtt_client_info['broker_idx']
        broker = topics.get_broker_config(state, bidx)
        broker_name = broker.get('name', f'broker-{bidx}')
        if not mqtt_client_info.get('connected') or mqtt_client_info.get('client') is None:
            state.stats['publish_failures'] += 1
            _warn_dropped(state, bidx, f"[{broker_name}] Disconnected - dropping publish")
            continue

        topic = topics.get_topic(state, topic_type, bidx)
        if not topic:
            continue

        try:
            broker_client = mqtt_client_info['client']
            qos = broker.get('qos', 0)
            if qos == 1:
                qos = 0  # force qos=1 to 0 because qos 1 can cause retry storms

            result = broker_client.publish(topic, payload, qos=qos, retain=retain)
            if not result:
                _warn_dropped(state, bidx, f"[{broker_name}] Publish rejected (disconnected or queue full)")
                state.stats['publish_failures'] += 1
            else:
                logger.debug(f"[{broker_name}] Accepted publish to {topic}")
                success = True
        except Exception as e:
            _warn_dropped(state, bidx, f"[{broker_name}] Publish error to {topic}: {str(e)}")
            state.stats['publish_failures'] += 1

    return success


def build_status_message(state: BridgeState, status: str, include_stats: bool = True) -> dict[str, Any]:
    """Build a status message with all required fields."""
    message: dict[str, Any] = {
        "status": status,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "origin": state.repeater_name,
        "origin_id": state.repeater_pub_key,
        "radio": state.radio_info if state.radio_info else "unknown",
        "model": state.model if state.model else "unknown",
        "firmware_version": state.firmware_version if state.firmware_version else "unknown",
        "client_version": state.client_version
    }

    if include_stats and state.stats['device']:
        message['stats'] = state.stats['device']

    return message


def publish_status(
    state: BridgeState,
    status: str,
    client: BrokerClient | None = None,
    broker_idx: int | None = None,
) -> None:
    """Publish periodic status without retaining it; shutdown uses confirmation."""
    status_msg = build_status_message(state, status, include_stats=True)
    
    if client:
        safe_publish(state, "status", json.dumps(status_msg), retain=False, client=client, broker_idx=broker_idx)
    else:
        safe_publish(state, "status", json.dumps(status_msg), retain=False)

    logger.debug(f"Published status: {status}")


def publish_shutdown_status(
    state: BridgeState, client: BrokerClient, broker_idx: int, timeout: float = 2.0,
) -> bool:
    """Confirm offline before graceful disconnect, or preserve the broker LWT.

    Shutdown is the one low-volume path where QoS 1 must not be downgraded.
    A PUBACK proves the retained update was accepted, unlike QoS 0 handoff.
    If confirmation fails, close without DISCONNECT so the offline LWT remains
    the fallback. Custom clients without confirmation get a best-effort send;
    without abort support they cannot guarantee this transport-level fallback.
    """
    broker = topics.get_broker_config(state, broker_idx)
    topic = topics.get_topic(state, 'status', broker_idx)
    confirmed = False
    try:
        if topic and timeout > 0:
            payload = json.dumps(build_status_message(state, 'offline'))
            retain = broker.get('retain', True)
            qos = max(1, min(2, broker.get('qos', 0)))
            publish_confirmed = getattr(client, 'publish_confirmed', None)
            if publish_confirmed is not None:
                confirmed = bool(publish_confirmed(
                    topic, payload, qos=qos, retain=retain, timeout=timeout,
                ))
            else:
                client.publish(topic, payload, qos=qos, retain=retain)
    except Exception:
        logger.warning('[%s] Could not confirm offline status', broker.get('name', broker_idx), exc_info=True)
    if not confirmed:
        abort = getattr(client, 'abort', None)
        if abort is not None:
            try:
                abort()
                logger.warning('[%s] Offline status unconfirmed; relying on broker LWT',
                               broker.get('name', broker_idx))
            except Exception:
                logger.warning('[%s] Could not abort unconfirmed shutdown transport',
                               broker.get('name', broker_idx), exc_info=True)
        else:
            logger.warning('[%s] Offline status unconfirmed; custom client lacks abort support',
                           broker.get('name', broker_idx))
    return confirmed
