"""ResilientLLMClient circuit-breaker tests."""

from __future__ import annotations

from src.llm import Diagnosis, HeuristicClient, ResilientLLMClient


class _Quota:
    """Mimics openai.RateLimitError for an insufficient_quota body."""

    def __init__(self) -> None:
        self.calls = 0

    def diagnose(self, signal_payload: dict, log_excerpt: str) -> Diagnosis:
        self.calls += 1
        raise RuntimeError(
            "Error code: 429 - {'error': {'code': 'insufficient_quota', ...}}"
        )


class _Flaky:
    """Fails the first call with a transient error, succeeds the second."""

    def __init__(self) -> None:
        self.calls = 0

    def diagnose(self, signal_payload: dict, log_excerpt: str) -> Diagnosis:
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("temporary outage")
        return Diagnosis(
            root_cause="ok", impact="ok", confidence=0.9,
            suggested_action="notify_only", action_args={},
            explanation="primary recovered", code_or_config_fix="",
        )


def test_hard_failure_trips_breaker_and_falls_back() -> None:
    primary = _Quota()
    client = ResilientLLMClient(primary=primary, fallback=HeuristicClient(), max_attempts=2)

    first = client.diagnose({"kind": "critical_pattern"}, "")
    second = client.diagnose({"kind": "critical_pattern"}, "")

    assert primary.calls == 1, "breaker should prevent the second call from hitting the API"
    assert "hard failure" in first.explanation.lower()
    assert "llm disabled" in second.explanation.lower()


def test_transient_failure_retries_then_succeeds() -> None:
    primary = _Flaky()
    client = ResilientLLMClient(primary=primary, fallback=HeuristicClient(), max_attempts=2)

    diag = client.diagnose({"kind": "api_error_burst"}, "")
    assert diag.root_cause == "ok"
    assert primary.calls == 2
