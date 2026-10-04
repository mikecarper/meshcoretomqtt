"""Serial line parsing and MQTT publishing."""
from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from . import topics
from .mqtt_publish import safe_publish

if TYPE_CHECKING:
    from .state import BridgeState

logger = logging.getLogger(__name__)

RAW_PATTERN = re.compile(r"(\d{2}:\d{2}:\d{2}) - (\d{1,2}/\d{1,2}/\d{4}) U RAW: (.*)")
MAX_RAW_BYTES = 255
RAW_PAIR_TIMEOUT = 5.0
PACKET_PREFIX_PATTERN = re.compile(
    r"\d{2}:\d{2}:\d{2} - \d{1,2}/\d{1,2}/\d{4} U: (?:RX|TX),"
)
PACKET_PATTERN = re.compile(
    r"(\d{2}:\d{2}:\d{2}) - (\d{1,2}/\d{1,2}/\d{4}) U: (RX|TX), len=(\d+) \(type=(\d+), route=([A-Z]), payload_len=(\d+)\)"
    r"(?: SNR=(-?\d+) RSSI=(-?\d+) score=(\d+)(?: time=(\d+))?)?"
    r"(?: hash=([0-9A-F]+))?"
    r"(?: \[(.*)\])?$"
)


def parse_and_publish(state: BridgeState, line: str) -> None:
    """Parse a serial line and publish to MQTT."""
    if not line:
        return

    logger.debug(f"From Radio: {line}")

    message: dict = {
        "origin": state.repeater_name,
        "origin_id": state.repeater_pub_key,
        "timestamp": datetime.now(timezone.utc).isoformat()
    }

    # DEBUG text may quote RAW or DROP records. It is not packet framing,
    # even when debug publishing is disabled.
    if line.startswith("DEBUG"):
        if state.debug:
            message.update({
                "type": "DEBUG",
                "message": line
            })
            safe_publish(state, "debug", json.dumps(message))
        return

    # Both firmware congestion and the bounded host reader can drop records.
    # Never carry a RAW payload across an explicitly reported gap.
    if re.search(r"(?:^|\s)DROP:\d+(?:\s|$)", line):
        state.last_raw = None
        state.last_raw_stamp = None
        return

    # Handle RAW messages
    if "U RAW:" in line:
        state.last_raw = None
        state.last_raw_stamp = None
        raw_match = RAW_PATTERN.fullmatch(line)
        if raw_match:
            raw_hex = raw_match.group(3).strip()
            if (0 < len(raw_hex) <= MAX_RAW_BYTES * 2
                    and len(raw_hex) % 2 == 0
                    and re.fullmatch(r"[0-9A-Fa-f]+", raw_hex)):
                state.last_raw = raw_hex
                state.last_raw_stamp = (raw_match.group(1), raw_match.group(2))
                state.last_raw_at = time.monotonic()
                state.stats['bytes_processed'] += len(raw_hex) // 2
        return

    # Handle Packet messages (RX and TX)
    packet_match = PACKET_PATTERN.match(line)
    if packet_match is None:
        if PACKET_PREFIX_PATTERN.match(line):
            # A malformed or newer-format summary still ends this RAW pair.
            # Otherwise its payload could be attached to a different packet
            # with the same length and firmware timestamp a moment later.
            state.last_raw = None
            state.last_raw_stamp = None
        return
    if packet_match:
        direction = packet_match.group(3).lower()
        raw = state.last_raw
        if raw is not None and (
                len(raw) // 2 != int(packet_match.group(4))
                or state.last_raw_stamp != (packet_match.group(1), packet_match.group(2))
                or time.monotonic() - state.last_raw_at > RAW_PAIR_TIMEOUT):
            raw = None
        # A RAW record belongs to at most one summary, even if publishing fails.
        state.last_raw = None
        state.last_raw_stamp = None

        if direction == "rx":
            state.stats['packets_rx'] += 1
        else:
            state.stats['packets_tx'] += 1

        payload: dict = {
            "type": "PACKET",
            "direction": direction,
            "time": packet_match.group(1),
            "date": packet_match.group(2),
            "len": packet_match.group(4),
            "packet_type": packet_match.group(5),
            "route": packet_match.group(6),
            "payload_len": packet_match.group(7),
            "raw": raw
        }

        if direction == "rx":
            payload.update({
                "SNR": packet_match.group(8),
                "RSSI": packet_match.group(9),
                "score": packet_match.group(10),
                "duration": packet_match.group(11),
                "hash": packet_match.group(12)
            })

            if packet_match.group(6) == "D" and packet_match.group(13):
                payload["path"] = packet_match.group(13)

        message.update(payload)
        safe_publish(state, "packets", json.dumps(message))
