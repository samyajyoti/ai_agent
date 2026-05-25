from __future__ import annotations

from src.actions import run_action


def test_unknown_action_is_downgraded() -> None:
    res = run_action("nuke_everything", {}, allowed=["restart_container"], dry_run=True)
    assert res.ok is True
    assert res.action == "nuke_everything"
    assert "manual" in res.message.lower() or "human" in res.message.lower()


def test_disallowed_action_refused() -> None:
    res = run_action(
        "restart_container",
        {"container": "x"},
        allowed=["notify_only"],
        dry_run=True,
    )
    assert res.ok is False
    assert "not in ALLOWED_ACTIONS" in res.message


def test_dry_run_restart() -> None:
    res = run_action(
        "restart_container",
        {"container": "insurance-api-dev"},
        allowed=["restart_container"],
        dry_run=True,
    )
    assert res.ok is True
    assert res.dry_run is True
    assert "DRY RUN" in res.message


def test_scale_refuses_unsafe_replicas() -> None:
    res = run_action(
        "scale_service",
        {"service": "api", "replicas": 99},
        allowed=["scale_service"],
        dry_run=False,
    )
    assert res.ok is False
    assert "safety" in res.message.lower()
