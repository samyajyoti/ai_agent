"""Log collectors for Docker containers and plain log files.

Each collector is an async iterator that yields :class:`LogEvent` instances.
The agent's main loop multiplexes all collectors into a single buffer.
"""

from __future__ import annotations

import asyncio
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import AsyncIterator

try:
    import docker  # type: ignore[import-untyped]
    from docker.errors import DockerException, NotFound  # type: ignore[import-untyped]
except ImportError:  # pragma: no cover - allow running without docker installed
    docker = None  # type: ignore[assignment]
    DockerException = Exception  # type: ignore[assignment, misc]
    NotFound = Exception  # type: ignore[assignment, misc]


# ---------------------------------------------------------------------------
# Event model
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class LogEvent:
    """A single normalized log line from any source."""

    source: str           # logical name, e.g. "insurance-api"
    kind: str             # api | nginx_access | nginx_error | generic
    line: str             # raw text
    ts: float = field(default_factory=lambda: time.time())
    parsed: dict = field(default_factory=dict)

    @property
    def isoformat(self) -> str:
        return datetime.fromtimestamp(self.ts, tz=timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Parsers
# ---------------------------------------------------------------------------


_NGINX_ACCESS_RE = re.compile(
    r'(?P<remote>\S+) \S+ \S+ \[(?P<time>[^\]]+)\] '
    r'"(?P<method>[A-Z]+) (?P<path>[^ ]+) [^"]+" '
    r'(?P<status>\d{3}) (?P<size>\d+)'
    r'(?: "[^"]*" "[^"]*")?'
    r'(?: (?P<rt>[\d.]+))?'
)


def parse_nginx_access(line: str) -> dict:
    m = _NGINX_ACCESS_RE.search(line)
    if not m:
        return {}
    out = m.groupdict()
    try:
        out["status_int"] = int(out["status"])
    except (TypeError, ValueError):
        pass
    if out.get("rt"):
        try:
            out["request_time"] = float(out["rt"])
        except ValueError:
            pass
    return out


_API_ERROR_RE = re.compile(
    r"\b(error|exception|fatal|panic|traceback|unhandled)\b",
    re.IGNORECASE,
)


def parse_api(line: str) -> dict:
    out: dict = {}
    if _API_ERROR_RE.search(line):
        out["is_error"] = True
    return out


def parse_line(kind: str, line: str) -> dict:
    if kind == "nginx_access":
        return parse_nginx_access(line)
    if kind in {"api", "nginx_error", "generic"}:
        return parse_api(line)
    return {}


# ---------------------------------------------------------------------------
# Docker collector
# ---------------------------------------------------------------------------


async def tail_docker_container(
    name: str,
    container_name: str,
    kind: str,
    stop_event: asyncio.Event,
) -> AsyncIterator[LogEvent]:
    """Tail a single Docker container's stdout/stderr.

    Reconnects with backoff if the container restarts.
    """
    if docker is None:
        raise RuntimeError(
            "The 'docker' Python package is not installed; cannot tail Docker logs."
        )

    client = docker.from_env()
    backoff = 1.0

    while not stop_event.is_set():
        try:
            container = client.containers.get(container_name)
        except NotFound:
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30)
            continue
        except DockerException as exc:
            yield LogEvent(
                source=name,
                kind="generic",
                line=f"[ai-agent] docker error: {exc}",
            )
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30)
            continue

        backoff = 1.0
        stream = container.logs(
            stream=True, follow=True, tail=0, stdout=True, stderr=True
        )

        loop = asyncio.get_running_loop()
        try:
            while not stop_event.is_set():
                chunk = await loop.run_in_executor(None, _safe_next, stream)
                if chunk is None:
                    break
                for raw_line in chunk.splitlines():
                    text = raw_line.decode("utf-8", errors="replace").rstrip()
                    if not text:
                        continue
                    yield LogEvent(
                        source=name,
                        kind=kind if kind != "nginx" else "nginx_error",
                        line=text,
                        parsed=parse_line(kind, text),
                    )
        finally:
            try:
                stream.close()
            except Exception:  # noqa: BLE001
                pass


def _safe_next(stream) -> bytes | None:
    """Pull the next chunk from a blocking docker log stream."""
    try:
        return next(stream)
    except StopIteration:
        return None
    except Exception:  # noqa: BLE001
        return None


# ---------------------------------------------------------------------------
# File collector
# ---------------------------------------------------------------------------


async def tail_file(
    name: str,
    path: str,
    kind: str,
    stop_event: asyncio.Event,
) -> AsyncIterator[LogEvent]:
    """Tail a plain log file, handling rotation by reopening on inode change."""
    inode: int | None = None
    fh = None

    async def _open() -> None:
        nonlocal fh, inode
        try:
            fh = open(path, "r", encoding="utf-8", errors="replace")  # noqa: SIM115
            fh.seek(0, os.SEEK_END)
            inode = os.fstat(fh.fileno()).st_ino
        except FileNotFoundError:
            fh = None
            inode = None

    await _open()

    try:
        while not stop_event.is_set():
            if fh is None:
                await asyncio.sleep(1.0)
                await _open()
                continue

            line = fh.readline()
            if not line:
                # Check for log rotation.
                try:
                    st = os.stat(path)
                    if inode is not None and st.st_ino != inode:
                        fh.close()
                        await _open()
                        continue
                except FileNotFoundError:
                    fh.close()
                    fh = None
                    continue
                await asyncio.sleep(0.5)
                continue

            text = line.rstrip("\n")
            if not text:
                continue
            yield LogEvent(
                source=name,
                kind=kind,
                line=text,
                parsed=parse_line(kind, text),
            )
    finally:
        if fh is not None:
            try:
                fh.close()
            except Exception:  # noqa: BLE001
                pass
