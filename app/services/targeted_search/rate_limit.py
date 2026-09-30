"""Bounded in-process request budget for the existing shared-key API."""

from __future__ import annotations

import threading
import time


class RequestBudget:
    def __init__(self, per_minute: int = 120, clock=time.monotonic):
        self.limit = per_minute
        self.clock = clock
        self._clients: dict[str, tuple[float, float]] = {}
        self._lock = threading.Lock()

    def allow(self, client: str) -> bool:
        if self.limit <= 0:
            return True
        with self._lock:
            timestamp = self.clock()
            if len(self._clients) >= 1024:
                self._clients = {key: value for key, value in self._clients.items() if timestamp - value[1] < 60}
                if client not in self._clients and len(self._clients) >= 1024:
                    return False
            tokens, previous = self._clients.get(client, (float(self.limit), timestamp))
            tokens = min(float(self.limit), tokens + max(0, timestamp - previous) * self.limit / 60)
            allowed = tokens >= 1
            self._clients[client] = (tokens - 1 if allowed else tokens, timestamp)
            return allowed
