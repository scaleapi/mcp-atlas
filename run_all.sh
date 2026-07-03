#!/usr/bin/env bash
#
# run_all.sh — bring up the full MCP-Atlas eval chain with one command.
#
# Orchestrates the three pieces that otherwise run in separate terminals:
#   1. agent-environment sandbox (Docker, port 1984)    — the MCP servers
#   2. agent-harness            (TypeScript, port 3001)  — the agent loop
#   3. run_eval.py                                       — the batch client
#
# This script does NOT do the one-time setup for you — it checks that setup
# is in place and tells you the exact command to run if something is missing.
# Required setup (once):
#   cp env.template .env          # then set LLM_API_KEY and LLM_BASE_URL
#   docker pull ghcr.io/scaleapi/mcp-atlas:1.2.5 \
#     && docker tag ghcr.io/scaleapi/mcp-atlas:1.2.5 agent-environment:latest
#   make install-harness          # npm install for the harness
#   make install-python           # pip install -r requirements.txt
#
# Then this script starts the two long-running services (skipping any that
# are already up), waits for them to become healthy, and runs the eval. On
# exit it tears down only what it started.
#
# Usage:
#   ./run_all.sh --model openai/gpt-4o --output outputs.csv
#   ./run_all.sh --model openai/gpt-4o --num-tasks 5      # quick smoke
#   ./run_all.sh --serve-only                             # just bring services up
#
# Any extra args (e.g. --num-tasks, --input, --concurrency) are passed
# straight through to run_eval.py.
#
set -euo pipefail

SELF="$(cd "$(dirname "$0")" && pwd)/$(basename "$0")"
cd "$(dirname "$SELF")"

SANDBOX_PORT="${SANDBOX_PORT:-1984}"
IMAGE="${IMAGE:-agent-environment:latest}"
SANDBOX_NAME="${SANDBOX_NAME:-mcp-atlas-sandbox}"
OUTPUT="outputs.csv"
MODEL=""
SERVE_ONLY=0
EXTRA_ARGS=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --model)      MODEL="${2:-}"; shift 2;;
    --output)     OUTPUT="${2:-}"; shift 2;;
    --serve-only) SERVE_ONLY=1; shift;;
    -h|--help)    tail -n +2 "$SELF" | grep '^#' | sed 's/^# \{0,1\}//'; exit 0;;
    *)            EXTRA_ARGS+=("$1"); shift;;
  esac
done

PYTHON="${PYTHON:-python3}"
command -v "$PYTHON" >/dev/null 2>&1 || PYTHON=python

die()  { echo "ERROR: $*" >&2; exit 1; }
have() { command -v "$1" >/dev/null 2>&1; }

# Harness port comes from .env (PORT=), default 3001.
HARNESS_PORT=3001
if [[ -f .env ]]; then
  p="$(grep -E '^PORT=' .env | tail -1 | cut -d= -f2- | tr -d '[:space:]' || true)"
  [[ -n "${p:-}" ]] && HARNESS_PORT="$p"
fi

echo "==> Checking prerequisites"
for c in docker curl; do have "$c" || die "'$c' not found on PATH."; done

[[ -f .env ]] || die ".env not found. Run: cp env.template .env  (then set LLM_API_KEY and LLM_BASE_URL)"
grep -Eq '^LLM_API_KEY=.+'  .env || die "LLM_API_KEY is empty in .env (required)."
grep -Eq '^LLM_BASE_URL=.+' .env || die "LLM_BASE_URL is empty in .env (required)."

docker image inspect "$IMAGE" >/dev/null 2>&1 || die \
"Docker image '$IMAGE' not found. Either:
   Option A:  docker pull ghcr.io/scaleapi/mcp-atlas:1.2.5 && docker tag ghcr.io/scaleapi/mcp-atlas:1.2.5 $IMAGE
   Option B:  make build"

[[ -d services/agent-harness/node_modules ]] || die "Harness deps missing. Run: make install-harness"

if [[ "$SERVE_ONLY" -eq 0 ]]; then
  [[ -n "$MODEL" ]] || die "--model is required (e.g. --model openai/gpt-4o). Use --serve-only to just bring services up."
  "$PYTHON" -c 'import aiohttp' 2>/dev/null || die "Python deps missing. Run: make install-python"
fi

sandbox_healthy() { curl -sf "http://localhost:${SANDBOX_PORT}/health" 2>/dev/null | grep -q health_and_client_connection_ok; }
harness_healthy() { curl -sf "http://localhost:${HARNESS_PORT}/health" >/dev/null 2>&1; }

wait_until() {  # wait_until <name> <timeout-seconds> <cmd...>
  local name="$1" timeout="$2"; shift 2
  local waited=0
  printf '==> Waiting for %s ' "$name"
  while ! "$@"; do
    sleep 2; waited=$((waited + 2)); printf '.'
    if (( waited >= timeout )); then echo " TIMEOUT after ${timeout}s"; return 1; fi
  done
  echo " ok (${waited}s)"
}

STARTED_SANDBOX=0
STARTED_HARNESS=0
HARNESS_PID=""
cleanup() {
  echo
  if [[ "$STARTED_HARNESS" -eq 1 && -n "$HARNESS_PID" ]]; then
    echo "==> Stopping harness (pid $HARNESS_PID)"
    pkill -P "$HARNESS_PID" 2>/dev/null || true
    kill "$HARNESS_PID" 2>/dev/null || true
  fi
  if [[ "$STARTED_SANDBOX" -eq 1 ]]; then
    echo "==> Stopping sandbox container '$SANDBOX_NAME'"
    docker stop "$SANDBOX_NAME" >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT INT TERM

LOGDIR="$(mktemp -d)"
echo "==> Logs: $LOGDIR"

# 1. Sandbox -----------------------------------------------------------------
if sandbox_healthy; then
  echo "==> Sandbox already healthy on :$SANDBOX_PORT — reusing it"
else
  echo "==> Starting sandbox '$SANDBOX_NAME' on :$SANDBOX_PORT (first start takes ~1 min)"
  docker rm -f "$SANDBOX_NAME" >/dev/null 2>&1 || true
  docker run -d --rm --name "$SANDBOX_NAME" -p "${SANDBOX_PORT}:1984" --env-file .env "$IMAGE" >/dev/null
  STARTED_SANDBOX=1
  if ! wait_until "sandbox :$SANDBOX_PORT" 240 sandbox_healthy; then
    echo "---- last sandbox logs ----"; docker logs "$SANDBOX_NAME" 2>&1 | tail -40
    die "sandbox did not become healthy"
  fi
fi
echo "==> Enabled servers:"
curl -s "http://localhost:${SANDBOX_PORT}/enabled-servers" 2>/dev/null | { have jq && jq -c || cat; } || true

# 2. Harness -----------------------------------------------------------------
if harness_healthy; then
  echo "==> Harness already healthy on :$HARNESS_PORT — reusing it"
else
  echo "==> Starting harness on :$HARNESS_PORT"
  npm --prefix services/agent-harness run dev >"$LOGDIR/harness.log" 2>&1 &
  HARNESS_PID=$!
  STARTED_HARNESS=1
  if ! wait_until "harness :$HARNESS_PORT" 90 harness_healthy; then
    echo "---- harness logs ----"; tail -40 "$LOGDIR/harness.log" 2>/dev/null || true
    die "harness did not become healthy"
  fi
fi

# 3. Eval (or just serve) ----------------------------------------------------
if [[ "$SERVE_ONLY" -eq 1 ]]; then
  echo
  echo "==> Services are up:"
  echo "      sandbox : http://localhost:${SANDBOX_PORT}"
  echo "      harness : http://localhost:${HARNESS_PORT}"
  echo "    Press Ctrl-C to stop."
  while true; do sleep 3600; done
fi

# Pre-flight: one read-only call per server so you see what's actually working
# before spending time on the eval. Non-fatal — a "failure" here is almost always
# a server whose API key isn't set in .env, which is fine if you don't need it.
echo
echo "==> Checking MCP servers (one read-only call each)"
if ! "$PYTHON" services/mcp_eval/test_servers.py; then
  echo "    NOTE: some servers failed above (usually just missing API keys in .env)."
  echo "    The eval will still run; tasks needing those servers may fail."
fi

# Point run_eval.py at the harness port we actually used.
export HARNESS_URL="http://localhost:${HARNESS_PORT}"

echo
echo "==> Running eval: model=$MODEL output=$OUTPUT ${EXTRA_ARGS[*]:-}"
"$PYTHON" run_eval.py --model "$MODEL" --output "$OUTPUT" "${EXTRA_ARGS[@]+"${EXTRA_ARGS[@]}"}"
echo "==> Eval complete. Output: $OUTPUT"
