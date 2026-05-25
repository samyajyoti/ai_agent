"""Deterministic, rule-based diagnoser.

Implements the same protocol as the LLM clients (``diagnose(signal_payload,
log_excerpt) -> Diagnosis``), so it can be dropped in wherever an LLM is used.

Behaviour:
  1. Look at the signal first. For an ``http_health_failure`` signal we have
     the precise set of failing subsystems in ``signal.metrics`` /
     ``sample_events[-1].parsed['failing']``.
  2. Score every :class:`DependencyRule` against:
        * the failing-subsystem names (when present),
        * the signal title + description,
        * the log excerpt itself.
  3. Pick the highest-scoring rule. If at least one keyword matched, propose
     ``restart_container`` for that rule's container.
  4. Otherwise apply the policy's ``default_action``:
        * restart_api  -> restart the configured API container,
        * notify_only  -> just notify.
"""

from __future__ import annotations

import logging
import re

from .config import DependencyRule, PolicyConfig
from .llm import Diagnosis

log = logging.getLogger(__name__)


class PolicyEngine:
    def __init__(self, policy: PolicyConfig) -> None:
        self._policy = policy
        self._compiled: list[tuple[DependencyRule, list[re.Pattern[str]]]] = []
        for rule in policy.dependencies:
            patterns: list[re.Pattern[str]] = []
            for kw in rule.keywords:
                try:
                    patterns.append(re.compile(kw, re.IGNORECASE))
                except re.error as exc:
                    log.warning(
                        "Skipping bad keyword %r in rule %r: %s", kw, rule.label, exc
                    )
            self._compiled.append((rule, patterns))

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------

    def diagnose(self, signal_payload: dict, log_excerpt: str) -> Diagnosis:
        failing_subsystems = _extract_failing_subsystems(signal_payload)
        haystacks = self._build_haystacks(signal_payload, log_excerpt, failing_subsystems)

        match, score = self._best_match(haystacks)

        if match is not None and score > 0:
            rule = match
            sub_hint = (
                f"Failing subsystem(s): {', '.join(sorted(failing_subsystems))}. "
                if failing_subsystems else ""
            )
            return Diagnosis(
                root_cause=(
                    f"{rule.label} appears to be unhealthy or unreachable from the "
                    f"API (matched policy rule '{rule.label}', score={score})."
                ),
                impact=(
                    f"Any request path that depends on {rule.label} will fail until "
                    "the dependency recovers."
                ),
                confidence=min(0.4 + 0.1 * score, 0.95),
                suggested_action="restart_container",
                action_args={"container": rule.container},
                explanation=(
                    f"Policy-based decision (no LLM). {sub_hint}"
                    f"Matched keywords against rule '{rule.label}' "
                    f"(container: {rule.container})."
                ),
                code_or_config_fix="",
            )

        return self._default_diagnosis(signal_payload, failing_subsystems)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _build_haystacks(
        self,
        signal_payload: dict,
        log_excerpt: str,
        failing_subsystems: set[str],
    ) -> list[str]:
        haystacks: list[str] = []
        title = str(signal_payload.get("title", ""))
        description = str(signal_payload.get("description", ""))
        if title:
            haystacks.append(title)
        if description:
            haystacks.append(description)
        if failing_subsystems:
            haystacks.append(" ".join(failing_subsystems))
        if log_excerpt:
            haystacks.append(log_excerpt)
        return haystacks

    def _best_match(
        self, haystacks: list[str]
    ) -> tuple[DependencyRule | None, int]:
        best_rule: DependencyRule | None = None
        best_score = 0
        for rule, patterns in self._compiled:
            score = 0
            for pat in patterns:
                for hay in haystacks:
                    if pat.search(hay):
                        score += 1
                        break  # don't double-count one keyword across haystacks
            if score > best_score:
                best_score = score
                best_rule = rule
        return best_rule, best_score

    def _default_diagnosis(
        self,
        signal_payload: dict,
        failing_subsystems: set[str],
    ) -> Diagnosis:
        kind = signal_payload.get("kind", "")
        api_container = self._policy.api_container

        if self._policy.default_action == "restart_api" and api_container:
            return Diagnosis(
                root_cause=(
                    "API anomaly detected but no specific dependency rule matched."
                ),
                impact="Requests may be failing or degraded.",
                confidence=0.45,
                suggested_action="restart_container",
                action_args={"container": api_container},
                explanation=(
                    "Policy default_action=restart_api. Restarting the API "
                    "container to clear transient state."
                    + (
                        f" Failing subsystems reported: {', '.join(sorted(failing_subsystems))}."
                        if failing_subsystems else ""
                    )
                ),
                code_or_config_fix="",
            )

        return Diagnosis(
            root_cause=f"{kind or 'Anomaly'} detected; no dependency rule matched.",
            impact="Unknown without manual investigation.",
            confidence=0.3,
            suggested_action="notify_only",
            action_args={},
            explanation=(
                "Policy-based decision. Add a matching `dependencies` rule in "
                "config.yaml to enable an automated restart for this signal."
                + (
                    f" Failing subsystems reported: {', '.join(sorted(failing_subsystems))}."
                    if failing_subsystems else ""
                )
            ),
            code_or_config_fix="",
        )


def _extract_failing_subsystems(signal_payload: dict) -> set[str]:
    """Pull failing-subsystem names out of an http_health_failure signal."""
    out: set[str] = set()
    title = str(signal_payload.get("title", ""))
    if "Health endpoint failing:" in title:
        after = title.split(":", 1)[1]
        for item in after.split(","):
            name = item.strip()
            if name:
                out.add(name)
    return out
