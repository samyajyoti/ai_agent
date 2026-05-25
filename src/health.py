"""Minimal HTTP health endpoint for Docker HEALTHCHECK / Kubernetes probes.

Exposes:
  GET /health   -> {"status": "ok", "buffer": N, "uptime_seconds": ...}
  GET /metrics  -> simple text/plain metrics
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Callable


class HealthServer:
    def __init__(
        self,
        port: int,
        buffer_size: Callable[[], int],
        incident_count: Callable[[], int],
    ) -> None:
        self._port = port
        self._buffer_size = buffer_size
        self._incident_count = incident_count
        self._started = time.time()
        self._server: asyncio.AbstractServer | None = None

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle, "0.0.0.0", self._port)

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    async def _handle(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        try:
            request_line = await asyncio.wait_for(reader.readline(), timeout=2.0)
        except asyncio.TimeoutError:
            writer.close()
            return

        try:
            _method, path, _version = request_line.decode("latin-1").split(" ", 2)
        except ValueError:
            writer.close()
            return

        while True:
            line = await reader.readline()
            if not line or line in (b"\r\n", b"\n"):
                break

        if path.startswith("/health"):
            body = json.dumps(
                {
                    "status": "ok",
                    "uptime_seconds": int(time.time() - self._started),
                    "buffer": self._buffer_size(),
                    "incidents": self._incident_count(),
                }
            ).encode("utf-8")
            writer.write(_http_response(200, "application/json", body))
        elif path.startswith("/metrics"):
            body = (
                f"ai_agent_uptime_seconds {int(time.time() - self._started)}\n"
                f"ai_agent_buffer_size {self._buffer_size()}\n"
                f"ai_agent_incidents_total {self._incident_count()}\n"
            ).encode("utf-8")
            writer.write(_http_response(200, "text/plain; version=0.0.4", body))
        else:
            writer.write(_http_response(404, "text/plain", b"not found"))

        await writer.drain()
        writer.close()


def _http_response(status: int, content_type: str, body: bytes) -> bytes:
    reason = {200: "OK", 404: "Not Found"}.get(status, "OK")
    headers = (
        f"HTTP/1.1 {status} {reason}\r\n"
        f"Content-Type: {content_type}\r\n"
        f"Content-Length: {len(body)}\r\n"
        f"Connection: close\r\n"
        f"\r\n"
    ).encode("latin-1")
    return headers + body
