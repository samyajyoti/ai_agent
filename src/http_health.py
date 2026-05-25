"""HTTP health-check collector.

Polls a JSON health endpoint on an interval and emits a :class:`LogEvent` per
scrape. Failures (non-2xx, timeout, malformed JSON, or any subsystem whose
value does not equal ``expected_value``) are marked with
``parsed["health_failure"] = True`` so the detector can fire on them
immediately.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import AsyncIterator

import httpx

from .log_sources import LogEvent

log = logging.getLogger(__name__)


async def poll_http_health(
    name: str,
    url: str,
    interval_seconds: int,
    timeout_seconds: float,
    expected_value: str,
    method: str,
    verify_ssl: bool,
    headers: dict[str, str],
    stop_event: asyncio.Event,
) -> AsyncIterator[LogEvent]:
    """Periodically scrape an HTTP health endpoint."""
    expected_norm = expected_value.strip().lower()
    interval = max(1, int(interval_seconds))

    async with httpx.AsyncClient(
        timeout=timeout_seconds,
        verify=verify_ssl,
        headers=headers or None,
        follow_redirects=True,
    ) as client:
        while not stop_event.is_set():
            ev = await _scrape_once(name, url, method, client, expected_norm)
            yield ev
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=interval)
            except asyncio.TimeoutError:
                continue


async def _scrape_once(
    name: str,
    url: str,
    method: str,
    client: httpx.AsyncClient,
    expected_norm: str,
) -> LogEvent:
    parsed: dict = {"url": url}
    try:
        resp = await client.request(method, url)
    except httpx.TimeoutException as exc:
        line = f"health check TIMEOUT for {url}: {exc}"
        parsed.update({"is_error": True, "health_failure": True, "reason": "timeout"})
        return LogEvent(source=name, kind="http_health", line=line, parsed=parsed)
    except httpx.HTTPError as exc:
        line = f"health check ERROR for {url}: {exc}"
        parsed.update({"is_error": True, "health_failure": True, "reason": "transport_error"})
        return LogEvent(source=name, kind="http_health", line=line, parsed=parsed)

    parsed["status_int"] = resp.status_code
    if resp.status_code >= 500:
        line = f"health check 5xx ({resp.status_code}) for {url}"
        parsed.update({"is_error": True, "health_failure": True, "reason": "http_5xx"})
        return LogEvent(source=name, kind="http_health", line=line, parsed=parsed)
    if resp.status_code >= 400:
        line = f"health check 4xx ({resp.status_code}) for {url}"
        parsed.update({"is_error": True, "health_failure": True, "reason": "http_4xx"})
        return LogEvent(source=name, kind="http_health", line=line, parsed=parsed)

    try:
        body = resp.json()
    except (ValueError, json.JSONDecodeError):
        line = f"health check returned non-JSON body from {url}"
        parsed.update({"is_error": True, "health_failure": True, "reason": "non_json"})
        return LogEvent(source=name, kind="http_health", line=line, parsed=parsed)

    failing = _find_failures(body, expected_norm)
    parsed["subsystems"] = body if isinstance(body, dict) else {"_raw": body}

    if failing:
        parsed.update({
            "is_error": True,
            "health_failure": True,
            "reason": "subsystem_failure",
            "failing": failing,
        })
        details = ", ".join(f"{k}={v!r}" for k, v in failing.items())
        line = f"health check FAILED for {url}: {details}"
        return LogEvent(source=name, kind="http_health", line=line, parsed=parsed)

    line = f"health check OK for {url} ({len(parsed['subsystems'])} subsystems)"
    return LogEvent(source=name, kind="http_health", line=line, parsed=parsed)


def _find_failures(body: object, expected_norm: str) -> dict[str, str]:
    """Return {subsystem: actual_value} for any value != expected_value."""
    failing: dict[str, str] = {}
    if isinstance(body, dict):
        for key, value in body.items():
            if isinstance(value, str):
                if value.strip().lower() != expected_norm:
                    failing[key] = value
            elif isinstance(value, bool):
                if value is not True:
                    failing[key] = str(value)
            elif isinstance(value, dict):
                status = value.get("status") or value.get("state")
                if isinstance(status, str) and status.strip().lower() != expected_norm:
                    failing[key] = status
            else:
                if str(value).strip().lower() != expected_norm:
                    failing[key] = str(value)
    return failing
