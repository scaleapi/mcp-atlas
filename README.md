# Agent Environment

A complete setup with ~45 pre-configured Model Context Protocol (MCP) servers for AI agents, plus an agent completion service and evaluation scripts for testing.

Some MCP servers don't require API keys, but many others require you to get your own API keys. See `env.template` for where to get keys.

## Quick Start

This project depends on these CLI tools: [docker](https://www.docker.com/products/docker-desktop/), [uv](https://docs.astral.sh/uv/getting-started/installation/#installation-methods), and python3.

### 1. Configure environment

```bash
cp env.template .env
```

Edit `.env` and set:
- `LLM_API_KEY` - Your OpenAI API key (required for agent completions). If you want to use other LLM models, also set `LLM_BASE_URL`, and LiteLLM is also supported.
- `GEMINI_API_KEY` - Your Google Gemini API key (required for evaluation scoring). Get it from: https://aistudio.google.com/apikey

### 2. Start the MCP servers

**Option A: Use pre-built image (faster, recommended):**

```bash
docker pull --platform linux/amd64 ghcr.io/scaleapi/mcp-atlas:1.2.0
docker tag ghcr.io/scaleapi/mcp-atlas:1.2.0 agent-environment:latest
make run-docker
```

**Option B: Build from source:**

```bash
make build && make run-docker
```

This starts the agent-environment service on port 1984 (takes 1+ minute to initialize). Before continuing, please wait for this to finish, you'll see log "Uvicorn running on http://0.0.0.0:1984". 

By default, 25 servers that don't require API keys are enabled. Servers requiring API keys are auto-enabled only if you've set their keys in `.env`. To see the enabled mcp servers: `curl -s http://localhost:1984/enabled-servers | jq`

Optional: to check what tools are available, you can use this CURL script `./services/agent-environment/dev_scripts/debug_and_concurrency_tests/curl_scripts/mcp__list_tools.sh | jq > list_tools.json ; open list_tools.json`

### 3. Start the completion service (in a new terminal)

```bash
make run-mcp-completion
```

This starts the MCP completion service on port 3000. It provides an API that connects LLMs to the MCP servers, handling the agentic loop: the LLM decides which tools to call, the service executes them via the MCP servers (port 1984), and returns results back to the LLM until the task is complete.

### 4. Test with a simple agent completion

```bash
curl -X POST http://localhost:3000/v2/mcp_eval/run_agent \
  -H "Content-Type: application/json" \
  -d '{
    "model": "openai/gpt-4o",
    "messages": [{"role": "user", "content": "What is the square root of 95?"}],
    "enabledTools": ["calculator_calculate"],
    "maxTurns": 20
  }'
```

### 5. Test with a sample of 10 tasks

**Note:** Steps 5-8 are all run from the `services/mcp_eval/` directory.

```bash
cd services/mcp_eval
```

Run the script with a small sample of 10 tasks. This will use the specified input CSV file. It should be solvable with only the 25 MCP servers that don't require any API keys (enabled by default). For details on servers, see `env.template` and `mcp_server_template.json`.

```bash
uv run python mcp_completion_script.py \
  --model "openai/gpt-4o" \
  --input "sample_tasks.csv" \
  --output "sample_4o_results.csv"
```

Results are saved to `completion_results/sample_4o_results.csv`. 

**Note:** The script automatically skips tasks that are already in the output file. To re-run all tasks, delete or rename the output file first.

Options:
- `--model` - [required] LLM model to use (e.g., `openai/gpt-4o`)
- `--input` or `--input_huggingface` - [required] Input CSV file or HuggingFace dataset name
- `--output` - [required] Output CSV filename (saved to `completion_results/`)
- `--no-filter` - Disable filtering by available MCP servers (runs all tasks regardless of missing API keys)
- `--num-tasks` - Limit to first N tasks (useful for testing)
- `--concurrency` - Maximum concurrent API requests (default: 10, range: 10-30)

Note: For these 10 tasks, they have more servers/tools in "ENABLED_TOOLS", but they are not required to get the correct answer (ground truth claims in "GTFA_CLAIMS"). At the time of creation of each task's prompt and trajectory, all the "ENABLED_TOOLS" were available to the LLM, but the ground truth was determined using only the servers in "TRAJECTORY". However, if you don't provide an API key then that mcp server won't start, and that server's tools will be unavailable to the LLM you're evaluating.

### 6. Evaluate the results

Make sure `GEMINI_API_KEY` is set in `.env` (from step 1).

```bash
uv run mcp_evals_scores.py \
--input-file="completion_results/sample_4o_results.csv" \
--model-name="gpt4o"
```

**Note:** The input file from step 5 contains both ground truth data (TASK, PROMPT, TRAJECTORY, GTFA_CLAIMS) and completion results (script_model_response, trajectory, errors, etc.) in a single file.

Options:
- `--input-file` - [required] Path to completion results CSV from step 5
- `--model-name` - [required] Short name for output files (if identical, will overwrite)
- `--evaluator-model` - Gemini model for scoring (default: `gemini-2.5-pro`)
- `--num-tasks` - Limit to first N tasks (useful for testing)
- `--concurrency` - Number of concurrent Gemini API requests (default: 5)

Outputs saved to `evaluation_results/`:
- `scored_gpt4o.csv` - Coverage scores for each task
- `coverage_stats_gpt4o.csv` - Summary statistics
- `coverage_histogram_gpt4o.png` - Score distribution plot

### 7. Add more API keys (strongly recommended)

Approximately 27% of evaluation tasks work with the 25 default servers. To run more tasks, add API keys to your `.env` file (see `env.template` for setup instructions). Note that a task may require multiple mcp servers, and that task will be skipped if any of its required servers are unavailable. For example, google-workspace is used in 14% of tasks as part of the ground truth trajectory, and without that api key, you'll skip 14% of tasks. API-requiring mcp server usage:

- google-workspace: 14% | brave-search: 12% | balldontlie: 11% | twelvedata: 9%
- notion: 8% | airtable: 6% | alchemy: 6% | google-maps: 6% | mongodb: 5%
- exa: 5% | slack: 5% | oxylabs: 4% | youtube: 3% | lara-translate: 2%
- national-parks: 2% | e2b-server: 2% | github: 1% | rijksmuseum-server: 1%


**Important:** Five servers require both API keys AND sample data to be uploaded to your account:
- **Airtable** - Visit the [shared base](https://airtable.com/appIF9byLfQwdHqE2/shr1KTZOgPl0qQmA8) and click "Copy base"
- **Google Calendar (google-workspace)** - Import `data_exports/calendar_mcp_eval_export.zip` (8KB)
- **Notion** - Import `data_exports/notion_mcp_eval_export.zip` (13MB) via Settings > Import
- **MongoDB** - Restore `data_exports/mongo_dump_video_game_store-UNZIP-FIRST.zip` (486KB) using `mongorestore`
- **Slack** - Import `data_exports/slack_mcp_eval_export_add100days.zip` (43KB) at your workspace's import page

**See `data_exports/README.md` for detailed setup instructions for each service.** Without this sample data, these MCP servers will still function but may return empty results when evaluation tasks reference specific data.

Note: Some services are paid and require billing setup. 

**Note: When you add more API keys to `.env`, you need to restart the server in step 2.** On start, it'll automatically detect what API keys are available, and start those respective MCP servers.

### 8. Evaluate with the full HuggingFace dataset

Run completions using the HuggingFace dataset:

```bash
uv run python mcp_completion_script.py \
  --model "openai/gpt-4o" \
  --input_huggingface "bhertz/mcp_dummy" \  # TODO switch to dataset on ScaleAI
  --output "mcp_eval_4o_results.csv"
```

This saves the completion results to:
- `completion_results/mcp_eval_4o_results.csv` - Contains both ground truth and completion data

Then evaluate the results:

```bash
uv run mcp_evals_scores.py \
--input-file="completion_results/mcp_eval_4o_results.csv" \
--model-name="gpt4o"
```

**Note:** Tasks are filtered by default (see step 5). To disable, add `--no-filter`, but we recommend adding missing API keys to `.env` instead.

## What's Included

- **45+ MCP servers** including calculator, Wikipedia, filesystem, Git, weather, GitHub, and more
- **Agent completion service** for running multi-turn LLM conversations with tool use
- **Docker containerization** for consistent MCP server environments
- **HTTP APIs** for tool calling and listing available tools
- **Sample debug scripts** in `services/agent-environment/dev_scripts/debug_and_concurrency_tests/curl_scripts/` for directly testing individual MCP servers
