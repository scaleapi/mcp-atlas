#!/usr/bin/env bash
#
# push_baked_api_keys.sh
#
# Build an agent-environment Docker image with API keys from .env baked in,
# and push it to your company's internal AWS ECR. Use this when you need to
# deploy somewhere that can't mount --env-file at runtime.
#
# What this script does (5 phases, matching the on-screen "Phase N/5" headers):
#   1. Check that the current branch isn't behind origin/main (fetches with a
#      15s timeout; skips the check if the network is down so you can still
#      build offline, but hard-fails if you ARE reachably behind main).
#   2. Auto-detect your AWS account/region/identity via `aws sts get-caller-identity`
#      and ask you to confirm before pushing to that account.
#   3. Verify .env has the keys expected by mcp_server_template.json
#      (auto-proceed if all present; list missing ones and prompt otherwise).
#   4. Run `make build` (fast via Docker cache if source hasn't changed), then
#      build the baked image on top, tagged baked-YYYY-MM-DD-<git-short-sha>
#      AND :baked-latest.
#   5. Push both tags to <ACCOUNT>.dkr.ecr.<REGION>.amazonaws.com and print
#      instructions for verifying the baked keys actually work.
#
# Run from the repo root:
#     make push-baked-api-keys
# or directly:
#     bash scripts/push_baked_api_keys.sh

set -euo pipefail

# ── Colors ───────────────────────────────────────────────────────────────────
if [ -t 1 ]; then
  BOLD=$'\033[1m'
  RED=$'\033[1;31m'
  YELLOW=$'\033[1;33m'
  GREEN=$'\033[1;32m'
  RESET=$'\033[0m'
else
  BOLD=""; RED=""; YELLOW=""; GREEN=""; RESET=""
fi

err()  { printf "%s\n" "${RED}ERROR:${RESET} $*" >&2; }
info() { printf "%s\n" "$*"; }
warn() { printf "%s\n" "${YELLOW}$*${RESET}"; }
ok()   { printf "%s\n" "${GREEN}$*${RESET}"; }

confirm() {
  # confirm "prompt" — returns 0 on y/Y, 1 otherwise
  local prompt="$1"
  local ans
  printf "%s " "${YELLOW}${prompt}${RESET}"
  read -r ans
  [[ "$ans" == "y" || "$ans" == "Y" ]]
}

# ── Locate repo root ─────────────────────────────────────────────────────────
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

TEMPLATE="services/agent-environment/src/agent_environment/mcp_server_template.json"
ENV_FILE=".env"
DOCKERFILE="Dockerfile.baked"

# ── Helper: git fetch with a portable timeout ────────────────────────────────
# macOS doesn't ship `timeout` by default (Linux does). Try gtimeout (brew
# coreutils), then timeout, then fall back to a bash-only background+kill.
git_fetch_with_timeout() {
  local branch="$1"
  local secs="${2:-15}"
  if command -v gtimeout >/dev/null 2>&1; then
    gtimeout "$secs" git fetch origin "$branch" --quiet 2>/dev/null
  elif command -v timeout >/dev/null 2>&1; then
    timeout "$secs" git fetch origin "$branch" --quiet 2>/dev/null
  else
    ( git fetch origin "$branch" --quiet 2>/dev/null ) &
    local pid=$! waited=0
    while kill -0 "$pid" 2>/dev/null; do
      if [ "$waited" -ge "$secs" ]; then
        kill "$pid" 2>/dev/null || true
        wait "$pid" 2>/dev/null || true
        return 124
      fi
      sleep 1
      waited=$((waited + 1))
    done
    wait "$pid"
    return $?
  fi
}

# ── Phase 1: Branch freshness vs origin/main ─────────────────────────────────
echo
info "${BOLD}=== Phase 1/5: Verify branch is not behind origin/main ===${RESET}"

DEFAULT_BRANCH=$(git symbolic-ref refs/remotes/origin/HEAD 2>/dev/null | sed 's@^refs/remotes/origin/@@' || true)
[ -z "$DEFAULT_BRANCH" ] && DEFAULT_BRANCH="main"
info "  Default branch: ${BOLD}origin/${DEFAULT_BRANCH}${RESET}"

if git_fetch_with_timeout "$DEFAULT_BRANCH" 15; then
  BEHIND=$(git rev-list --count "HEAD..origin/${DEFAULT_BRANCH}" 2>/dev/null || echo "0")
  AHEAD=$(git rev-list --count "origin/${DEFAULT_BRANCH}..HEAD" 2>/dev/null || echo "0")
  info "  Behind origin/${DEFAULT_BRANCH}: ${BEHIND}    Ahead: ${AHEAD}"
  if [ "$BEHIND" -gt 0 ]; then
    err "Your branch is ${BEHIND} commit(s) behind origin/${DEFAULT_BRANCH}."
    err "Merge or rebase origin/${DEFAULT_BRANCH} before building a baked image —"
    err "you don't want to push an image built from stale source."
    err ""
    err "    git merge origin/${DEFAULT_BRANCH}    # or"
    err "    git rebase origin/${DEFAULT_BRANCH}"
    exit 1
  fi
  ok "  Up to date with origin/${DEFAULT_BRANCH}."
else
  warn "  Couldn't fetch origin/${DEFAULT_BRANCH} within 15s (offline? VPN? remote down?)."
  warn "  Skipping freshness check and continuing — make sure you're not on stale code."
fi

# ── Phase 2: AWS identity ────────────────────────────────────────────────────
echo
info "${BOLD}=== Phase 2/5: Verify AWS identity ===${RESET}"

if ! aws sts get-caller-identity >/dev/null 2>&1; then
  err "Not logged in to AWS. Run:"
  err "    aws sso login"
  err "(or whichever auth your team uses) and re-run."
  exit 1
fi

ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
ARN=$(aws sts get-caller-identity --query Arn --output text)
REGION=$(aws configure get region || true)
if [ -z "$REGION" ]; then
  REGION="us-west-2"
  warn "No region in AWS config; defaulting to ${REGION}"
fi
# Extract the trailing identity (typically your email for SSO assumed-roles)
IDENTITY_TAIL="${ARN##*/}"
ECR="${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com"

info "  AWS account:  ${BOLD}${ACCOUNT_ID}${RESET}"
info "  Identity:     ${BOLD}${IDENTITY_TAIL}${RESET}"
info "  Region:       ${BOLD}${REGION}${RESET}"
info "  Will push to: ${BOLD}${ECR}${RESET}"
echo

if ! confirm "Push the baked image to this account & region? [y/N]"; then
  err "Aborted."
  exit 1
fi

# ── Phase 3: .env key check ──────────────────────────────────────────────────
echo
info "${BOLD}=== Phase 3/5: Verify .env has expected API keys ===${RESET}"

if [ ! -f "$ENV_FILE" ]; then
  err ".env not found at repo root."
  err "Create one (copy from your team's secret store) and re-run."
  exit 1
fi

if [ ! -f "$TEMPLATE" ]; then
  err "Template not found: $TEMPLATE"
  exit 1
fi

# Expected: every ${VAR} reference in the template.
EXPECTED_KEYS=$(grep -oE '\$\{[A-Z_][A-Z0-9_]*\}' "$TEMPLATE" | sed 's/[${}]//g' | sort -u)
EXPECTED_COUNT=$(echo "$EXPECTED_KEYS" | wc -l | tr -d ' ')

PRESENT_COUNT=0
MISSING_KEYS=()
for key in $EXPECTED_KEYS; do
  # Look for KEY=<non-empty> in .env (ignore commented lines)
  if grep -qE "^${key}=.+" "$ENV_FILE"; then
    PRESENT_COUNT=$((PRESENT_COUNT + 1))
  else
    MISSING_KEYS+=("$key")
  fi
done

info "  Expected: ${EXPECTED_COUNT} keys (from ${TEMPLATE})"
info "  Present:  ${BOLD}${PRESENT_COUNT}/${EXPECTED_COUNT}${RESET} in .env"

if [ ${#MISSING_KEYS[@]} -eq 0 ]; then
  ok "  All keys present — auto-proceeding."
else
  warn "  Missing or empty:"
  for k in "${MISSING_KEYS[@]}"; do
    warn "    - $k"
  done
  warn "  MCP servers requiring these keys will fail when the image runs."
  echo
  if ! confirm "Proceed with the build anyway? [y/N]"; then
    err "Aborted. Populate the missing keys in .env and re-run."
    exit 1
  fi
fi

# ── Phase 4: Build source image + baked image ────────────────────────────────
echo
info "${BOLD}=== Phase 4/5: Build source image, then bake API keys on top ===${RESET}"

# Always run `make build` to guarantee the source image matches current code.
# Docker layer caching makes this fast (~seconds) if nothing changed; slow only
# when there's actual work. On Apple Silicon it builds amd64 via QEMU emulation.
info "  Running 'make build' to ensure agent-environment:latest is current..."
make build

GIT_SHA=$(git rev-parse --short HEAD 2>/dev/null || echo "nogit")
DATE=$(date -u +%Y-%m-%d)
TAG="baked-${DATE}-${GIT_SHA}"
IMAGE="agent-environment:${TAG}"

info "  Tag: ${BOLD}${TAG}${RESET}"

# Assemble a tiny build context in a temp dir containing just the files we need:
# .env, the Dockerfile, and entrypoint-baked.sh. This lets us `COPY .env` directly
# from the Dockerfile (.env is excluded by the repo's .dockerignore, so we can't
# build from the repo root). Also keeps the context tiny → faster build.
# The temp dir is cleaned up on any exit (success, failure, Ctrl-C).
BUILD_CTX=$(mktemp -d)
cleanup() {
  rm -rf "$BUILD_CTX"
}
trap cleanup EXIT

cp "$ENV_FILE"            "$BUILD_CTX/.env"
cp "$DOCKERFILE"          "$BUILD_CTX/Dockerfile.baked"
cp entrypoint-baked.sh    "$BUILD_CTX/entrypoint-baked.sh"

DOCKER_DEFAULT_PLATFORM=linux/amd64 docker build \
  --platform linux/amd64 \
  -t "$IMAGE" \
  -t "agent-environment:baked-latest" \
  -f "$BUILD_CTX/Dockerfile.baked" \
  "$BUILD_CTX"

ok "  Built: $IMAGE (also tagged agent-environment:baked-latest)"

# ── Phase 5: Push to ECR ─────────────────────────────────────────────────────
echo
info "${BOLD}=== Phase 5/5: Push to ECR ===${RESET}"

info "  Authenticating to ${ECR}..."
aws ecr get-login-password --region "$REGION" | \
  docker login --username AWS --password-stdin "$ECR" >/dev/null

REMOTE_IMAGE="${ECR}/agent-environment:${TAG}"
REMOTE_LATEST="${ECR}/agent-environment:baked-latest"
info "  Tagging for remote: ${REMOTE_IMAGE} + ${REMOTE_LATEST}"
docker tag "$IMAGE" "$REMOTE_IMAGE"
docker tag "$IMAGE" "$REMOTE_LATEST"

info "  Pushing ${TAG}..."
docker push "$REMOTE_IMAGE"
info "  Pushing baked-latest..."
docker push "$REMOTE_LATEST"

# ── Done ─────────────────────────────────────────────────────────────────────
echo
ok "${BOLD}✓ Successfully pushed ${REMOTE_IMAGE}${RESET}"
echo
warn "════════════════════════════════════════════════════════════════════════"
warn "  IMPORTANT: This image has real API keys baked in. To verify they"
warn "  actually work (not just that they're present in .env), do this:"
warn ""
warn "    1. Run the image locally:"
warn "         docker run --rm -d --platform linux/amd64 -p 1984:1984 \\"
warn "           --name baked-test agent-environment:baked-latest"
warn ""
warn "    2. Wait ~3 minutes for all MCP servers to initialize, then health-check:"
warn "         curl http://localhost:1984/health"
warn ""
warn "    3. Run the per-server test suite (calls each MCP server once):"
warn "         cd services/mcp_eval && uv run python test_servers.py"
warn ""
warn "    4. Stop the container when done:"
warn "         docker stop baked-test"
warn ""
warn "  Any test failures here mean a key is wrong or expired — fix .env and"
warn "  re-run 'make push-baked-api-keys' before anyone uses this image."
warn "════════════════════════════════════════════════════════════════════════"
echo
