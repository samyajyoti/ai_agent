"""PolicyEngine classification tests."""

from __future__ import annotations

from src.config import DependencyRule, PolicyConfig
from src.policy import PolicyEngine


def _policy() -> PolicyConfig:
    return PolicyConfig(
        api_container="demtest2-api",
        default_action="restart_api",
        dependencies=[
            DependencyRule(
                label="RabbitMQ",
                container="demtest2-rabbitmq",
                keywords=["rabbitmq", "amqp", "RabbitMQHealthCheck"],
            ),
            DependencyRule(
                label="Redis",
                container="demtest2-redis",
                keywords=["redis", "ECONNREFUSED.*:6379"],
            ),
            DependencyRule(
                label="PostgreSQL",
                container="demtest2-postgres",
                keywords=["postgres", "DatabaseBackend"],
            ),
        ],
    )


def test_matches_rabbitmq_subsystem_from_health_signal() -> None:
    engine = PolicyEngine(_policy())
    diag = engine.diagnose(
        {
            "kind": "http_health_failure",
            "title": "Health endpoint failing: RabbitMQHealthCheck",
            "description": "testodsy-health: 1 failed scrape ... RabbitMQHealthCheck='down'",
            "severity": "critical",
        },
        log_excerpt="",
    )
    assert diag.suggested_action == "restart_container"
    assert diag.action_args == {"container": "demtest2-rabbitmq"}
    assert "RabbitMQ" in diag.root_cause


def test_matches_redis_from_log_excerpt() -> None:
    engine = PolicyEngine(_policy())
    excerpt = (
        "[demtest2-api/api] ERROR redis.connection: ECONNREFUSED 10.0.0.5:6379\n"
        "[demtest2-api/api] retry attempt 3 failed\n"
    )
    diag = engine.diagnose(
        {"kind": "critical_pattern", "title": "Critical pattern detected: /ECONNREFUSED/"},
        log_excerpt=excerpt,
    )
    assert diag.action_args == {"container": "demtest2-redis"}


def test_default_action_restart_api_when_no_match() -> None:
    engine = PolicyEngine(_policy())
    diag = engine.diagnose(
        {"kind": "api_error_burst", "title": "12 API/app errors in window"},
        log_excerpt="something unrelated happened",
    )
    assert diag.suggested_action == "restart_container"
    assert diag.action_args == {"container": "demtest2-api"}


def test_notify_only_default() -> None:
    cfg = _policy()
    cfg.default_action = "notify_only"
    cfg.api_container = None
    engine = PolicyEngine(cfg)
    diag = engine.diagnose(
        {"kind": "api_error_burst", "title": "12 API/app errors in window"},
        log_excerpt="something unrelated",
    )
    assert diag.suggested_action == "notify_only"
