"""CLI entry point for the AI Ops Agent."""

from __future__ import annotations

import asyncio
import json
import logging
import signal
import sys
from pathlib import Path

import typer
from rich.console import Console

from .agent import Agent
from .config import load_runtime_config, load_yaml_config
from .detector import Detector
from .llm import build_llm_client
from .log_sources import LogEvent, parse_line

app = typer.Typer(add_completion=False, help="AI Ops Agent for Docker / API / Nginx logs.")
console = Console()


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )


@app.command()
def run(
    config: Path = typer.Option(Path("config.yaml"), "--config", "-c", help="Path to YAML config."),
    env: Path = typer.Option(Path(".env"), "--env", "-e", help="Path to .env file."),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Start the agent (tail logs, detect, diagnose, remediate)."""
    _setup_logging(verbose)
    if not config.exists():
        console.print(f"[red]Config not found at {config}. Copy config.example.yaml and edit it.[/red]")
        raise typer.Exit(2)

    runtime = load_runtime_config(env if env.exists() else None)
    yaml_cfg = load_yaml_config(config)
    agent = Agent(runtime, yaml_cfg)

    loop = asyncio.new_event_loop()

    def _handle_signal(*_args) -> None:
        console.print("\n[yellow]Shutting down...[/yellow]")
        agent.stop()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _handle_signal)
        except NotImplementedError:
            signal.signal(sig, _handle_signal)

    try:
        loop.run_until_complete(agent.run())
    finally:
        loop.close()


@app.command()
def analyze(
    log_file: Path = typer.Argument(..., help="Log file to analyze."),
    kind: str = typer.Option("api", "--kind", "-k", help="api|nginx_access|nginx_error|generic"),
    env: Path = typer.Option(Path(".env"), "--env", "-e"),
    config: Path = typer.Option(Path("config.yaml"), "--config", "-c"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """One-shot analyze a static log file and print a diagnosis."""
    _setup_logging(verbose)
    if not log_file.exists():
        console.print(f"[red]File not found: {log_file}[/red]")
        raise typer.Exit(2)

    runtime = load_runtime_config(env if env.exists() else None)
    yaml_cfg = load_yaml_config(config) if config.exists() else None

    text = log_file.read_text(encoding="utf-8", errors="replace").splitlines()
    events = [
        LogEvent(source=log_file.name, kind=kind, line=line, parsed=parse_line(kind, line))
        for line in text
        if line.strip()
    ]
    if not events:
        console.print("[yellow]No log lines found.[/yellow]")
        raise typer.Exit(0)

    if yaml_cfg is not None:
        detector = Detector(yaml_cfg.detector)
        from .buffer import LogBuffer

        buf = LogBuffer(capacity=len(events) + 10)
        for ev in events:
            buf.add(ev)
        signals = detector.scan(buf)
        if not signals:
            console.print("[green]No anomalies detected by heuristics.[/green]")
            console.print("Asking LLM for a general review...")
            signals_payload = {
                "kind": "manual_review",
                "title": f"Manual review of {log_file.name}",
                "description": "User-requested ad hoc analysis.",
                "severity": "info",
                "metrics": {},
            }
            excerpt = "\n".join(e.line for e in events[-200:])
            client = build_llm_client(runtime)
            diag = client.diagnose(signals_payload, excerpt)
            console.print_json(json.dumps(diag.to_dict(), ensure_ascii=False))
            return

        client = build_llm_client(runtime)
        for s in signals:
            excerpt = "\n".join(e.line for e in events[-200:])
            payload = {
                "kind": s.kind,
                "title": s.title,
                "description": s.description,
                "severity": s.severity,
                "metrics": s.metrics,
            }
            diag = client.diagnose(payload, excerpt)
            console.rule(f"[bold]{s.title}[/bold]")
            console.print_json(json.dumps({"signal": payload, "diagnosis": diag.to_dict()}, ensure_ascii=False))


@app.command("self-test")
def self_test() -> None:
    """Validate config files and connectivity to Docker and the LLM provider."""
    runtime = load_runtime_config(".env" if Path(".env").exists() else None)
    cfg_path = Path("config.yaml")
    if not cfg_path.exists():
        console.print("[yellow]config.yaml not found (using example).[/yellow]")
        cfg_path = Path("config.example.yaml")
    yaml_cfg = load_yaml_config(cfg_path)
    console.print(f"[green]✓[/green] Loaded {cfg_path}: "
                  f"{len(yaml_cfg.sources.docker)} docker, {len(yaml_cfg.sources.files)} files.")

    try:
        import docker  # type: ignore[import-untyped]

        client = docker.from_env()
        client.ping()
        console.print("[green]✓[/green] Docker daemon reachable.")
    except Exception as exc:  # noqa: BLE001
        console.print(f"[red]✗[/red] Docker not reachable: {exc}")

    client = build_llm_client(runtime)
    console.print(f"[green]✓[/green] LLM client: {type(client).__name__}")


def entry() -> None:
    try:
        app()
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    entry()
