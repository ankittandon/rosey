#!/usr/bin/env bash
# Container entrypoint that launches both processes side-by-side:
#   1. Baileys sidecar (Node.js) on loopback :3001 + outbound forwarder
#      to Python on :8080
#   2. Hypercorn (Python/Quart) on :8080 — public-facing
#
# We send SIGTERM to both children when the container is shutting down,
# wait for either to exit, then exit ourselves with whichever code came
# first. Fly's container-restart will bring everything back fresh.
set -uo pipefail

cleanup() {
  echo "[start] received signal, shutting down children"
  # Be polite first, kill -9 if needed
  [[ -n "${BAILEYS_PID:-}" ]] && kill -TERM "$BAILEYS_PID" 2>/dev/null || true
  [[ -n "${HYPERCORN_PID:-}" ]] && kill -TERM "$HYPERCORN_PID" 2>/dev/null || true
  [[ -n "${MCP_PID:-}" ]] && kill -TERM "$MCP_PID" 2>/dev/null || true
  wait
  exit 0
}
trap cleanup SIGTERM SIGINT

# Skip Baileys entirely if the operator hasn't opted in. The Cloud API
# path still works for 1:1 WhatsApp; Baileys is only needed for groups.
if [[ "${BAILEYS_MODE:-off}" == "on" ]]; then
  echo "[start] launching baileys sidecar"
  ( cd /app/baileys && node index.js ) &
  BAILEYS_PID=$!
  echo "[start] baileys pid=$BAILEYS_PID"
else
  echo "[start] BAILEYS_MODE=off — skipping baileys sidecar (Cloud API only)"
fi

# Co-located MCP server (port 8089) — exposes /data/memories as MCP tools so
# the voice PWA and other MCP clients share the household's memory. Supervised
# in a restart loop so an MCP crash NEVER takes down the bot (the loop keeps
# it out of the `wait -n` that crashes the container on a critical-child exit).
echo "[start] launching mcp server (supervised, :8089)"
(
  while true; do
    python /app/mcp_server.py
    echo "[start] mcp server exited, restarting in 2s"
    sleep 2
  done
) &
MCP_PID=$!
echo "[start] mcp supervisor pid=$MCP_PID"

echo "[start] launching hypercorn"
hypercorn server:asgi_app \
  --bind "[::]:8080" \
  --access-logfile - \
  --error-logfile - &
HYPERCORN_PID=$!
echo "[start] hypercorn pid=$HYPERCORN_PID"

# Wait for either child to exit. Whichever one dies first crashes the
# container — Fly will restart and we get a clean state.
wait -n
EXIT=$?
echo "[start] one child exited with code=$EXIT, tearing down siblings"
cleanup
