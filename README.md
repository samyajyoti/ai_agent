# AI Ops Agent

An AI-powered SRE sidekick for the `insurance-api` stack. It tails:

- Docker container logs (the API, Redis, anything else you list)
- Application/API logs
- Nginx access + error logs

…detects anomalies (5xx spikes, latency regressions, critical patterns, error
bursts), asks an LLM (OpenAI or Anthropic) for a root-cause diagnosis and
suggested fix, and — when you allow it — runs a small set of safe remediation
actions (restart container, scale service, truncate huge log files, or just
notify).

Everything is **dry-run by default**. Nothing destructive happens until you
explicitly turn it on per action.

## Monitor just one app (simplest setup)

```bash
cd ai-agent
cp .env.example .env                          # add your OPENAI_API_KEY
cp config.single-app.yaml config.yaml         # edit `container:` to your app
docker ps --format '{{.Names}}'               # find the exact container name

python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python run.py run
```

That's it. The agent will tail one container, run the detector + LLM on its
log stream, and write incidents to `incidents.jsonl`. Add more sources later
by editing `config.yaml` — nothing else changes.

---

## Quick start (local)

```bash
cd ai-agent
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env                  # add your OPENAI_API_KEY
cp config.example.yaml config.yaml    # edit your containers + nginx paths

python run.py self-test               # sanity-check docker + LLM
python run.py run                     # start the agent
```

Analyze a static log file in one shot:

```bash
python run.py analyze /var/log/nginx/access.log --kind nginx_access
python run.py analyze ../log/api.log --kind api
```

## Running as a Docker sidecar

```bash
cd ai-agent
cp .env.example .env && cp config.example.yaml config.yaml
docker compose up -d --build
docker logs -f ai-ops-agent
```

The container mounts the Docker socket read-only so it can tail other
containers' logs. To allow it to *restart* containers, mount it read-write and
add `restart_container` to `ALLOWED_ACTIONS`.

## How it works

```
┌───────────────┐   ┌──────────┐   ┌──────────┐   ┌──────────────┐   ┌────────────┐
│ Docker logs   │──▶│          │──▶│          │──▶│              │──▶│  Action    │
│ Nginx access  │──▶│  Buffer  │──▶│ Detector │──▶│  LLM diagnose│──▶│  runner    │
│ API/app logs  │──▶│          │──▶│          │──▶│              │──▶│ (allowlist)│
└───────────────┘   └──────────┘   └──────────┘   └──────────────┘   └────────────┘
                                                                          │
                                                          incidents.jsonl │
                                                          Slack webhook   ▼
                                                          Console (rich)
```

1. **Collectors** stream lines from Docker and files into a thread-safe ring
   buffer (`src/log_sources.py`, `src/buffer.py`).
2. **Detector** scans the buffer every `SCAN_INTERVAL_SECONDS` and emits
   `Signal`s when thresholds in `config.yaml` are exceeded
   (`src/detector.py`).
3. **LLM** receives the signal plus a tight log excerpt and returns a strict
   JSON object with `root_cause`, `impact`, `suggested_action`,
   `action_args`, and an optional `code_or_config_fix`
   (`src/llm.py`).
4. **Action runner** executes only actions in `ALLOWED_ACTIONS`, and only when
   `DRY_RUN=false` (`src/actions.py`).
5. **Sinks** persist every incident as JSONL and (optionally) post a summary
   to Slack (`src/notify.py`).

## Safety model

- `DRY_RUN=true` (default) → the agent prints what it *would* do, never
  executes destructive commands.
- `ALLOWED_ACTIONS` is an allowlist. Unknown actions or anything from the LLM
  that isn't in the list is silently downgraded to `notify_only`.
- `scale_service` refuses replica counts outside `[0, 10]`.
- Incident cooldown (`INCIDENT_COOLDOWN_SECONDS`) prevents action storms when
  the same signature fires repeatedly.
- All decisions are logged to `incidents.jsonl` for audit.

## Configuration

### `.env`

| Variable | Meaning |
| --- | --- |
| `LLM_PROVIDER` | `openai`, `anthropic`, or `none` (heuristic-only) |
| `OPENAI_API_KEY` / `OPENAI_MODEL` | OpenAI credentials |
| `ANTHROPIC_API_KEY` / `ANTHROPIC_MODEL` | Anthropic credentials |
| `DRY_RUN` | `true` (default) prevents any side-effect actions |
| `ALLOWED_ACTIONS` | CSV of action ids the agent may run |
| `SCAN_INTERVAL_SECONDS` | How often to scan the buffer |
| `INCIDENT_COOLDOWN_SECONDS` | Per-signature cooldown |
| `SLACK_WEBHOOK_URL` | Optional Slack incoming webhook |

### `config.yaml`

```yaml
sources:
  docker:
    - { name: insurance-api, container: insurance-api-dev, kind: api }
    - { name: insurance-redis, container: insurance-redis, kind: generic }
  files:
    - { name: nginx-access, path: /var/log/nginx/access.log, kind: nginx_access }
    - { name: nginx-error, path: /var/log/nginx/error.log,  kind: nginx_error  }
  http_health:
    - name: testodsy-health
      url: https://testodsy.wrtual.in/getHealth/
      interval_seconds: 30
      expected_value: working

detector:
  window_seconds: 60
  nginx_5xx_rate_threshold: 0.05
  api_error_count_threshold: 10
  nginx_p95_latency_seconds: 2.5
  critical_patterns:
    - "panic"
    - "out of memory"
    - "ECONNREFUSED"
```

## Allowlisted actions

| id | What it does | Required args |
| --- | --- | --- |
| `notify_only` | Just record + notify, never modify state | – |
| `restart_container` | `docker restart <container>` | `container` |
| `scale_service` | `docker compose ... --scale name=N` | `service`, `replicas` (≤ 10) |
| `prune_logs` | Truncate a container's JSON log file | `container` |

## CLI

```bash
python run.py run         # main loop
python run.py analyze     # one-shot diagnose a log file
python run.py self-test   # validate config + connectivity
```

## Troubleshooting

### `PermissionError(13, 'Permission denied')` on the Docker socket

The agent process can see `/var/run/docker.sock` but can't open it. The Docker
socket on the host is `root:docker` (mode 660), and the group id varies per
distro (Debian 999, Ubuntu 998, RHEL 994, Docker Desktop different again).

**In a container** (this repo's `compose.yml` / `Dockerfile`):
the entrypoint auto-detects the socket's gid and adds the unprivileged
`agent` user to a group with that gid before starting Python. If you still
see the error:

```bash
docker compose down
docker compose build --no-cache
docker compose up -d
docker exec ai-ops-agent id agent     # should list a group with the host docker gid
```

**Locally on Linux**: add yourself to the `docker` group and re-login:

```bash
sudo usermod -aG docker "$USER"
newgrp docker     # or log out and back in
```

**Locally on macOS**: ensure Docker Desktop is running. The agent talks to it
via `/var/run/docker.sock`, which Docker Desktop exposes once the app is up.

### Nginx file source shows no events

The path must be readable *inside the agent container*. In `compose.yml`
uncomment the bind-mount, e.g.:

```yaml
volumes:
  - /var/log/nginx:/var/log/nginx:ro
```

### HTTP health source reports `non_json` or `transport_error`

- `non_json`: the endpoint returned HTML or text. Make sure the URL really
  returns JSON (open it in a browser or `curl -i`).
- `transport_error`: DNS, TLS or network problem. If the endpoint uses a
  self-signed certificate, set `verify_ssl: false` on that source.
- Behind auth? Add headers under that source's `headers:` block.

## Limitations

- The agent reasons over a rolling window in memory; it is not a metrics
  store. Pair it with Prometheus/Loki if you need long-term analytics.
- LLM responses are parsed strictly as JSON. If the model returns prose, the
  agent falls back to `notify_only`.
- Truncating container logs requires the agent to run with sufficient
  privileges on the host (typically root-equivalent).
