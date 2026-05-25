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

# coreutils -> `truncate` for prune_logs
# tini      -> proper PID 1 / signal handling
# curl      -> HEALTHCHECK
# gosu      -> drop privileges from root to `agent` after entrypoint setup
RUN apt-get update \
 && apt-get install -y --no-install-recommends coreutils tini curl gosu \
 && rm -rf /var/lib/apt/lists/* \
 && groupadd --system --gid 1000 agent \
 && useradd  --system --uid 1000 --gid 1000 --home /app --shell /usr/sbin/nologin agent

WORKDIR /app

COPY --from=builder /opt/venv /opt/venv
COPY --chown=agent:agent src ./src
COPY --chown=agent:agent run.py ./run.py
COPY --chown=agent:agent config.example.yaml ./config.example.yaml
COPY entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh \
 && install -d -o agent -g agent /app/state

# NOTE: we deliberately stay as root here. entrypoint.sh maps the docker
# socket's gid into the container, then execs `gosu agent` to drop privileges.
# Do not add `USER agent` below this line.

EXPOSE 8787

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD curl -fsS "http://localhost:${AI_AGENT_HEALTH_PORT}/health" || exit 1

ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/entrypoint.sh", "python", "run.py"]
CMD ["run", "--config", "/app/config.yaml", "--env", "/app/.env"]
