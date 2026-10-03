"""Dependency-free systemd readiness and progress-based watchdog notifications."""
from __future__ import annotations

import math
import os
import socket
import time
from collections.abc import Callable, Mapping


class ServiceHealth:
    """Notify only when the caller makes progress, not from a blind timer thread."""

    def __init__(self, *, environment: Mapping[str, str] | None = None,
                 clock: Callable[[], float] = time.monotonic) -> None:
        environment = os.environ if environment is None else environment
        self._address = environment.get('NOTIFY_SOCKET', '')
        self._clock = clock
        self._last_ping = float('-inf')
        self._interval = 0.0
        try:
            watchdog_pid = int(environment.get('WATCHDOG_PID', str(os.getpid())))
            seconds = int(environment.get('WATCHDOG_USEC', '0')) / 1_000_000
            if watchdog_pid == os.getpid() and math.isfinite(seconds) and seconds > 0:
                self._interval = seconds / 3
        except (ValueError, OverflowError):
            pass

    def _notify(self, message: str) -> bool:
        if not self._address or not hasattr(socket, 'AF_UNIX'):
            return False
        address = self._address
        if address.startswith('@'):
            address = '\0' + address[1:]
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as notifier:
                notifier.settimeout(0.1)
                notifier.sendto(message.encode('ascii'), address)
            return True
        except (OSError, ValueError):
            # Direct runs, Docker and launchd do not require systemd.
            return False

    def ready(self) -> bool:
        return self._notify('READY=1')

    def tick(self, healthy: bool = True) -> bool:
        now = self._clock()
        if not healthy or self._interval <= 0 or now - self._last_ping < self._interval:
            return False
        if self._notify('WATCHDOG=1'):
            self._last_ping = now
            return True
        return False

    def stopping(self) -> bool:
        return self._notify('STOPPING=1')
