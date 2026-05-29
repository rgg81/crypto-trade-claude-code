from __future__ import annotations


class WeightLimiter:
    """Trailing-window weight budget (Binance fapi is ~2400 weight/min/IP). `allow` records the
    weight and returns False if it would exceed capacity in the current window."""

    def __init__(self, capacity: int, window_seconds: float):
        self.capacity = capacity
        self.window = window_seconds
        self._events: list[tuple[float, int]] = []

    def _prune(self, now: float) -> None:
        self._events = [(t, w) for t, w in self._events if t > now - self.window]

    def used(self, now: float) -> int:
        self._prune(now)
        return sum(w for _, w in self._events)

    def allow(self, weight: int, now: float) -> bool:
        self._prune(now)
        if sum(w for _, w in self._events) + weight > self.capacity:
            return False
        self._events.append((now, weight))
        return True
