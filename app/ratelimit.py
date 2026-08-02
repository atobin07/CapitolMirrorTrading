"""Per-user rate limiting to protect Ollama from floods and abuse.

Sliding-window counter kept in memory (one bot process per client, so this is
plenty). A user over the limit is throttled; they're warned once per window,
then ignored until the window clears — so a spammer can't even flood the warning.
"""
from __future__ import annotations

import time
from collections import deque


class RateLimiter:
    def __init__(self, max_per_min: int) -> None:
        self.max = max(1, max_per_min)
        self.window = 60.0
        self._hits: dict[int, deque[float]] = {}
        self._warned: dict[int, float] = {}

    def _now(self) -> float:
        return time.monotonic()

    def check(self, key: int) -> tuple[bool, bool]:
        """Register a hit.

        Returns (allowed, should_warn):
          allowed     — process this message?
          should_warn — send a one-time "slow down" notice (only when first
                        crossing the limit this window)?
        """
        now = self._now()
        hits = self._hits.setdefault(key, deque())
        cutoff = now - self.window
        while hits and hits[0] < cutoff:
            hits.popleft()

        if len(hits) >= self.max:
            warned_at = self._warned.get(key, 0.0)
            should_warn = (now - warned_at) > self.window
            if should_warn:
                self._warned[key] = now
            return False, should_warn

        hits.append(now)
        return True, False

    def cleanup(self, max_keys: int = 10000) -> None:
        """Drop stale entries so memory can't grow unbounded from many users."""
        if len(self._hits) <= max_keys:
            return
        cutoff = self._now() - self.window
        for key in list(self._hits):
            hits = self._hits[key]
            while hits and hits[0] < cutoff:
                hits.popleft()
            if not hits:
                self._hits.pop(key, None)
                self._warned.pop(key, None)
