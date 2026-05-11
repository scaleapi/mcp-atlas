# Makefile for Agent Environment

IMAGE_NAME = agent-environment
VERSION = 1.2.5
GHCR_REPO = ghcr.io/scaleapi/mcp-atlas

.PHONY: build run run-docker shell test run-mcp-completion push push-baked-api-keys

run-docker: # run docker container for mcp servers (agent-environment service)
	docker run --rm --platform linux/amd64 -p 1984:1984 --env-file .env $(IMAGE_NAME):latest

build: # builds agent-environment
	cd services/agent-environment && docker buildx build --platform linux/amd64 -t $(IMAGE_NAME) .
	docker tag $(IMAGE_NAME):latest $(IMAGE_NAME):$(VERSION)

shell: # shell for agent-environment
	docker run -it --rm --platform linux/amd64 --env-file .env $(IMAGE_NAME):latest bash


# Makefile for MCP Eval

# Run the MCP completion server (port 3000, http post endpoint at /v2/mcp_eval/run_agent)
# Note: This runs agent completions (not evaluation/scoring). For scoring, see mcp_evals_scores.py
run-mcp-completion: 
	cd services/mcp_eval && uv run python -m mcp_completion.main

# Build and push multi-arch image to ghcr.io
# Requires Docker, and may not work with Rancher Desktop
# First do: docker login ghcr.io
push:
	@echo "--- Building and pushing multi-arch $(GHCR_REPO):$(VERSION) and :latest ---"
	cd services/agent-environment && docker buildx build --platform linux/amd64,linux/arm64 \
		-t $(GHCR_REPO):$(VERSION) \
		-t $(GHCR_REPO):latest \
		--push .
	@echo "✓ Successfully pushed to $(GHCR_REPO):$(VERSION)"

# Build an agent-environment image with API keys from .env baked into it, and push
# it to your company's internal AWS ECR. INTERNAL USE ONLY — the resulting image
# contains real secrets and must never be pushed to a public registry like ghcr.io.
#
# Use this when you need to deploy to an environment that can't mount --env-file
# (some k8s setups, certain CI runners, etc.). For normal deployments prefer the
# regular `make push` flow which keeps secrets out of the image.
#
# What this target does (the script handles all of it with confirmations):
#   1. Auto-detects your AWS account ID and region (no hardcoded account numbers),
#      shows you who you're authenticated as, and asks before pushing.
#   2. Counts API keys present in .env vs the ~21 expected by mcp_server_template.json.
#   3. Builds the image tagged baked-YYYY-MM-DD-<git-short-sha>.
#   4. Pushes to <your-account-id>.dkr.ecr.<region>.amazonaws.com/agent-environment.
#   5. Prints instructions for verifying the baked keys actually work.
#
# Prerequisites:
#   - `aws sso login` (or equivalent) — must be authenticated to the target account.
#   - .env must exist at the repo root with API keys populated.
# (The script auto-runs `make build` for you, so you don't need to do it first.)
push-baked-api-keys:
	@bash scripts/push_baked_api_keys.sh