# syntax=docker/dockerfile:1.7

# ---------- Stage 1: build a clean virtualenv ----------
FROM python:3.12-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /build

RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential \
 && rm -rf /var/lib/apt/lists/*

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY requirements.txt .
RUN pip install --upgrade pip \
 && pip install -r requirements.txt


# ---------- Stage 2: minimal runtime ----------
FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH" \
    AI_AGENT_HEALTH_PORT=8787

# coreutils gives us `truncate` for the prune_logs action; tini is a proper PID 1.
RUN apt-get update \
 && apt-get install -y --no-install-recommends coreutils tini curl \
 && rm -rf /var/lib/apt/lists/* \
 && groupadd --system --gid 1000 agent \
 && useradd  --system --uid 1000 --gid 1000 --home /app --shell /usr/sbin/nologin agent

WORKDIR /app

COPY --from=builder /opt/venv /opt/venv
COPY --chown=agent:agent src ./src
COPY --chown=agent:agent run.py ./run.py
COPY --chown=agent:agent config.example.yaml ./config.example.yaml

# /app must be writable by the agent for incidents.jsonl (bind-mounted in compose).
RUN install -d -o agent -g agent /app/state

USER agent

EXPOSE 8787

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD curl -fsS "http://localhost:${AI_AGENT_HEALTH_PORT}/health" || exit 1

ENTRYPOINT ["/usr/bin/tini", "--", "python", "run.py"]
CMD ["run", "--config", "/app/config.yaml", "--env", "/app/.env"]
