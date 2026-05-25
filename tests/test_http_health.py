"""Tests for HTTP health detection.

We exercise the parsing/failure-extraction logic and the detector rule
without actually doing HTTP I/O.
"""

from __future__ import annotations

import time

from src.buffer import LogBuffer
from src.config import DetectorConfig
from src.detector import Detector
from src.http_health import _find_failures
from src.log_sources import LogEvent


HEALTHY_BODY = {
    "Cache backend: default": "working",
    "DatabaseBackend": "working",
    "DefaultFileStorageHealthCheck": "working",
    "DiskUsage": "working",
    "MemoryUsage": "working",
    "MyHealthCheckBackend": "working",
    "RabbitMQHealthCheck": "working",
}


def test_find_failures_all_healthy() -> None:
    assert _find_failures(HEALTHY_BODY, "working") == {}


def test_find_failures_one_down() -> None:
    body = dict(HEALTHY_BODY)
    body["RabbitMQHealthCheck"] = "down"
    out = _find_failures(body, "working")
    assert out == {"RabbitMQHealthCheck": "down"}


def test_find_failures_case_insensitive() -> None:
    body = {"Cache": "WORKING", "DB": "Working"}
    assert _find_failures(body, "working") == {}


def test_detector_fires_on_health_failure() -> None:
    det = Detector(DetectorConfig(window_seconds=60))
    buf = LogBuffer()
    buf.add(LogEvent(
        source="testodsy-health",
        kind="http_health",
        line="health check FAILED",
        ts=time.time(),
        parsed={
            "is_error": True,
            "health_failure": True,
            "reason": "subsystem_failure",
            "url": "https://testodsy.wrtual.in/getHealth/",
            "failing": {"RabbitMQHealthCheck": "down"},
        },
    ))
    signals = det.scan(buf)
    health_signals = [s for s in signals if s.kind == "http_health_failure"]
    assert len(health_signals) == 1
    assert "RabbitMQHealthCheck" in health_signals[0].title
    assert health_signals[0].severity == "critical"


def test_detector_does_not_fire_when_healthy() -> None:
    det = Detector(DetectorConfig(window_seconds=60))
    buf = LogBuffer()
    buf.add(LogEvent(
        source="testodsy-health",
        kind="http_health",
        line="health check OK",
        ts=time.time(),
        parsed={
            "url": "https://testodsy.wrtual.in/getHealth/",
            "subsystems": HEALTHY_BODY,
            "status_int": 200,
        },
    ))
    signals = det.scan(buf)
    assert not [s for s in signals if s.kind == "http_health_failure"]
