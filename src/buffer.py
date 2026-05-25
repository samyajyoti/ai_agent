"""Rolling in-memory buffer for recent log events.

The detector scans this buffer at a fixed cadence. When an incident fires,
we extract the most relevant slice (recent + matching events) as context
for the LLM.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from typing import Iterable

from .log_sources import LogEvent


class LogBuffer:
    """Thread-safe ring buffer of recent :class:`LogEvent` items."""

    def __init__(self, capacity: int = 5000) -> None:
        self._buf: deque[LogEvent] = deque(maxlen=capacity)
        self._lock = threading.Lock()

    def add(self, event: LogEvent) -> None:
        with self._lock:
            self._buf.append(event)

    def snapshot(self) -> list[LogEvent]:
        with self._lock:
            return list(self._buf)

    def recent(self, seconds: float) -> list[LogEvent]:
        cutoff = time.time() - seconds
        with self._lock:
            return [e for e in self._buf if e.ts >= cutoff]

    def filter(
        self,
        seconds: float,
        kinds: Iterable[str] | None = None,
        sources: Iterable[str] | None = None,
    ) -> list[LogEvent]:
        cutoff = time.time() - seconds
        kset = set(kinds) if kinds else None
        sset = set(sources) if sources else None
        with self._lock:
            return [
                e
                for e in self._buf
                if e.ts >= cutoff
                and (kset is None or e.kind in kset)
                and (sset is None or e.source in sset)
            ]

    def __len__(self) -> int:
        with self._lock:
            return len(self._buf)
