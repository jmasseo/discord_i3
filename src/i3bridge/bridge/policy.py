"""Abuse controls: ignore patterns, per-key rate limiting, per-channel flood breaking."""

from __future__ import annotations

import fnmatch
import time
from collections import deque
from collections.abc import Callable, Iterable


def source_matches(patterns: Iterable[str], mud: str, user: str | None) -> bool:
    """``user@mud`` patterns with fnmatch wildcards; ``mud`` or ``@mud`` ignores a whole mud."""
    mud_l = mud.lower()
    user_l = (user or "").lower()
    full = f"{user_l}@{mud_l}"
    for pat in patterns:
        pat = pat.lower()
        if "@" not in pat:
            if fnmatch.fnmatchcase(mud_l, pat):
                return True
            continue
        if pat.startswith("@"):
            if fnmatch.fnmatchcase(mud_l, pat[1:]):
                return True
            continue
        if fnmatch.fnmatchcase(full, pat):
            return True
    return False


class RateLimiter:
    """Sliding-window limiter: at most ``limit`` events per ``window`` seconds per key."""

    def __init__(
        self, limit: int, window: float, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self.limit = limit
        self.window = window
        self._clock = clock
        self._events: dict[object, deque[float]] = {}

    def allow(self, key: object) -> bool:
        if self.limit <= 0:
            return True
        now = self._clock()
        q = self._events.setdefault(key, deque())
        while q and now - q[0] > self.window:
            q.popleft()
        if len(q) >= self.limit:
            return False
        q.append(now)
        return True

    def forget(self, key: object) -> None:
        self._events.pop(key, None)


class FloodBreaker:
    """Per-channel breaker: once ``limit`` messages arrive within ``window`` seconds the
    channel is muted until the window drains. :meth:`record` returns the action to take."""

    def __init__(
        self, limit: int, window: float, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self.limit = limit
        self.window = window
        self._clock = clock
        self._events: dict[str, deque[float]] = {}
        self._muted_since: dict[str, float] = {}
        self._dropped: dict[str, int] = {}

    def record(self, channel: str) -> str:
        """Return ``"pass"``, ``"trip"`` (first dropped message) or ``"drop"``."""
        if self.limit <= 0:
            return "pass"
        now = self._clock()
        q = self._events.setdefault(channel, deque())
        while q and now - q[0] > self.window:
            q.popleft()
        if channel in self._muted_since:
            if q:
                self._dropped[channel] = self._dropped.get(channel, 0) + 1
                return "drop"
            del self._muted_since[channel]
        q.append(now)
        if len(q) > self.limit:
            self._muted_since[channel] = now
            self._dropped[channel] = 1
            return "trip"
        return "pass"

    def muted(self, channel: str) -> bool:
        return channel in self._muted_since

    def take_dropped(self, channel: str) -> int:
        return self._dropped.pop(channel, 0)
