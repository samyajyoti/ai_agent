#!/usr/bin/env sh
# Entrypoint that grants the unprivileged `agent` user permission to read the
# Docker socket without baking a hard-coded gid into the image.
#
# Why: on the host, /var/run/docker.sock is typically root:docker (660). The
# group id of "docker" varies (Debian: 999, Ubuntu: 998, RHEL: 994, Docker
# Desktop on macOS: something else). We detect it at start-up, create a
# matching group inside the container, add `agent` to it, then drop to that
# user via gosu.
set -e

SOCKET_PATH="${DOCKER_SOCKET_PATH:-/var/run/docker.sock}"

if [ -S "$SOCKET_PATH" ]; then
    SOCK_GID="$(stat -c '%g' "$SOCKET_PATH" 2>/dev/null || echo 0)"
    if [ "$SOCK_GID" != "0" ] && [ -n "$SOCK_GID" ]; then
        # Make sure a group with that gid exists.
        if ! getent group "$SOCK_GID" > /dev/null 2>&1; then
            groupadd --system --gid "$SOCK_GID" docker-host || true
        fi
        GROUP_NAME="$(getent group "$SOCK_GID" | cut -d: -f1)"
        # Add agent to that group (idempotent).
        if ! id agent | grep -q "($GROUP_NAME)"; then
            usermod -aG "$GROUP_NAME" agent || true
        fi
    fi
fi

# Make /app writable for incidents.jsonl on bind-mounted volumes.
if [ -e /app/incidents.jsonl ]; then
    chown agent:agent /app/incidents.jsonl 2>/dev/null || true
fi

exec gosu agent "$@"
