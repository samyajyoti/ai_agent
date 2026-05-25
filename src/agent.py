"""Main agent orchestrator.

Wires log collectors -> buffer -> detector -> LLM -> action runner -> sinks.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from .actions import run_action
from .buffer import LogBuffer
from .config import RuntimeConfig, YamlConfig
from .detector import Detector, Signal
from .health import HealthServer
from .http_health import poll_http_health
from .llm import Diagnosis, LLMClient, build_llm_client
from .log_sources import LogEvent, tail_docker_container, tail_file
from .notify import JsonlSink, SlackSink

log = logging.getLogger(__name__)
console = Console()


@dataclass(slots=True)
class IncidentRecord:
    ts: float
    signature: str
    signal: Signal
    diagnosis: Diagnosis
    action_result_dict: dict


class Agent:
    def __init__(
        self,
        runtime: RuntimeConfig,
        yaml_cfg: YamlConfig,
        llm: LLMClient | None = None,
    ) -> None:
        self.runtime = runtime
        self.yaml_cfg = yaml_cfg
        self.buffer = LogBuffer(capacity=10_000)
        self.detector = Detector(yaml_cfg.detector)
        self.llm: LLMClient = llm or build_llm_client(runtime)
        self.jsonl_sink = JsonlSink("incidents.jsonl")
        self.slack_sink = SlackSink(runtime.slack_webhook_url) if runtime.slack_webhook_url else None
        self._cooldowns: dict[str, float] = {}
        self._incident_count = 0
        self._stop = asyncio.Event()
        self._health: HealthServer | None = None

    # ------------------------------------------------------------------
    # Public lifecycle
    # ------------------------------------------------------------------

    async def run(self) -> None:
        self._banner()
        await self._start_health_server()
        tasks: list[asyncio.Task] = []

        for src in self.yaml_cfg.sources.docker:
            tasks.append(asyncio.create_task(
                self._consume(tail_docker_container(src.name, src.container, src.kind, self._stop)),
                name=f"docker:{src.container}",
            ))
        for src in self.yaml_cfg.sources.files:
            tasks.append(asyncio.create_task(
                self._consume(tail_file(src.name, src.path, src.kind, self._stop)),
                name=f"file:{src.path}",
            ))
        for src in self.yaml_cfg.sources.http_health:
            tasks.append(asyncio.create_task(
                self._consume(poll_http_health(
                    name=src.name,
                    url=src.url,
                    interval_seconds=src.interval_seconds,
                    timeout_seconds=src.timeout_seconds,
                    expected_value=src.expected_value,
                    method=src.method,
                    verify_ssl=src.verify_ssl,
                    headers=src.headers,
                    stop_event=self._stop,
                )),
                name=f"http_health:{src.name}",
            ))
        tasks.append(asyncio.create_task(self._scan_loop(), name="scan_loop"))

        if not tasks:
            console.print("[yellow]No sources configured. Exiting.[/yellow]")
            return

        try:
            await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            pass
        finally:
            if self._health is not None:
                await self._health.stop()

    def stop(self) -> None:
        self._stop.set()

    async def _start_health_server(self) -> None:
        port_str = os.getenv("AI_AGENT_HEALTH_PORT", "8787")
        try:
            port = int(port_str)
        except ValueError:
            port = 8787
        if port <= 0:
            return
        self._health = HealthServer(
            port=port,
            buffer_size=lambda: len(self.buffer),
            incident_count=lambda: self._incident_count,
        )
        try:
            await self._health.start()
            log.info("Health endpoint listening on :%d", port)
        except OSError as exc:
            log.warning("Could not start health endpoint on :%d (%s)", port, exc)
            self._health = None

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _banner(self) -> None:
        table = Table.grid(padding=(0, 1))
        table.add_row("Provider", self.runtime.llm_provider)
        table.add_row("Dry run", str(self.runtime.dry_run))
        table.add_row("Allowed actions", ", ".join(self.runtime.allowed_actions) or "-")
        table.add_row("Docker sources", str(len(self.yaml_cfg.sources.docker)))
        table.add_row("File sources", str(len(self.yaml_cfg.sources.files)))
        table.add_row("HTTP health sources", str(len(self.yaml_cfg.sources.http_health)))
        console.print(Panel(table, title="AI Ops Agent", border_style="cyan"))

    async def _consume(self, async_iter) -> None:
        try:
            async for event in async_iter:
                self.buffer.add(event)
        except Exception:  # noqa: BLE001
            log.exception("collector crashed")

    async def _scan_loop(self) -> None:
        interval = max(1, self.runtime.scan_interval_seconds)
        while not self._stop.is_set():
            try:
                await asyncio.sleep(interval)
                signals = self.detector.scan(self.buffer)
                for signal in signals:
                    await self._handle_signal(signal)
            except asyncio.CancelledError:
                break
            except Exception:  # noqa: BLE001
                log.exception("scan loop error")

    async def _handle_signal(self, signal: Signal) -> None:
        sig = signal.signature
        now = time.time()
        last = self._cooldowns.get(sig, 0.0)
        if now - last < self.runtime.incident_cooldown_seconds:
            return
        self._cooldowns[sig] = now

        console.print(
            Panel.fit(
                f"[bold red]{signal.title}[/bold red]\n"
                f"[dim]{signal.description}[/dim]",
                title=f"Signal · {signal.kind} · {signal.severity}",
                border_style="red" if signal.severity == "critical" else "yellow",
            )
        )

        excerpt = self._build_log_excerpt(signal)
        signal_payload = {
            "kind": signal.kind,
            "title": signal.title,
            "description": signal.description,
            "severity": signal.severity,
            "metrics": signal.metrics,
        }

        diagnosis = await asyncio.to_thread(self.llm.diagnose, signal_payload, excerpt)
        action_result = run_action(
            diagnosis.suggested_action,
            diagnosis.action_args,
            allowed=self.runtime.allowed_actions,
            dry_run=self.runtime.dry_run,
        )

        self._render_diagnosis(signal, diagnosis, action_result.to_dict())

        payload = {
            "ts": now,
            "signature": sig,
            "signal": signal_payload,
            "diagnosis": diagnosis.to_dict(),
            "action_result": action_result.to_dict(),
            "sample_lines": [e.line for e in signal.sample_events[-10:]],
        }
        self.jsonl_sink.write(payload)
        self._incident_count += 1
        if self.slack_sink:
            await asyncio.to_thread(self.slack_sink.write, payload)

    def _build_log_excerpt(self, signal: Signal, max_lines: int = 80) -> str:
        sample = list(signal.sample_events)
        recent = self.buffer.recent(self.yaml_cfg.detector.window_seconds)
        merged: dict[float, LogEvent] = {e.ts: e for e in recent}
        for e in sample:
            merged[e.ts] = e
        events = sorted(merged.values(), key=lambda e: e.ts)[-max_lines:]
        return "\n".join(f"[{e.isoformat}] [{e.source}/{e.kind}] {e.line}" for e in events)

    def _render_diagnosis(
        self,
        signal: Signal,
        diagnosis: Diagnosis,
        action_result: dict,
    ) -> None:
        table = Table(show_header=False, box=None, padding=(0, 1))
        table.add_row("[bold]Root cause[/bold]", diagnosis.root_cause)
        table.add_row("[bold]Impact[/bold]", diagnosis.impact)
        table.add_row("[bold]Confidence[/bold]", f"{diagnosis.confidence:.0%}")
        table.add_row(
            "[bold]Action[/bold]",
            f"{diagnosis.suggested_action}  ->  {action_result['message']}",
        )
        if diagnosis.explanation:
            table.add_row("[bold]Explanation[/bold]", diagnosis.explanation)
        if diagnosis.code_or_config_fix.strip():
            table.add_row("[bold]Suggested fix[/bold]", diagnosis.code_or_config_fix.strip())
        console.print(
            Panel(
                table,
                title=f"Diagnosis · {signal.kind}",
                border_style="green" if action_result.get("ok") else "red",
            )
        )
