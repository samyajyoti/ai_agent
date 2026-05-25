"""LLM client wrapper.

Two providers are supported (OpenAI, Anthropic). A third "none" provider is a
deterministic fallback that produces a structured report from heuristics
alone — useful for local testing without API keys.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Protocol

from .config import RuntimeConfig

log = logging.getLogger(__name__)


@dataclass(slots=True)
class Diagnosis:
    """Structured LLM output describing the incident."""

    root_cause: str
    impact: str
    confidence: float           # 0..1
    suggested_action: str       # one of the allowlisted action ids or "manual"
    action_args: dict           # arguments for the action (e.g. container name)
    explanation: str            # human-readable summary
    code_or_config_fix: str     # patch/snippet or empty string

    def to_dict(self) -> dict:
        return {
            "root_cause": self.root_cause,
            "impact": self.impact,
            "confidence": self.confidence,
            "suggested_action": self.suggested_action,
            "action_args": self.action_args,
            "explanation": self.explanation,
            "code_or_config_fix": self.code_or_config_fix,
        }


SYSTEM_PROMPT = """You are an SRE/AI-Ops assistant for a Dockerized API stack
(Bun + Elysia API, Redis, optional Nginx in front). You receive:

  - a description of a detected anomaly
  - recent log lines from Docker containers, the API, and Nginx access logs

Your job:

  1. Identify the most likely root cause in one or two sentences.
  2. Estimate user/business impact in one sentence.
  3. Recommend ONE action from this allowlist (or "manual" if none applies):
       - "notify_only"        : just alert humans
       - "restart_container"  : restart a Docker container (args: {"container": "<name>"})
       - "scale_service"      : scale a compose service (args: {"service": "<name>", "replicas": <int>})
       - "prune_logs"         : truncate large Docker log files (args: {"container": "<name>"})
  4. Provide a confidence score from 0 to 1.
  5. If a code or config change would fix it, include a short diff or snippet.

Respond ONLY with a single JSON object matching this schema:

{
  "root_cause": "string",
  "impact": "string",
  "confidence": 0.0,
  "suggested_action": "notify_only|restart_container|scale_service|prune_logs|manual",
  "action_args": {},
  "explanation": "string",
  "code_or_config_fix": "string"
}
"""


def _build_user_prompt(signal_payload: dict, log_excerpt: str) -> str:
    return (
        "Detected anomaly:\n"
        f"{json.dumps(signal_payload, indent=2)}\n\n"
        "Recent relevant log lines (most recent last):\n"
        "------------------------------------------------------------\n"
        f"{log_excerpt}\n"
        "------------------------------------------------------------\n"
        "Return ONLY the JSON object."
    )


class LLMClient(Protocol):
    def diagnose(self, signal_payload: dict, log_excerpt: str) -> Diagnosis: ...


# ---------------------------------------------------------------------------
# Providers
# ---------------------------------------------------------------------------


class OpenAIClient:
    def __init__(self, api_key: str, model: str) -> None:
        from openai import OpenAI  # local import keeps dep optional

        self._client = OpenAI(api_key=api_key)
        self._model = model

    def diagnose(self, signal_payload: dict, log_excerpt: str) -> Diagnosis:
        resp = self._client.chat.completions.create(
            model=self._model,
            response_format={"type": "json_object"},
            temperature=0.1,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": _build_user_prompt(signal_payload, log_excerpt)},
            ],
        )
        content = resp.choices[0].message.content or "{}"
        return _parse_diagnosis(content)


class AnthropicClient:
    def __init__(self, api_key: str, model: str) -> None:
        import anthropic  # local import keeps dep optional

        self._client = anthropic.Anthropic(api_key=api_key)
        self._model = model

    def diagnose(self, signal_payload: dict, log_excerpt: str) -> Diagnosis:
        msg = self._client.messages.create(
            model=self._model,
            max_tokens=1024,
            temperature=0.1,
            system=SYSTEM_PROMPT,
            messages=[
                {"role": "user", "content": _build_user_prompt(signal_payload, log_excerpt)},
            ],
        )
        text = "".join(
            getattr(block, "text", "") for block in msg.content if getattr(block, "type", "") == "text"
        )
        return _parse_diagnosis(text)


class HeuristicClient:
    """Offline fallback that produces a sane default diagnosis."""

    def diagnose(self, signal_payload: dict, log_excerpt: str) -> Diagnosis:
        kind = signal_payload.get("kind", "")
        if kind == "nginx_5xx_spike":
            return Diagnosis(
                root_cause="Upstream API is returning 5xx responses to Nginx.",
                impact="End users see failed requests; SLA at risk.",
                confidence=0.4,
                suggested_action="notify_only",
                action_args={},
                explanation=(
                    "Without an LLM, the agent cannot reason about the exact cause. "
                    "Inspect the attached log excerpt and consider restarting the API "
                    "container if errors persist."
                ),
                code_or_config_fix="",
            )
        if kind == "critical_pattern":
            return Diagnosis(
                root_cause="A critical error pattern was logged.",
                impact="Service may be degraded or crashing.",
                confidence=0.5,
                suggested_action="notify_only",
                action_args={},
                explanation="Investigate the matched log line; restart may be required.",
                code_or_config_fix="",
            )
        return Diagnosis(
            root_cause="Anomaly detected; no LLM available for deeper analysis.",
            impact="Unknown.",
            confidence=0.2,
            suggested_action="notify_only",
            action_args={},
            explanation="Enable an LLM provider for full root-cause analysis.",
            code_or_config_fix="",
        )


# ---------------------------------------------------------------------------
# Factory + parsing
# ---------------------------------------------------------------------------


def build_llm_client(cfg: RuntimeConfig) -> LLMClient:
    provider = cfg.llm_provider
    if provider == "openai" and cfg.openai_api_key:
        return OpenAIClient(cfg.openai_api_key, cfg.openai_model)
    if provider == "anthropic" and cfg.anthropic_api_key:
        return AnthropicClient(cfg.anthropic_api_key, cfg.anthropic_model)
    if provider != "none":
        log.warning(
            "LLM provider '%s' requested but no API key configured; falling back to heuristic mode.",
            provider,
        )
    return HeuristicClient()


def _parse_diagnosis(content: str) -> Diagnosis:
    """Parse the LLM's JSON response into a Diagnosis, defensively."""
    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        start = content.find("{")
        end = content.rfind("}")
        if start != -1 and end != -1 and end > start:
            try:
                data = json.loads(content[start : end + 1])
            except json.JSONDecodeError:
                data = {}
        else:
            data = {}

    return Diagnosis(
        root_cause=str(data.get("root_cause", "Unknown")),
        impact=str(data.get("impact", "Unknown")),
        confidence=float(data.get("confidence", 0.0) or 0.0),
        suggested_action=str(data.get("suggested_action", "manual")),
        action_args=dict(data.get("action_args") or {}),
        explanation=str(data.get("explanation", "")),
        code_or_config_fix=str(data.get("code_or_config_fix", "")),
    )
