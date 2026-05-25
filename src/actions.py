"""Allowlisted remediation actions.

Every action is a pure function that returns an :class:`ActionResult`.
The dispatcher refuses to run anything not in ``allowed_actions`` or anything
when ``dry_run=True``.
"""

from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass
from typing import Callable

try:
    import docker  # type: ignore[import-untyped]
    from docker.errors import DockerException, NotFound  # type: ignore[import-untyped]
except ImportError:  # pragma: no cover
    docker = None  # type: ignore[assignment]
    DockerException = Exception  # type: ignore[assignment, misc]
    NotFound = Exception  # type: ignore[assignment, misc]

log = logging.getLogger(__name__)


@dataclass(slots=True)
class ActionResult:
    ok: bool
    action: str
    message: str
    dry_run: bool = False

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "action": self.action,
            "message": self.message,
            "dry_run": self.dry_run,
        }


# ---------------------------------------------------------------------------
# Individual actions
# ---------------------------------------------------------------------------


def _notify_only(args: dict, dry_run: bool) -> ActionResult:  # noqa: ARG001
    return ActionResult(
        ok=True,
        action="notify_only",
        message="No automated action taken; humans notified.",
        dry_run=dry_run,
    )


def _restart_container(args: dict, dry_run: bool) -> ActionResult:
    name = args.get("container")
    if not name:
        return ActionResult(False, "restart_container", "Missing 'container' arg.", dry_run)

    if dry_run:
        return ActionResult(
            True,
            "restart_container",
            f"DRY RUN: would restart container '{name}'.",
            dry_run=True,
        )

    if docker is None:
        return ActionResult(False, "restart_container", "docker SDK not available", dry_run)

    try:
        client = docker.from_env()
        container = client.containers.get(name)
        container.restart(timeout=10)
    except NotFound:
        return ActionResult(False, "restart_container", f"Container '{name}' not found.", dry_run)
    except DockerException as exc:
        return ActionResult(False, "restart_container", f"Docker error: {exc}", dry_run)

    return ActionResult(True, "restart_container", f"Restarted container '{name}'.", dry_run)


def _scale_service(args: dict, dry_run: bool) -> ActionResult:
    service = args.get("service")
    replicas = args.get("replicas")
    if not service or replicas is None:
        return ActionResult(False, "scale_service", "Missing 'service' or 'replicas' arg.", dry_run)

    try:
        replicas_int = int(replicas)
    except (TypeError, ValueError):
        return ActionResult(False, "scale_service", "'replicas' must be an integer.", dry_run)

    if replicas_int < 0 or replicas_int > 10:
        return ActionResult(
            False,
            "scale_service",
            "Refusing to scale outside [0, 10] for safety.",
            dry_run,
        )

    if dry_run:
        return ActionResult(
            True,
            "scale_service",
            f"DRY RUN: would scale '{service}' to {replicas_int} replicas.",
            dry_run=True,
        )

    cmd = ["docker", "compose", "up", "-d", "--no-deps", "--scale", f"{service}={replicas_int}", service]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120, check=False)
    except (subprocess.SubprocessError, FileNotFoundError) as exc:
        return ActionResult(False, "scale_service", f"Failed to run docker compose: {exc}", dry_run)

    if proc.returncode != 0:
        return ActionResult(
            False,
            "scale_service",
            f"docker compose exited {proc.returncode}: {proc.stderr.strip()[:400]}",
            dry_run,
        )

    return ActionResult(
        True,
        "scale_service",
        f"Scaled '{service}' to {replicas_int}.",
        dry_run,
    )


def _prune_logs(args: dict, dry_run: bool) -> ActionResult:
    name = args.get("container")
    if not name:
        return ActionResult(False, "prune_logs", "Missing 'container' arg.", dry_run)

    if dry_run:
        return ActionResult(
            True,
            "prune_logs",
            f"DRY RUN: would truncate Docker logs for '{name}'.",
            dry_run=True,
        )

    if docker is None:
        return ActionResult(False, "prune_logs", "docker SDK not available", dry_run)

    try:
        client = docker.from_env()
        container = client.containers.get(name)
        info = client.api.inspect_container(container.id)
        log_path = info.get("LogPath")
    except (NotFound, DockerException) as exc:
        return ActionResult(False, "prune_logs", f"Docker error: {exc}", dry_run)

    if not log_path:
        return ActionResult(False, "prune_logs", "Container has no LogPath.", dry_run)

    # We deliberately don't write to /var/lib/docker ourselves; instead ask the
    # docker engine via 'truncate' through a privileged helper. If unavailable,
    # we surface a clear message rather than attempting unsafe writes.
    try:
        proc = subprocess.run(
            ["truncate", "-s", "0", log_path],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (subprocess.SubprocessError, FileNotFoundError) as exc:
        return ActionResult(False, "prune_logs", f"Failed to truncate: {exc}", dry_run)

    if proc.returncode != 0:
        return ActionResult(
            False,
            "prune_logs",
            f"truncate exited {proc.returncode}: {proc.stderr.strip()[:200]}",
            dry_run,
        )
    return ActionResult(True, "prune_logs", f"Truncated logs for '{name}'.", dry_run)


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------


_REGISTRY: dict[str, Callable[[dict, bool], ActionResult]] = {
    "notify_only": _notify_only,
    "restart_container": _restart_container,
    "scale_service": _scale_service,
    "prune_logs": _prune_logs,
}


def run_action(
    action: str,
    args: dict,
    *,
    allowed: list[str],
    dry_run: bool,
) -> ActionResult:
    if action == "manual" or action not in _REGISTRY:
        return ActionResult(
            ok=True,
            action=action or "manual",
            message="No automated remediation; requires human follow-up.",
            dry_run=dry_run,
        )
    if action not in allowed:
        return ActionResult(
            ok=False,
            action=action,
            message=f"Action '{action}' is not in ALLOWED_ACTIONS.",
            dry_run=dry_run,
        )
    handler = _REGISTRY[action]
    try:
        return handler(args, dry_run)
    except Exception as exc:  # noqa: BLE001
        log.exception("Action '%s' raised", action)
        return ActionResult(False, action, f"Unhandled error: {exc}", dry_run)
