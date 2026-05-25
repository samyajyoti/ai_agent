"""Detector + parser unit tests.

Run with: python -m pytest -q (after `pip install pytest`).
"""

from __future__ import annotations

import time

from src.buffer import LogBuffer
from src.config import DetectorConfig
from src.detector import Detector
from src.log_sources import LogEvent, parse_nginx_access, parse_api


def _ev(line: str, kind: str = "api", source: str = "api", parsed: dict | None = None) -> LogEvent:
    return LogEvent(source=source, kind=kind, line=line, ts=time.time(), parsed=parsed or {})


def test_parse_nginx_access_basic() -> None:
    line = (
        '10.0.0.1 - - [25/May/2026:12:00:00 +0000] '
        '"GET /api/health HTTP/1.1" 200 17 "-" "curl/8" 0.012'
    )
    out = parse_nginx_access(line)
    assert out["status_int"] == 200
    assert out["request_time"] == 0.012


def test_parse_api_marks_errors() -> None:
    assert parse_api("ERROR: database is not available")["is_error"] is True
    assert parse_api("info: ok") == {}


def test_detector_fires_on_5xx_spike() -> None:
    cfg = DetectorConfig(
        window_seconds=60, nginx_5xx_rate_threshold=0.05, api_error_count_threshold=999
    )
    det = Detector(cfg)
    buf = LogBuffer()
    for _ in range(95):
        buf.add(_ev("ok", kind="nginx_access", source="nginx", parsed={"status_int": 200}))
    for _ in range(5):
        buf.add(_ev("err", kind="nginx_access", source="nginx", parsed={"status_int": 502}))

    signals = det.scan(buf)
    kinds = {s.kind for s in signals}
    assert "nginx_5xx_spike" in kinds


def test_detector_fires_on_critical_pattern() -> None:
    cfg = DetectorConfig(window_seconds=60, critical_patterns=["out of memory"])
    det = Detector(cfg)
    buf = LogBuffer()
    buf.add(_ev("FATAL: out of memory while allocating"))
    signals = det.scan(buf)
    assert any(s.kind == "critical_pattern" for s in signals)


def test_detector_quiet_when_no_anomalies() -> None:
    cfg = DetectorConfig(window_seconds=60, api_error_count_threshold=10)
    det = Detector(cfg)
    buf = LogBuffer()
    for _ in range(5):
        buf.add(_ev("info: started", parsed={}))
    assert det.scan(buf) == []
