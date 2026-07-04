# MCP-Atlas: A Large-Scale Benchmark for Tool-Use Competency with Real MCP Servers

MCP-Atlas evaluates how well AI agents use tools to complete real-world tasks, across 36 Model Context Protocol (MCP) servers in a reproducible Docker sandbox, scored with an LLM-as-judge.

- Paper: [arxiv.org/abs/2602.00933](https://arxiv.org/abs/2602.00933) ([local copy](assets/MCP_Atlas.pdf))
- Leaderboard: [scale.com/leaderboard/mcp_atlas](https://scale.com/leaderboard/mcp_atlas)
- Dataset: [huggingface.co/datasets/ScaleAI/MCP-Atlas](https://huggingface.co/datasets/ScaleAI/MCP-Atlas)

## Overview

- **36 real MCP servers** spanning search, code execution, databases, APIs, and productivity tools — 20 need no setup, 11 require API keys, and 5 require API keys plus data setup (see [`data_exports/README.md`](data_exports/README.md)). All are open-source and version-pinned for reproducibility.
- **500 tasks** with ground-truth expected tool calls and answers.
- **LLM-as-judge scoring** reporting pass rate and coverage, plus per-task failure-mode diagnostics.

Server definitions are in [`mcp_server_template.json`](services/agent-environment/src/agent_environment/mcp_server_template.json); a full list of the 36 servers and 307 tools is [here](https://gist.github.com/geobio/d0272d41ea395376233f1617a3988860).

## Quick Start

Requires [docker](https://www.docker.com/products/docker-desktop/), [jq](https://jqlang.org/download/), and Python 3.10+.

```bash
git clone git@github.com:scaleapi/mcp-atlas.git && cd mcp-atlas
```

### 1. Configure

```bash
cp env.template .env
```

Set in `.env`:
- `LLM_API_KEY` — key for the model under evaluation (comma-separated keys are rotated per request).
- `LLM_BASE_URL` — any OpenAI Chat-Completions-compatible endpoint (a [LiteLLM](https://docs.litellm.ai/) proxy, OpenAI, Anthropic-via-LiteLLM, Azure, or a self-hosted vLLM/TGI server).
- `EVAL_LLM_API_KEY` / `EVAL_LLM_BASE_URL` / `EVAL_LLM_MODEL` — *optional* judge settings for scoring and diagnostics; fall back to `LLM_*`, with the judge defaulting to `gemini/gemini-3.1-pro-preview`.
- `MCP_SANDBOX_URL` — *optional*, defaults to `http://localhost:1984`.

> The agent harness was rewritten from Python to TypeScript in v2.0.0 — see [`CHANGELOG.md`](CHANGELOG.md).

### 2. Start the MCP servers

Allocate at least 8 GB (10 GB+ recommended) to Docker.

**Option A — prebuilt image (recommended):**
```bash
docker pull ghcr.io/scaleapi/mcp-atlas:1.2.5
docker tag ghcr.io/scaleapi/mcp-atlas:1.2.5 agent-environment:latest
make run-docker
```

**Option B — build from source** (only if you're modifying the server set, pinned versions, or baked-in data):
```bash
make build && make run-docker
```

Neither bakes in API keys — both inject them at runtime from `.env`. Startup takes 1+ minute; wait for `Uvicorn running on http://0.0.0.0:1984`. The 20 no-key servers are enabled by default; key-gated servers turn on when their keys are present. Verify:

```bash
curl -s http://localhost:1984/enabled-servers | jq -c
```

### 3. Start the agent harness (new terminal)

```bash
make install-harness
make run-harness
```

Starts the TypeScript harness on port 3001, exposing `/v2/mcp_eval/run_agent` — the multi-turn agent loop that runs the model against the sandbox until it finishes or hits a limit.

### 4. Smoke-test one task (new terminal)

Expected answer: "Customer".

```bash
curl -X POST http://localhost:3001/v2/mcp_eval/run_agent \
  -H "Content-Type: application/json" \
  -d '{
    "model": "openai/gpt-4o",
    "messages": [{"role": "user", "content": "What is the first word of the file at /data/Barber Shop.csv?"}],
    "enabledTools": ["filesystem_read_text_file"],
    "image": "ghcr.io/scaleapi/mcp-atlas:1.2.5"
  }' | jq
```

### 5. Run the full eval

```bash
make install-python   # one-time: deps for run_eval, scoring, diagnostics
python run_eval.py --model "openai/gpt-4o" --output outputs.csv
```

Pulls the 500-task dataset from HuggingFace by default; pass `--input tasks.csv` for a local CSV (`TASK`, `PROMPT`, `ENABLED_TOOLS` columns). Reruns skip already-completed `task_id`s, so an interrupted run resumes by rerunning the same command. Output columns: `task_id`, `raw_conversation_history`, `response`.

Keep each run's artifacts together by writing `--output` into a per-run directory and pointing the scoring/diagnosis steps at the same directory.

#### Configuration

Override any default per run:

| Flag | Default | What it does |
|------|---------|--------------|
| `--max-turns N` | `256` | Max agent-loop iterations per task. |
| `--max-tool-calls N` | `100` | Max total tool calls per task. |
| `--tool-output-cap N` | uncapped | Truncate each tool result to N characters before it's fed back to the model. |
| `--context-window-management compact` | off | Summarize older turns once the conversation grows large. |
| `--extra-llm-params '<json>'` | none | Forward a JSON object verbatim into the completion request (e.g. reasoning level). |
| `--system-prompt "..."` | none | Prepend a system message to every task. |
| `--concurrency N` | `5` | Tasks run in parallel. |
| `--timeout S` | `1800` | Per-task timeout, in seconds. |
| `--num-tasks N` | all | Run only the first N tasks. |
| `--input PATH` | HuggingFace | Use a local CSV instead of `ScaleAI/MCP-Atlas`. |
| `--image NAME` | `ghcr.io/scaleapi/mcp-atlas:1.2.5` | Sandbox image. |
| `--skip-health-check` | off | Skip the pre-flight sandbox health check. |

- `--extra-llm-params` sets reasoning/provider-specific options, e.g. `--extra-llm-params '{"reasoning_effort": "high"}'` (use whatever key your provider expects; default is the provider's own).
- Harness request timeouts are env-configurable for slow models: `TOOL_CALL_TIMEOUT_MS` (60000), `LIST_TOOLS_TIMEOUT_MS` (180000), `LLM_TIMEOUT_MS` (600000).
- Each run writes a `run_config.json` beside the output CSV; the scorer embeds it into `coverage_stats_*.json` so every result is traceable to its configuration.

### 6. Score

```bash
python services/scoring/score_claims.py \
  --groundtruth-file path/to/groundtruth.csv \
  --model-file outputs.csv \
  --model-name your-model \
  --output-dir results/your-model
```

LLM-as-judge claim-coverage scoring (default judge `gemini/gemini-3.1-pro-preview`). The ground-truth file is the HuggingFace dataset exported to CSV (columns `TASK`, `PROMPT`, `GTFA_CLAIMS`), or the same `--input` CSV if you ran locally. Outputs `scored_<model>.csv`, `coverage_stats_<model>_*.json` (pass rates at 0.50 and 0.75 coverage thresholds), and a coverage histogram. `--concurrency` auto-tunes per judge model.

### 6b. Diagnose failures (optional)

```bash
python services/diagnostics/single_model_diagnostic.py --scored-file scored_<model>.csv --verbose
```

Classifies each failing task into one of 11 failure modes (4 tool-call + 7 cognitive) over an enriched trajectory, and writes a `diagnosis_*.csv` plus a model-level narrative.

### 7. Evaluate another model

Change `LLM_API_KEY` / `LLM_BASE_URL` in `.env`, restart the harness, and rerun with a different `--model`. See [LiteLLM providers](https://docs.litellm.ai/docs/providers) for model names.

## Scaling throughput

A single sandbox handles concurrent tasks comfortably, and **you can run several evals in parallel against it.** The agent loop is I/O-bound — most of each task's time is spent waiting on the model, not calling tools — so one sandbox stays well under capacity at typical concurrency. Raise `--concurrency` or launch multiple runs as needed; reach for the scale-out options below only when the sandbox itself becomes the bottleneck (very high concurrency, or tool-heavy workloads where some MCP servers degrade under load):

**Shard across independent stacks (simplest).** Run several sandbox + harness pairs on different ports, point `run_eval.py` at a slice of the tasks for each, then concatenate the output CSVs. Each task runs end-to-end on one stack, so within-task state (filesystem, memory, git) stays consistent. The harness's `.env` does not override variables already set in the environment, so per-stack `PORT` / `MCP_SANDBOX_URL` overrides just work:

```bash
# Stack A — sandbox on 1984, harness on 3001
docker run -d -p 1984:1984 --env-file .env ghcr.io/scaleapi/mcp-atlas:1.2.5
PORT=3001 MCP_SANDBOX_URL=http://localhost:1984 make run-harness

# Stack B — sandbox on 1985, harness on 3002
docker run -d -p 1985:1984 --env-file .env ghcr.io/scaleapi/mcp-atlas:1.2.5
PORT=3002 MCP_SANDBOX_URL=http://localhost:1985 make run-harness

# Run each half of the dataset against its own harness, then concatenate
HARNESS_URL=http://localhost:3001 python run_eval.py --input tasks_part_a.csv --output out_a.csv --model "<model>"
HARNESS_URL=http://localhost:3002 python run_eval.py --input tasks_part_b.csv --output out_b.csv --model "<model>"
```

**Point at an orchestrator (scales furthest).** Because the harness reaches the sandbox solely through `MCP_SANDBOX_URL`, you can point it at a service that provisions an ephemeral sandbox per task — no harness changes; any HTTP endpoint implementing the agent-environment API works.

> **One rule when adding sandboxes:** keep all of a task's tool calls on the same sandbox. Per-call load-balancing across replicas breaks stateful tools (filesystem, memory, git, MongoDB), which assume a consistent view within a task.

## What's Included

- **Agent harness** (`services/agent-harness/`, TypeScript) — multi-turn agent loop, talks to the sandbox via `MCP_SANDBOX_URL`.
- **Agent environment** (`services/agent-environment/`, Python) — Dockerized sandbox serving the 36 MCP servers over HTTP.
- **Scoring** (`services/scoring/`, Python) — LLM-as-judge claim-coverage scoring.
- **Diagnostics** (`services/diagnostics/`, Python) — failure-mode classification across an 11-mode taxonomy.

## Citation

If you use MCP-Atlas in your research, please cite:

```bibtex
@misc{bandi2026mcpatlas,
  title         = {MCP-Atlas: A Large-Scale Benchmark for Tool-Use Competency with Real MCP Servers},
  author        = {Bandi, Chaithanya and Dumitru, Razvan-Gabriel and Hertzberg, Ben and Agarwal, Divyansh and Boo, Geobio and Polakam, Tejas and Hassaan, Sami and Da, Jeff and Kim, HiJae and Gupta, Vipul and Sharma, Manasi and Park, Andrew and Dimakis, Martin and Hernandez Montoya, Ernesto Gabriel and Rambado, Dan and Salazar, Ivan and Cruz, Rafael and Rezaei, MohammadHossein and Rane, Chetan and Levin, Ben and Zhang, Daniel Yue and Kenstler, Brad and Liu, Bing},
  year          = {2026},
  eprint        = {2602.00933},
  archivePrefix = {arXiv},
  primaryClass  = {cs.SE},
  url           = {https://arxiv.org/abs/2602.00933}
}
```
