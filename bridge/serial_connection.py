"""Serial connection abstraction for MeshCore device communication."""
from __future__ import annotations

import calendar
from collections import deque
import json
import logging
import math
import os
import re
import threading
import time
from abc import ABC, abstractmethod
from typing import Any

import serial

logger = logging.getLogger(__name__)


class SerialConnection(ABC):
    """Abstract interface for MeshCore device communication.

    Implementations own their own locking — callers never manage threading.
    Methods are getters that return parsed values; no external state mutation.
    """

    @abstractmethod
    def set_time(self) -> None: ...

    @abstractmethod
    def get_name(self) -> str | None: ...

    @abstractmethod
    def get_pubkey(self) -> str | None: ...

    @abstractmethod
    def get_privkey(self) -> str | None: ...

    @abstractmethod
    def get_radio_info(self) -> str | None: ...

    @abstractmethod
    def get_firmware_version(self) -> str | None: ...

    @abstractmethod
    def get_board_type(self) -> str | None: ...

    @abstractmethod
    def get_device_stats(self) -> dict[str, Any]: ...

    @abstractmethod
    def execute_command(self, command: str, timeout: float = 10.0) -> tuple[bool, str]: ...

    @abstractmethod
    def read_line(self) -> str | None:
        """Non-blocking read of next available line, or None if nothing waiting."""
        ...

    @abstractmethod
    def seconds_since_activity(self) -> float:
        """Seconds since data was last received from the device."""
        ...

    @abstractmethod
    def close(self) -> None: ...

    @property
    @abstractmethod
    def is_open(self) -> bool: ...


class RealSerialConnection(SerialConnection):
    """Drain USB independently of MQTT, with bounded logs and CLI responses.

    Only the reader thread touches receive bytes. Command callers share a
    deadline-aware write lock and receive replies through a separate condition.
    Serial logging is best effort: a stopped consumer loses complete records,
    not memory or the ability to service the device's USB endpoint.
    """

    def __init__(self, port: serial.Serial, *, max_line_bytes: int = 4096,
                 max_pending_lines: int = 256, line_timeout: float = 5.0,
                 write_timeout: float = 2.0, command_timeout: float = 10.0,
                 reader_timeout: float = 0.1) -> None:
        if (isinstance(max_line_bytes, bool) or not isinstance(max_line_bytes, int)
                or not 64 <= max_line_bytes <= 1024 * 1024):
            raise ValueError("max_line_bytes must be between 64 and 1048576")
        if (isinstance(max_pending_lines, bool) or not isinstance(max_pending_lines, int)
                or not 1 <= max_pending_lines <= 65536):
            raise ValueError("max_pending_lines must be between 1 and 65536")
        for name, value in (("line_timeout", line_timeout),
                            ("write_timeout", write_timeout),
                            ("command_timeout", command_timeout),
                            ("reader_timeout", reader_timeout)):
            if (isinstance(value, bool) or not isinstance(value, (int, float))
                    or not math.isfinite(value) or value <= 0):
                raise ValueError(f"{name} must be a positive finite number")
        self._port = port
        self._lock = threading.Lock()
        self._close_lock = threading.Lock()
        self._port_closed = False
        self._condition = threading.Condition()
        self._stop = threading.Event()
        self._max_line_bytes = max_line_bytes
        self._max_pending_lines = max_pending_lines
        self._line_timeout = float(line_timeout)
        self._write_timeout = float(write_timeout)
        self._command_timeout = float(command_timeout)
        self._reader_timeout = min(float(reader_timeout), 0.1)
        self._last_activity = time.monotonic()
        self._lines: deque[str] = deque()
        self._overflow_dropped = 0
        self._reader_error: Exception | None = None
        self._active_command: str | None = None
        self._response_lines: list[str] = []
        self._response_size = 0
        self._response_done = False
        self._response_error: str | None = None
        self._max_response_bytes = 65536
        # Even a caller-provided port must have bounded read/write operations.
        # The short read poll is independent of the generous command deadline.
        self._port.timeout = self._reader_timeout
        self._port.write_timeout = self._write_timeout
        self._reader = threading.Thread(target=self._read_loop, daemon=True,
                                        name="MeshCore-Serial-Reader")
        self._reader.start()

    @staticmethod
    def _is_log_record(line: str) -> bool:
        return (bool(re.match(r"^\d{2}:\d{2}:\d{2} - \d{1,2}/\d{1,2}/\d{4}\s", line))
                or bool(re.search(r"\b(?:RAW:|(?:RX|TX),\s*len=)", line))
                or line.startswith(("DEBUG", "DROP:", "BLE:", "MQTT:",
                                    "WiFi:", "ESP-Now:", "Alert:",
                                    "POWERSAVING:", "[USB ")))

    def _queue_line_locked(self, line: str) -> None:
        if len(self._lines) >= self._max_pending_lines:
            self._lines.popleft()
            self._overflow_dropped += 1
        self._lines.append(line)

    def _dispatch_line(self, line: str) -> None:
        stripped = line.strip()
        if not stripped:
            return
        with self._condition:
            if self._stop.is_set():
                return
            # Companion explicitly indents getter replies by two spaces;
            # legacy firmware prefixes the same value with an arrow.
            # Keep that framing before stripping whitespace: a legitimate
            # name such as DEBUGNode or BLE: relay is not an unsolicited log.
            getter_active = bool(
                self._active_command is not None
                and self._active_command.lower().startswith("get ")
                and not self._response_done
            )
            getter_reply = getter_active and (
                line.startswith("  >") or stripped.startswith("-> >")
            )
            # An unframed terminal prompt may prefix the next unsolicited log
            # or command echo/framed reply. Do not strip '> value' getters.
            if not getter_reply and stripped.startswith("> "):
                tail = stripped[2:].lstrip()
                prefixed_getter = getter_active and stripped[2:].startswith("  >")
                prefixed_reply = prefixed_getter or tail.startswith("-> ")
                if (tail == self._active_command or self._is_log_record(tail)
                        or prefixed_reply):
                    # Full Companion's final prompt has no newline. A log
                    # emitted immediately afterward shares the prompt's line,
                    # so finish the existing reply before queuing that log.
                    # A prompt preceding the command echo or its first reply
                    # must not finish a transaction that has no reply yet.
                    if (self._active_command is not None
                            and self._response_lines and not self._response_done
                            and not prefixed_reply):
                        self._response_done = True
                        self._condition.notify_all()
                    getter_reply = prefixed_getter or (
                        getter_active and tail.startswith("-> >")
                    )
                    stripped = tail
            if not getter_reply and self._is_log_record(stripped):
                self._queue_line_locked(stripped)
                return
            if self._active_command is None or self._response_done:
                if stripped != ">":
                    self._queue_line_locked(stripped)
                return
            if stripped == self._active_command:
                return  # command echo, not its response
            if stripped == ">":
                if self._response_lines:
                    self._response_done = True
                    self._condition.notify_all()
                return
            response = stripped if stripped.startswith("-> ") else "-> " + stripped
            response_bytes = len(response.encode()) + 1
            if self._response_size + response_bytes > self._max_response_bytes:
                self._response_error = "Serial command response exceeded 65536 bytes"
                self._response_done = True
            else:
                # Preserve legacy arrow syntax for the existing getters while
                # accepting Full Companion's bare ASCII response + prompt.
                self._response_lines.append(response)
                self._response_size += response_bytes
                if stripped.startswith("-> "):
                    self._response_done = True
            self._condition.notify_all()

    def _note_invalid_line(self) -> None:
        with self._condition:
            if self._stop.is_set():
                return
            # Insert this marker at the actual stream boundary, unlike an
            # overflow marker which precedes the remaining retained records.
            self._queue_line_locked("DROP:1")
            if self._active_command is not None and not self._response_done:
                self._response_error = "Serial line exceeded size or assembly deadline"
                self._response_done = True
                self._condition.notify_all()

    def _read_loop(self) -> None:
        pending = bytearray()
        line_started = 0.0
        discarding = False
        try:
            while not self._stop.is_set():
                waiting = self._port.in_waiting
                data = self._port.read(min(max(waiting, 1), 1024))
                now = time.monotonic()
                if data:
                    with self._condition:
                        self._last_activity = now
                if pending and now - line_started >= self._line_timeout:
                    pending.clear()
                    discarding = True
                    self._note_invalid_line()
                parts = data.split(b"\n")
                for part_index, part in enumerate(parts):
                    ends_line = part_index < len(parts) - 1
                    if discarding:
                        if ends_line:
                            discarding = False
                        continue
                    if not pending and part:
                        line_started = now
                    if len(pending) + len(part) > self._max_line_bytes:
                        pending.clear()
                        discarding = not ends_line
                        self._note_invalid_line()
                        continue
                    pending.extend(part)
                    if ends_line:
                        self._dispatch_line(pending.decode(errors="replace"))
                        pending.clear()
                # Full Companion ends replies with an unframed '> ' prompt.
                # Complete it after a quiet poll only once a reply exists.
                # Before then, keep a fragmented '> value' prefix intact.
                if not data and bytes(pending).strip() == b">":
                    with self._condition:
                        final_or_idle_prompt = (self._active_command is None
                                                or bool(self._response_lines))
                    if final_or_idle_prompt:
                        self._dispatch_line(">")
                        pending.clear()
        except (serial.SerialException, OSError, TypeError) as exc:
            if not self._stop.is_set():
                with self._condition:
                    self._reader_error = exc
                    self._condition.notify_all()
                logger.warning("Serial reader stopped: %s", exc)
                self.close()

    def _transact(self, cmd: str, deadline: float) -> str:
        command = cmd.strip()
        if not command or "\r" in command or "\n" in command:
            raise ValueError("Serial commands must contain exactly one nonempty line")
        encoded = (command + "\r\n").encode()
        if len(encoded) > self._max_line_bytes:
            raise ValueError("Serial command exceeds max_line_bytes")
        with self._condition:
            if self._stop.is_set() or not self.is_open:
                raise serial.SerialException("Serial connection is closed")
            self._active_command = command
            self._response_lines = []
            self._response_size = 0
            self._response_done = False
            self._response_error = None
        try:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise serial.SerialTimeoutException("Serial command deadline expired before write")
            self._port.write_timeout = min(self._write_timeout, remaining)
            if self._port.write(encoded) != len(encoded):
                raise serial.SerialTimeoutException("Serial command write was incomplete")
            if time.monotonic() >= deadline:
                raise serial.SerialTimeoutException("Serial command deadline expired during write")
            with self._condition:
                while not self._response_done and not self._stop.is_set():
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise serial.SerialTimeoutException("Serial command response timed out")
                    self._condition.wait(remaining)
                if self._response_error:
                    raise serial.SerialException(self._response_error)
                if self._reader_error:
                    raise serial.SerialException(str(self._reader_error))
                if self._stop.is_set():
                    raise serial.SerialException("Serial connection closed during command")
                if time.monotonic() >= deadline:
                    raise serial.SerialTimeoutException("Serial command response timed out")
                return "\n".join(self._response_lines)
        except (serial.SerialException, OSError):
            # The ASCII protocol has no transaction IDs. After a partial write
            # or timeout a late reply cannot safely satisfy the next request;
            # force a fresh serial session rather than reuse that ownership.
            self.close()
            raise
        finally:
            with self._condition:
                self._active_command = None
                self._response_lines = []
                self._response_size = 0

    def _send_with_timeout(self, cmd: str, timeout: float) -> str:
        if (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
                or not math.isfinite(timeout) or timeout <= 0):
            raise ValueError("Serial command timeout must be a positive finite number")
        deadline = time.monotonic() + timeout
        self._acquire_command(deadline)
        try:
            return self._transact(cmd, deadline)
        finally:
            self._lock.release()

    def _acquire_command(self, deadline: float) -> None:
        while not self._stop.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise serial.SerialTimeoutException("Timed out waiting for serial command ownership")
            if self._lock.acquire(timeout=min(remaining, 0.05)):
                return
        raise serial.SerialException("Serial connection is closed")

    def _send(self, cmd: str, delay: float = 0.5) -> str:
        """Query with a finite deadline; legacy delay is no longer a sleep."""
        try:
            return self._send_with_timeout(cmd, self._command_timeout)
        except (serial.SerialException, OSError) as exc:
            logger.warning("Serial query failed: %s", exc)
            return ""

    def _send_unlocked(self, cmd: str, delay: float = 0.5) -> str:
        """Query with the command lock held; retained for internal compatibility."""
        return self._transact(cmd, time.monotonic() + self._command_timeout)

    def set_time(self) -> None:
        epoch_time = int(calendar.timegm(time.gmtime()))
        cmd = f'time {epoch_time}\r\n'
        response = self._send(cmd)
        logger.debug(f"Set time response: {response}")

    def get_name(self) -> str | None:
        response = self._send("get name\r\n")
        logger.debug(f"Raw response: {response}")

        if "-> >" in response:
            name = response.split("-> >", 1)[1].strip()
            if '\n' in name:
                name = name.split('\n')[0]
            name = name.replace('\r', '').strip()
            logger.info(f"Repeater name: {name}")
            return name

        logger.error("Failed to get repeater name from response")
        return None

    def get_pubkey(self) -> str | None:
        response = self._send("get public.key\r\n", delay=1.0)
        logger.debug(f"Raw response: {response}")

        if "-> >" in response:
            pub_key = response.split("-> >", 1)[1].strip()
            if '\n' in pub_key:
                pub_key = pub_key.split('\n')[0]
            pub_key_clean = pub_key.replace(' ', '').replace('\r', '').replace('\n', '')

            if not pub_key_clean or len(pub_key_clean) != 64 or not all(c in '0123456789ABCDEFabcdef' for c in pub_key_clean):
                logger.error(f"Invalid public key format: {repr(pub_key_clean)} (extracted from: {repr(pub_key)})")
                return None

            result = pub_key_clean.upper()
            logger.info(f"Repeater pub key: {result}")
            return result

        logger.error("Failed to get repeater pub key from response")
        return None

    def get_privkey(self) -> str | None:
        response = self._send("get prv.key\r\n", delay=1.0)

        if "-> >" in response:
            priv_key = response.split("-> >", 1)[1].strip()
            if '\n' in priv_key:
                priv_key = priv_key.split('\n')[0]

            priv_key_clean = priv_key.replace(' ', '').replace('\r', '').replace('\n', '')
            if len(priv_key_clean) == 128:
                if all(c in '0123456789ABCDEFabcdef' for c in priv_key_clean):
                    logger.info(f"Repeater priv key: {priv_key_clean[:4]}... (truncated for security)")
                    return priv_key_clean
                logger.error("Private key response contains non-hexadecimal characters")
            else:
                logger.error(f"Response wrong length: {len(priv_key_clean)} (expected 128)")

        logger.error("Failed to get repeater priv key from response - command may not be supported by firmware")
        return None

    def get_radio_info(self) -> str | None:
        response = self._send("get radio\r\n")
        logger.debug(f"Raw radio response: {response}")

        if "-> >" in response:
            radio_info = response.split("-> >", 1)[1].strip()
            if '\n' in radio_info:
                radio_info = radio_info.split('\n')[0]
            logger.debug(f"Parsed radio info: {radio_info}")
            return radio_info

        logger.error("Failed to get radio info from response")
        return None

    def get_firmware_version(self) -> str | None:
        response = self._send("ver\r\n")
        logger.debug(f"Raw version response: {response}")

        if "-> " in response:
            version = response.split("-> ", 1)[1]
            version = version.split('\n')[0].replace('\r', '').strip()
            logger.info(f"Firmware version: {version}")
            return version

        logger.warning("Failed to get firmware version from response")
        return None

    def get_board_type(self) -> str | None:
        response = self._send("board\r\n")
        logger.debug(f"Raw board response: {response}")

        if "-> " in response:
            board_type = response.split("-> ", 1)[1]
            board_type = board_type.split('\n')[0].replace('\r', '').strip()
            if board_type == "Unknown command":
                board_type = "unknown"
            logger.info(f"Board type: {board_type}")
            return board_type

        logger.warning("Failed to get board type from response")
        return None

    @staticmethod
    def _copy_numeric_stats(target: dict[str, Any], source: dict[str, Any],
                            fields: tuple[tuple[str, str], ...]) -> None:
        """Keep numeric telemetry usable by the periodic stats formatter."""
        for source_name, target_name in fields:
            value = source.get(source_name)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                continue
            # The formatter computes floating-point rates. Oversized integers
            # are unusable there even though integers themselves are finite.
            try:
                finite = math.isfinite(value)
            except OverflowError:
                finite = False
            if not finite:
                continue
            if source_name != 'noise_floor' and value < 0:
                continue
            target[target_name] = value

    def get_device_stats(self) -> dict[str, Any]:
        stats: dict[str, Any] = {}

        try:
            self._acquire_command(time.monotonic() + self._command_timeout)
        except serial.SerialException as exc:
            logger.warning("Unable to acquire serial stats ownership: %s", exc)
            return stats
        try:
            # stats-core: battery_mv, uptime_secs, errors, queue_len
            response = self._send_unlocked("stats-core\r\n")
            logger.debug(f"Raw stats-core response: {response}")

            if "-> " in response and "Unknown command" not in response:
                try:
                    json_str = response.split("-> ", 1)[1].strip()
                    json_str = json_str.split('\n')[0].replace('\r', '').strip()
                    core_stats = json.loads(json_str)
                    if not isinstance(core_stats, dict):
                        raise ValueError("stats-core JSON root must be an object")
                    self._copy_numeric_stats(stats, core_stats, (
                        ('battery_mv', 'battery_mv'),
                        ('uptime_secs', 'uptime_secs'),
                        ('errors', 'debug_flags'),
                        ('queue_len', 'queue_len'),
                    ))
                except (json.JSONDecodeError, ValueError) as e:
                    logger.debug(f"Failed to parse stats-core: {e}")

            # stats-radio: noise_floor, tx_air_secs, rx_air_secs
            response = self._send_unlocked("stats-radio\r\n")
            logger.debug(f"Raw stats-radio response: {response}")

            if "-> " in response and "Unknown command" not in response:
                try:
                    json_str = response.split("-> ", 1)[1].strip()
                    json_str = json_str.split('\n')[0].replace('\r', '').strip()
                    radio_stats = json.loads(json_str)
                    if not isinstance(radio_stats, dict):
                        raise ValueError("stats-radio JSON root must be an object")
                    self._copy_numeric_stats(stats, radio_stats, (
                        ('noise_floor', 'noise_floor'),
                        ('tx_air_secs', 'tx_air_secs'),
                        ('rx_air_secs', 'rx_air_secs'),
                    ))
                except (json.JSONDecodeError, ValueError) as e:
                    logger.debug(f"Failed to parse stats-radio: {e}")

            # stats-packets: recv_errors
            response = self._send_unlocked("stats-packets\r\n")
            logger.debug(f"Raw stats-packets response: {response}")

            if "-> " in response and "Unknown command" not in response:
                try:
                    json_str = response.split("-> ", 1)[1].strip()
                    json_str = json_str.split('\n')[0].replace('\r', '').strip()
                    packets_stats: dict[str, Any] = json.loads(json_str)
                    if not isinstance(packets_stats, dict):
                        raise ValueError("stats-packets JSON root must be an object")
                    self._copy_numeric_stats(stats, packets_stats, (
                        ('recv_errors', 'recv_errors'),
                    ))
                except (json.JSONDecodeError, ValueError) as e:
                    logger.debug(f"Failed to parse stats-packets: {e}")

        except (serial.SerialException, OSError) as exc:
            logger.warning("Serial stats query failed: %s", exc)
        finally:
            self._lock.release()

        return stats

    def execute_command(self, command: str, timeout: float = 10.0) -> tuple[bool, str]:
        try:
            full_response = self._send_with_timeout(command, timeout)
            response_text = "\n".join(
                re.sub(r"^->\s*>?\s?", "", line).strip()
                for line in full_response.splitlines()
            ).strip()
            logger.debug("[SERIAL] Command response received (%d characters)", len(response_text))
            return True, response_text or "(no output)"

        except serial.SerialException as e:
            logger.error(f"[SERIAL] Serial error executing command: {e}")
            return False, f"Serial error: {str(e)}"
        except Exception as e:
            logger.error(f"[SERIAL] Error executing command: {e}")
            return False, f"Error: {str(e)}"

    def read_line(self) -> str | None:
        with self._condition:
            if self._reader_error:
                raise OSError(str(self._reader_error))
            if self._stop.is_set() or not self.is_open:
                raise OSError("Serial connection is closed")
            if self._overflow_dropped:
                dropped = self._overflow_dropped
                self._overflow_dropped = 0
                return f"DROP:{dropped}"
            if self._lines:
                return self._lines.popleft()
        return None

    def seconds_since_activity(self) -> float:
        with self._condition:
            return time.monotonic() - self._last_activity

    def close(self) -> None:
        self._stop.set()
        with self._condition:
            self._condition.notify_all()
            self._lines.clear()
            self._overflow_dropped = 0
        # Never acquire the command lock here: shutdown must cancel a blocked
        # write and wake a command waiting on its condition or ownership lock.
        with self._close_lock:
            if not self._port_closed:
                for operation in ("cancel_read", "cancel_write"):
                    try:
                        cancel = getattr(self._port, operation, None)
                        if cancel is not None:
                            cancel()
                    except (serial.SerialException, OSError, TypeError, AttributeError):
                        pass
                try:
                    self._port.close()
                except (serial.SerialException, OSError, TypeError):
                    pass
                self._port_closed = True
        if self._reader is not threading.current_thread():
            self._reader.join(timeout=self._reader_timeout + 0.5)

    @property
    def is_open(self) -> bool:
        return not self._stop.is_set() and getattr(self._port, 'is_open', False)


def connect(config: dict[str, Any], *, expected_public_key: str | None = None) -> RealSerialConnection | None:
    """Try candidates, optionally requiring the startup radio's identity."""
    if expected_public_key is not None:
        if not isinstance(expected_public_key, str) or not re.fullmatch(r"[0-9A-Fa-f]{64}", expected_public_key):
            logger.error("Expected serial public key must be 64 hexadecimal characters")
            return None
        expected_public_key = expected_public_key.upper()
    serial_cfg = config.get('serial', {})
    ports = serial_cfg.get('ports', ['/dev/ttyACM0'])
    baud_rate = serial_cfg.get('baud_rate', 115200)
    legacy_timeout = serial_cfg.get('timeout', 2)
    if (isinstance(legacy_timeout, bool) or not isinstance(legacy_timeout, (int, float))
            or not math.isfinite(legacy_timeout) or legacy_timeout < 0):
        logger.error("Serial timeout must be a nonnegative finite number")
        return None
    # The old nonblocking timeout=0 setting remains valid, but the independent
    # reader still waits between polls instead of spinning on empty reads.
    legacy_poll = min(legacy_timeout, 0.1) if legacy_timeout > 0 else 0.1
    timeout = serial_cfg.get('reader_timeout', legacy_poll)
    write_timeout = serial_cfg.get('write_timeout', 2.0)
    for name, value in (("reader_timeout", timeout), ("write_timeout", write_timeout)):
        if (isinstance(value, bool) or not isinstance(value, (int, float))
                or not math.isfinite(value) or value <= 0):
            logger.error("Serial %s must be a positive finite number", name)
            return None

    for port in ports:
        ser = None
        connection = None
        try:
            ser = serial.Serial(
                port=port,
                baudrate=baud_rate,
                parity=serial.PARITY_NONE,
                stopbits=serial.STOPBITS_ONE,
                bytesize=serial.EIGHTBITS,
                timeout=timeout,
                write_timeout=write_timeout,
                rtscts=False,
                **({'exclusive': True} if os.name == 'posix' else {})
            )
            connection = RealSerialConnection(
                ser, reader_timeout=timeout, write_timeout=write_timeout,
                max_line_bytes=serial_cfg.get('max_line_bytes', 4096),
                max_pending_lines=serial_cfg.get('max_pending_lines', 256),
                line_timeout=serial_cfg.get('line_timeout', 5.0),
                command_timeout=serial_cfg.get('command_timeout', 10.0),
            )
            if expected_public_key is not None and connection.get_pubkey() != expected_public_key:
                logger.warning("Serial identity mismatch or unavailable on %s; trying next candidate", port)
                connection.close()
                continue
            logger.info(f"Connected to {port}")
            return connection
        except (serial.SerialException, OSError, ValueError, TypeError) as e:
            if connection is not None:
                connection.close()
            elif ser is not None:
                ser.close()
            logger.warning(f"Failed to connect to {port}: {str(e)}")
            continue

    logger.error("Failed to connect to any serial port")
    return None
