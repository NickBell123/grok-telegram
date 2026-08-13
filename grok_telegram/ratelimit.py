import time
from collections import defaultdict, deque
from typing import Callable, Deque, Dict


class SlidingWindowLimiter:
    def __init__(
        self,
        max_events: int,
        window_seconds: float,
        now_fn: Callable[[], float] = time.monotonic,
    ):
        self.max_events = max_events
        self.window_seconds = window_seconds
        self.now_fn = now_fn
        self._events: Dict[str, Deque[float]] = defaultdict(deque)

    def allow(self, key: str) -> bool:
        now = self.now_fn()
        q = self._events[key]
        cutoff = now - self.window_seconds
        while q and q[0] < cutoff:
            q.popleft()
        if len(q) >= self.max_events:
            return False
        q.append(now)
        return True
