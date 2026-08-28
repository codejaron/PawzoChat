# PawzoChat - Multi-platform LLM-powered chatbot
# Copyright (C) 2026  iwyxdxl
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published
# by the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""Small, explicit per-client limiter for the administrator login route."""

from __future__ import annotations

from collections import deque
import threading
import time


class LoginRateLimiter:
    def __init__(self, *, max_failures: int = 5, window_seconds: int = 900):
        self.max_failures = max_failures
        self.window_seconds = window_seconds
        self._failures: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def _prune_locked(self, key: str, now: float) -> deque[float]:
        failures = self._failures.setdefault(key, deque())
        cutoff = now - self.window_seconds
        while failures and failures[0] <= cutoff:
            failures.popleft()
        if not failures:
            self._failures.pop(key, None)
            return deque()
        return failures

    def retry_after(self, key: str) -> int:
        now = time.monotonic()
        with self._lock:
            failures = self._prune_locked(key, now)
            if len(failures) < self.max_failures:
                return 0
            return max(1, int(self.window_seconds - (now - failures[0])))

    def record_failure(self, key: str) -> int:
        now = time.monotonic()
        with self._lock:
            failures = self._prune_locked(key, now)
            if not failures:
                failures = self._failures.setdefault(key, deque())
            failures.append(now)
            if len(failures) < self.max_failures:
                return 0
            return max(1, int(self.window_seconds - (now - failures[0])))

    def record_success(self, key: str) -> None:
        with self._lock:
            self._failures.pop(key, None)
