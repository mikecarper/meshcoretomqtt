"""Remote serial command handling via MQTT."""
from __future__ import annotations

import logging
import math
import time
from typing import Any, TYPE_CHECKING

from . import topics

if TYPE_CHECKING:
    from .state import BridgeState
    from .broker_client import BrokerClient

logger = logging.getLogger(__name__)


def is_command_allowed(state: BridgeState, command: str) -> tuple[bool, str | None]:
    """Check if a command is allowed (not in disallowed list)."""
    cmd_lower = command.strip().lower()

    for disallowed in state.remote_serial_disallowed_commands:
        if cmd_lower.startswith(disallowed.lower()):
            return False, disallowed

    return True, None


def cleanup_old_nonces(state: BridgeState, *, now: float | None = None) -> None:
    """Remove expired nonces from the tracking dict."""
    current_time = time.time() if now is None else now
    with state.remote_serial_nonce_lock:
        expired = [nonce for nonce, deadline in state.remote_serial_nonces.items()
                   if deadline <= current_time]
        for nonce in expired:
            del state.remote_serial_nonces[nonce]

    if expired:
        logger.debug(f"[SERIAL] Cleaned up {len(expired)} expired nonces")


def _claim_nonce(state: BridgeState, nonce: str, expires: float) -> str | None:
    """Atomically reserve once; never evict live entries to make room."""
    with state.remote_serial_nonce_lock:
        now = time.time()
        cleanup_old_nonces(state, now=now)
        if expires <= now:
            return 'expired'
        if nonce in state.remote_serial_nonces:
            return 'duplicate'
        if len(state.remote_serial_nonces) >= state.remote_serial_max_pending_nonces:
            return 'full'
        state.remote_serial_nonces[nonce] = max(expires, now + state.remote_serial_nonce_ttl)
        return None


def _command_fields(state: BridgeState, payload: Any) -> tuple[str, str, str, str, float]:
    """Validate unauthenticated input before using it or signing a response."""
    if not isinstance(payload, dict):
        raise ValueError('Command JWT payload must be an object')
    public_key, target = payload.get('publicKey'), payload.get('target')
    for key in (public_key, target):
        if (not isinstance(key, str) or len(key) != 64
                or any(c not in '0123456789abcdefABCDEF' for c in key)):
            raise ValueError('Command JWT requires valid publicKey and target')
    command, nonce, expires = payload.get('command'), payload.get('nonce'), payload.get('exp')
    if not isinstance(nonce, str) or not 1 <= len(nonce) <= 256:
        raise ValueError('Command JWT requires a bounded nonce')
    limit = state.config.get('serial', {}).get('max_line_bytes', 4096)
    if (not isinstance(command, str) or not command.strip()
            or any(ord(c) < 32 or ord(c) == 127 for c in command)
            or len(command.encode('utf-8')) + 2 > limit):
        raise ValueError('Command JWT requires one bounded serial command')
    if (isinstance(expires, bool) or not isinstance(expires, (int, float))
            or not math.isfinite(expires)):
        raise ValueError('Command JWT requires a finite expiry')
    return public_key.upper(), command, target.upper(), nonce, expires


def subscribe_serial_commands(state: BridgeState, client: BrokerClient, broker_idx: int) -> None:
    """Subscribe to the serial/commands topic for this node."""
    if not state.remote_serial_enabled:
        return

    if not state.repeater_pub_key:
        broker = topics.get_broker_config(state, broker_idx)
        broker_name = broker.get('name', f'broker-{broker_idx}')
        logger.warning(f"[{broker_name}] Cannot subscribe to serial commands - public key not available")
        return

    topic = topics.resolve_topic_template(
        state, 'meshcore/{IATA}/{PUBLIC_KEY}/serial/commands', broker_idx)

    broker = topics.get_broker_config(state, broker_idx)
    broker_name = broker.get('name', f'broker-{broker_idx}')
    try:
        client.subscribe(topic, qos=1)
        logger.info(f"[{broker_name}] Subscribed to remote serial: {topic}")
    except Exception as e:
        logger.error(f"[{broker_name}] Error subscribing to {topic}: {e}")


def handle_serial_command(state: BridgeState, jwt_token: str, broker_idx: int) -> None:
    """Process an incoming serial command JWT."""
    if not state.remote_serial_enabled:
        logger.warning("[SERIAL] Remote serial command received but feature is disabled")
        return

    if not state.remote_serial_allowed_companions:
        logger.warning("[SERIAL] Remote serial command received but no companions are allowed")
        return

    if not state.auth:
        logger.error("[SERIAL] Auth provider not available")
        return

    # Decode only to select an allowed verification key. Never execute claims
    # until the verifier returns them and the nonce is atomically reserved.
    try:
        if not isinstance(jwt_token, str) or len(jwt_token) > 16384:
            raise ValueError('Command JWT exceeds input limit')
        companion_pubkey, command, target, nonce, exp = _command_fields(
            state, state.auth.decode_payload(jwt_token))
    except Exception as e:
        logger.warning(f"[SERIAL] Failed to decode command JWT: {e}")
        return

    # Verify target matches our public key
    if target != (state.repeater_pub_key or '').upper():
        logger.debug("[SERIAL] Command targets another node")
        return

    # Verify companion is in allowlist
    if companion_pubkey not in state.remote_serial_allowed_companions:
        logger.warning(f"[SERIAL] Command from unauthorized companion: {companion_pubkey[:16]}...")
        return

    # Check expiry against our system clock
    current_time = time.time()
    if current_time >= exp:
        logger.warning(f"[SERIAL] Command JWT expired (exp={exp}, now={current_time})")
        publish_serial_response(state, command, nonce, False, "Command expired", broker_idx)
        return

    # Verify JWT signature
    try:
        verified = _command_fields(state, state.auth.verify_token(jwt_token, companion_pubkey))
        if verified[0] != companion_pubkey or verified[2] != target:
            raise ValueError('Verified command identity changed')
        companion_pubkey, command, target, nonce, exp = verified
        logger.debug(f"[SERIAL] JWT signature verified for companion {companion_pubkey[:16]}...")
    except Exception as e:
        logger.warning(f"[SERIAL] JWT signature verification failed: {e}")
        publish_serial_response(state, command, nonce, False, "Invalid signature", broker_idx)
        return

    claim_error = _claim_nonce(state, nonce, exp)
    if claim_error == 'duplicate':
        logger.warning('[SERIAL] Duplicate command nonce dropped')
        return
    if claim_error:
        publish_serial_response(state, command, nonce, False,
                                'Command expired' if claim_error == 'expired'
                                else 'Replay protection cache full; retry with a new nonce later', broker_idx)
        return

    # Check if command is disallowed
    allowed, matched_rule = is_command_allowed(state, command)
    if not allowed:
        logger.warning(f"[SERIAL] Command blocked by rule '{matched_rule}'")
        publish_serial_response(state, command, nonce, False, f"Command blocked: {matched_rule}", broker_idx)
        return

    # Execute the serial command
    device = state.device
    if state.should_exit or device is None:
        publish_serial_response(state, command, nonce, False, "Serial port not connected", broker_idx)
        return

    logger.info(f"[SERIAL] Executing command from {companion_pubkey[:16]}...")
    try:
        success, response = device.execute_command(command, timeout=state.remote_serial_command_timeout)
    except Exception:
        logger.warning('[SERIAL] Command failed on its captured serial session', exc_info=True)
        success, response = False, 'Serial command failed; use a new nonce to retry'

    # Publish response
    publish_serial_response(state, command, nonce, success, response, broker_idx)


def publish_serial_response(
    state: BridgeState,
    command: str,
    request_id: str,
    success: bool,
    response: str,
    broker_idx: int | None = None,
) -> None:
    """Create and publish a signed response only to the requesting broker."""
    mqtt_info = next((info for info in list(state.mqtt_clients)
                      if info.get('broker_idx') == broker_idx and info.get('connected')), None)
    client = mqtt_info.get('client') if mqtt_info is not None else None
    if broker_idx is None or client is None:
        logger.warning('[SERIAL] Requesting broker unavailable; response not published')
        return
    if not state.repeater_priv_key or not state.repeater_pub_key:
        logger.error("[SERIAL] Cannot sign response - private key not available")
        return

    if not state.auth:
        logger.error("[SERIAL] Auth provider not available")
        return

    try:
        claims = {
            'command': command,
            'request_id': request_id,
            'success': success,
            'response': response
        }

        response_jwt = state.auth.create_token(
            state.repeater_pub_key,
            state.repeater_priv_key,
            expiry_seconds=60,
            **claims
        )

        response_topic = topics.resolve_topic_template(
            state, 'meshcore/{IATA}/{PUBLIC_KEY}/serial/responses', broker_idx)
        published = bool(client.publish(response_topic, response_jwt, qos=1))

        if published:
            logger.info(f"[SERIAL] Response published (success={success}, request_id={request_id[:16]}...)")
        else:
            logger.error("[SERIAL] Failed to publish response to requesting broker")

    except Exception as e:
        logger.error(f"[SERIAL] Failed to create/publish response: {e}")
