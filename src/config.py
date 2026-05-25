"""Configuration loading for the AI Ops Agent.

Environment variables come from `.env` (LLM keys, behavior toggles).
Log sources and detector thresholds come from `config.yaml`.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# YAML config models
# ---------------------------------------------------------------------------


class DockerSource(BaseModel):
    name: str
    container: str
    kind: Literal["api", "nginx", "generic"] = "generic"


class FileSource(BaseModel):
    name: str
    path: str
    kind: Literal["nginx_access", "nginx_error", "api", "generic"] = "generic"


class HttpHealthSource(BaseModel):
    """Periodically poll an HTTP health-check endpoint.

    The endpoint is expected to return JSON whose values are strings such as
    ``"working"`` / ``"ok"`` / ``"healthy"``. Any value that does not match
    ``expected_value`` (case-insensitive) is treated as a failed subsystem.
    """

    name: str
    url: str
    interval_seconds: int = 30
    timeout_seconds: float = 10.0
    expected_value: str = "working"
    method: Literal["GET", "POST"] = "GET"
    verify_ssl: bool = True
    # Optional headers, e.g. Authorization for protected endpoints.
    headers: dict[str, str] = Field(default_factory=dict)


class Sources(BaseModel):
    docker: list[DockerSource] = Field(default_factory=list)
    files: list[FileSource] = Field(default_factory=list)
    http_health: list[HttpHealthSource] = Field(default_factory=list)


class DetectorConfig(BaseModel):
    window_seconds: int = 60
    nginx_5xx_rate_threshold: float = 0.05
    api_error_count_threshold: int = 10
    nginx_p95_latency_seconds: float = 2.5
    critical_patterns: list[str] = Field(default_factory=list)


class DependencyRule(BaseModel):
    """Map error keywords / health-subsystem names to a container to restart."""

    label: str                                  # e.g. "RabbitMQ"
    container: str                              # docker container name to restart
    keywords: list[str] = Field(default_factory=list)  # case-insensitive regex/substrings


class PolicyConfig(BaseModel):
    """Deterministic, rule-based diagnoser. Used when LLM_PROVIDER=none, and
    also as the fallback whenever the LLM is unavailable.

    ``api_container`` is the container that runs the API itself; it's the
    default restart target if no dependency rule matches a failure.

    ``default_action`` is what to do when nothing matches:
      * restart_api  -> restart_container on api_container
      * notify_only  -> just alert humans
    """

    api_container: str | None = None
    default_action: Literal["restart_api", "notify_only"] = "notify_only"
    dependencies: list[DependencyRule] = Field(default_factory=list)


class YamlConfig(BaseModel):
    sources: Sources = Field(default_factory=Sources)
    detector: DetectorConfig = Field(default_factory=DetectorConfig)
    policy: PolicyConfig = Field(default_factory=PolicyConfig)


# ---------------------------------------------------------------------------
# Env-based runtime config
# ---------------------------------------------------------------------------


class RuntimeConfig(BaseModel):
    llm_provider: Literal["openai", "anthropic", "none"] = "openai"
    openai_api_key: str | None = None
    openai_model: str = "gpt-4o-mini"
    anthropic_api_key: str | None = None
    anthropic_model: str = "claude-3-5-sonnet-latest"

    dry_run: bool = True
    allowed_actions: list[str] = Field(default_factory=lambda: ["notify_only"])
    scan_interval_seconds: int = 15
    incident_cooldown_seconds: int = 300

    slack_webhook_url: str | None = None


def _split_csv(value: str | None) -> list[str]:
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


def load_runtime_config(env_file: str | os.PathLike[str] | None = ".env") -> RuntimeConfig:
    """Load runtime config from environment (with optional .env file)."""
    if env_file and Path(env_file).exists():
        load_dotenv(env_file, override=False)

    return RuntimeConfig(
        llm_provider=os.getenv("LLM_PROVIDER", "openai").lower(),  # type: ignore[arg-type]
        openai_api_key=os.getenv("OPENAI_API_KEY") or None,
        openai_model=os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
        anthropic_api_key=os.getenv("ANTHROPIC_API_KEY") or None,
        anthropic_model=os.getenv("ANTHROPIC_MODEL", "claude-3-5-sonnet-latest"),
        dry_run=os.getenv("DRY_RUN", "true").lower() in {"1", "true", "yes", "on"},
        allowed_actions=_split_csv(os.getenv("ALLOWED_ACTIONS", "notify_only")),
        scan_interval_seconds=int(os.getenv("SCAN_INTERVAL_SECONDS", "15")),
        incident_cooldown_seconds=int(os.getenv("INCIDENT_COOLDOWN_SECONDS", "300")),
        slack_webhook_url=os.getenv("SLACK_WEBHOOK_URL") or None,
    )


def load_yaml_config(path: str | os.PathLike[str]) -> YamlConfig:
    """Load source + detector config from a YAML file."""
    text = Path(path).read_text(encoding="utf-8")
    raw = yaml.safe_load(text) or {}
    return YamlConfig.model_validate(raw)
