"""Incident notification sinks.

The agent writes structured JSONL incidents to disk and optionally posts a
short summary to Slack via webhook. The console sink (rich) is handled in
``main`` so it can share the live UI.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import httpx

log = logging.getLogger(__name__)


class JsonlSink:
    """Append-only JSONL log of incidents for audit + offline review."""

    def __init__(self, path: str | Path = "incidents.jsonl") -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, payload: dict[str, Any]) -> None:
        with self._path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, ensure_ascii=False) + "\n")


class SlackSink:
    def __init__(self, webhook_url: str) -> None:
        self._url = webhook_url

    def write(self, payload: dict[str, Any]) -> None:
        diag = payload.get("diagnosis", {})
        action = payload.get("action_result", {})
        title = payload.get("signal", {}).get("title", "Incident detected")
        severity = payload.get("signal", {}).get("severity", "warning")
        text = (
            f"*[{severity.upper()}] {title}*\n"
            f"*Root cause:* {diag.get('root_cause', '?')}\n"
            f"*Impact:* {diag.get('impact', '?')}\n"
            f"*Action:* `{diag.get('suggested_action', '?')}` -> {action.get('message', '?')}\n"
            f"*Confidence:* {diag.get('confidence', 0):.0%}"
        )
        try:
            httpx.post(self._url, json={"text": text}, timeout=10)
        except httpx.HTTPError as exc:
            log.warning("Slack notify failed: %s", exc)
