# Changelog

## v2.0.0 — Agent harness migration to TypeScript

### What changed

The agent harness has been rewritten from Python to TypeScript. The previous
Python harness (`services/mcp_eval/mcp_completion/`) is removed and replaced by
a TypeScript implementation under `services/agent-harness/`.

Scoring (`services/scoring/`) and the new diagnostic pipeline
(`services/diagnostics/`) remain in Python.

### Why TypeScript

- **Pluggable provider strategies.** Each model provider lives in a single
  strategy file under `agent-evals/strategies/`. The included LiteLLM strategy
  works with any model reachable via a LiteLLM-compatible proxy (OpenAI,
  Anthropic, Gemini, self-hosted endpoints, etc.). Adding a new provider is a
  single-file change.
- **Better concurrency for parallel task evaluation.** Node's event loop lets
  one harness instance run many tasks against the sandbox concurrently
  without thread/process orchestration overhead.
- **Hot reload during development.** `npm run dev` picks up source changes
  automatically — useful when iterating on a strategy.
- **Per-strategy retry policy.** The LiteLLM strategy handles 429 / 401 / 5xx
  backoff appropriately, including LiteLLM proxy token-cache delays.

### Scaling beyond local docker

By default the TypeScript harness talks to a local docker container running
`ghcr.io/scaleapi/mcp-atlas`, accessed via the `MCP_SANDBOX_URL` env var
(default `http://localhost:1984`). A single sandbox is fine for development
and smaller evaluations.

For large-scale runs (many tasks in parallel, each on its own sandbox), the
harness can be combined with a **sandbox orchestrator** that creates an
ephemeral sandbox per task and exposes its URL via `MCP_SANDBOX_URL`. Any
orchestrator that returns an HTTP endpoint implementing the agent-environment
API will work — the harness needs no changes. Scale's internal eval
infrastructure uses a Modal-based proxy for this; that proxy is not included
in this release, but the public harness is designed to plug into one.

### New: top-level batch runner

- `run_eval.py` — drives the agent harness over the full dataset and writes a
  CSV in the format the scoring step expects (`task_id`,
  `raw_conversation_history`, `response`). Defaults to loading from HuggingFace
  (`ScaleAI/MCP-Atlas`, 500 tasks). Supports concurrent task execution and
  resumes safely if interrupted. See `make install-python` and
  `python run_eval.py --help`.

### New: scoring and diagnostic pipelines

- `services/scoring/score_claims.py` — LLM-as-judge coverage scoring with
  configurable evaluator model (default: `gemini/gemini-3.1-pro-preview`).
  Reports `pass_rate_0.75` and `mean_coverage` per split.
- `services/scoring/analyze_errors.py` — error distribution summary across
  completion runs.
- `services/diagnostics/single_model_diagnostic.py` — the headline addition:
  scoring tells you *that* a model failed; diagnostics tell you *how*. For every
  below-threshold task, an LLM judge reads an **enriched trajectory** (turn-by-turn
  tool calls, parameters, status, errors, and output summaries — not the raw
  transcript) and classifies the failure into one of **11 modes**:
  - **4 tool-call modes** — `malformed_call`, `wrong_tool`, `no_tool_use`,
    `err_recovery`.
  - **7 cognitive modes** — `task_misunderstanding`, `faulty_synthesis`,
    `response_misparsing`, `early_termination`, `hallucinated_fact`,
    `logical_error`, `constraint_violation`.

  Each task gets a primary failure plus any contributing failures (each with a
  root-cause flag), a calibrated confidence, and a scorer-grounded explanation.
  These roll up to a model-level tool-call-vs-cognitive split, a full
  failure-mode distribution, public-split examples per mode, and an
  LLM-generated narrative. Runs with no model-attributable failure (infra
  errors, empty output) are labelled `analysis_error` and excluded from the
  reported stats. The taxonomy in `mcp_failure_taxonomy.py` is the single
  source of truth for both diagnosis and reporting.
- `services/diagnostics/extract_enriched_trajectory.py` — builds the enriched
  trajectory (from the raw conversation history) that the diagnostic reads;
  also backfills it onto existing scored CSVs.

### Verification helpers

- `services/mcp_eval/test_servers.py` — health-check across every MCP server
  defined in `mcp_server_template.json`. Makes one representative call per
  server, reports pass/fail, and explicitly flags any API keys missing from
  `.env`. Run after adding keys to confirm everything works:
  ```bash
  python test_servers.py
  ```

### Provider strategy

- **LiteLLM** (`agent-evals/strategies/litellm-strategy.ts`) — generic OpenAI
  Chat Completions API client. Configure with `LLM_API_KEY` and
  `LLM_BASE_URL`. Comma-separated keys are rotated per-request.

To add a new provider, implement the `AgentCompletionStrategy` interface in
a new file under `agent-evals/strategies/` and dispatch to it from
`agent-evals/completion-strategy.ts`. Use `litellm-strategy.ts` as a
reference.

### Removed

- `services/mcp_eval/mcp_completion/` (the entire Python harness)
- `services/mcp_eval/mcp_completion_script.py`
- `services/mcp_eval/mcp_evals_scores.py` (replaced by `services/scoring/score_claims.py`)
- `services/mcp_eval/run.py` (a replacement public run script will land in a
  follow-up release)
- The `run-mcp-completion` Makefile target

### Environment variables

| Variable | Used by | Notes |
|----------|---------|-------|
| `LLM_API_KEY` | agent-harness | Required. Comma-separated keys are rotated. |
| `LLM_BASE_URL` | agent-harness | Required. URL of an OpenAI-compatible (or LiteLLM proxy) endpoint. |
| `MCP_SANDBOX_URL` | agent-harness | Default `http://localhost:1984`. Points at the running `agent-environment` container. |
| `EVAL_LLM_API_KEY` | scoring, diagnostics | Falls back to `LLM_API_KEY`. |
| `EVAL_LLM_BASE_URL` | scoring, diagnostics | Falls back to `LLM_BASE_URL`. |
| `EVAL_LLM_MODEL` | scoring, diagnostics | Default judge model. |
