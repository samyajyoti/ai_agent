"""Heuristic anomaly detection over the rolling log buffer.

Returns lightweight :class:`Signal` objects describing what's wrong and which
events triggered the detection. The orchestrator then asks the LLM for the
actual root-cause analysis and proposed fix.
"""

from __future__ import annotations

import hashlib
import re
import statistics
from dataclasses import dataclass, field
from typing import Iterable

from .buffer import LogBuffer
from .config import DetectorConfig
from .log_sources import LogEvent


@dataclass(slots=True)
class Signal:
    """A detected anomaly worth investigating."""

    kind: str                          # e.g. "nginx_5xx_spike", "critical_pattern"
    title: str
    description: str
    severity: str                      # info | warning | critical
    sample_events: list[LogEvent] = field(default_factory=list)
    metrics: dict[str, float] = field(default_factory=dict)

    @property
    def signature(self) -> str:
        """Stable hash used for incident deduplication / cooldowns."""
        seed = f"{self.kind}|{self.title}".encode("utf-8")
        return hashlib.sha1(seed, usedforsecurity=False).hexdigest()[:12]


class Detector:
    def __init__(self, config: DetectorConfig) -> None:
        self.config = config
        self._critical_re = [
            re.compile(p, re.IGNORECASE) for p in config.critical_patterns
        ]

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------

    def scan(self, buffer: LogBuffer) -> list[Signal]:
        window = self.config.window_seconds
        events = buffer.recent(window)
        if not events:
            return []

        signals: list[Signal] = []
        signals.extend(self._check_critical_patterns(events))
        signals.extend(self._check_nginx(events))
        signals.extend(self._check_api_errors(events))
        return signals

    # ------------------------------------------------------------------
    # Individual detectors
    # ------------------------------------------------------------------

    def _check_critical_patterns(self, events: Iterable[LogEvent]) -> list[Signal]:
        if not self._critical_re:
            return []
        hits: dict[str, list[LogEvent]] = {}
        for ev in events:
            for rx in self._critical_re:
                if rx.search(ev.line):
                    hits.setdefault(rx.pattern, []).append(ev)
                    break
        out: list[Signal] = []
        for pattern, matched in hits.items():
            out.append(
                Signal(
                    kind="critical_pattern",
                    title=f"Critical pattern detected: /{pattern}/",
                    description=(
                        f"{len(matched)} log line(s) in the last "
                        f"{self.config.window_seconds}s matched a critical pattern."
                    ),
                    severity="critical",
                    sample_events=matched[-10:],
                    metrics={"matches": float(len(matched))},
                )
            )
        return out

    def _check_nginx(self, events: Iterable[LogEvent]) -> list[Signal]:
        access = [e for e in events if e.kind == "nginx_access"]
        if not access:
            return []

        total = len(access)
        status_5xx = [e for e in access if e.parsed.get("status_int", 0) >= 500]
        rate_5xx = len(status_5xx) / total if total else 0.0

        signals: list[Signal] = []
        if rate_5xx >= self.config.nginx_5xx_rate_threshold and len(status_5xx) >= 3:
            signals.append(
                Signal(
                    kind="nginx_5xx_spike",
                    title=f"Nginx 5xx rate {rate_5xx:.1%} over {total} requests",
                    description=(
                        f"{len(status_5xx)} 5xx responses out of {total} requests "
                        f"in the last {self.config.window_seconds}s."
                    ),
                    severity="critical" if rate_5xx >= 0.2 else "warning",
                    sample_events=status_5xx[-10:],
                    metrics={
                        "rate_5xx": rate_5xx,
                        "count_5xx": float(len(status_5xx)),
                        "total": float(total),
                    },
                )
            )

        latencies = [
            e.parsed["request_time"]
            for e in access
            if isinstance(e.parsed.get("request_time"), (int, float))
        ]
        if len(latencies) >= 20:
            latencies.sort()
            p95 = latencies[int(len(latencies) * 0.95) - 1]
            if p95 >= self.config.nginx_p95_latency_seconds:
                signals.append(
                    Signal(
                        kind="nginx_latency_high",
                        title=f"Nginx p95 latency {p95:.2f}s",
                        description=(
                            f"p95 upstream/request time is {p95:.2f}s "
                            f"(threshold {self.config.nginx_p95_latency_seconds}s) "
                            f"over {len(latencies)} requests."
                        ),
                        severity="warning",
                        sample_events=[
                            e
                            for e in access
                            if e.parsed.get("request_time", 0)
                            >= self.config.nginx_p95_latency_seconds
                        ][-10:],
                        metrics={
                            "p95": float(p95),
                            "samples": float(len(latencies)),
                            "mean": float(statistics.fmean(latencies)),
                        },
                    )
                )
        return signals

    def _check_api_errors(self, events: Iterable[LogEvent]) -> list[Signal]:
        api_errors = [
            e
            for e in events
            if e.kind in {"api", "generic", "nginx_error"}
            and e.parsed.get("is_error")
        ]
        if len(api_errors) < self.config.api_error_count_threshold:
            return []
        return [
            Signal(
                kind="api_error_burst",
                title=f"{len(api_errors)} API/app errors in window",
                description=(
                    f"{len(api_errors)} log lines matched error/exception patterns "
                    f"in the last {self.config.window_seconds}s."
                ),
                severity="warning" if len(api_errors) < 50 else "critical",
                sample_events=api_errors[-15:],
                metrics={"count": float(len(api_errors))},
            )
        ]
