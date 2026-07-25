from __future__ import annotations

import threading
import time


class SessionClock:
    """One monotonic clock shared by video and mixed audio."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.start_event = threading.Event()
        self.stop_event = threading.Event()
        self.start_time: float | None = None
        self.stop_time: float | None = None

    def arm(self, delay_seconds: float = 0.25) -> float:
        with self._lock:
            if self.start_time is None:
                self.start_time = time.perf_counter() + max(0.05, delay_seconds)
                self.start_event.set()
            return self.start_time

    def stop(self) -> float:
        with self._lock:
            if self.stop_time is None:
                now = time.perf_counter()
                start = self.start_time or now
                self.stop_time = max(now, start)
                self.stop_event.set()
            return self.stop_time

    def duration(self) -> float:
        with self._lock:
            if self.start_time is None:
                return 0.0
            end = self.stop_time if self.stop_time is not None else time.perf_counter()
            return max(0.0, end - self.start_time)
