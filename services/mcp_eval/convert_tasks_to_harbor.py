#!/usr/bin/env python3
"""Convert MCP tool-use tasks (CSV or JSONL) into Harbor task bundles.

Each input row becomes one Harbor (https://github.com/harbor-framework/harbor)
task bundle under ``OUT/<task_id>/``:

    task.toml
    instruction.md
    environment/Dockerfile
    environment/enabled_tools.txt
    tests/agent_judge.py
    tests/test.sh
    solution/solve.sh

The bundle's environment is built ``FROM`` an MCP server image you provide with
``--image``. That image is expected to:

  * expose an MCP endpoint over streamable-http at ``http://localhost:<port>/mcp``
    (override the port with ``--mcp-port``), and
  * honor a newline-delimited tool allowlist file pointed to by the
    ``MCP_ENABLED_TOOLS_FILE`` environment variable, so the server only
    advertises the tools listed for each task.

The verifier (``tests/agent_judge.py``) is a self-contained LLM-as-judge: it
scores the agent's recorded trajectory against the task's rubrics and writes a
scalar reward to ``/logs/verifier/reward.json`` (the shape Harbor expects), plus
a human-readable ``rubric_breakdown.json``.

Input row fields:
    task_id        str   (required)
    prompt         str   (required)
    rubrics        list of {"id", "title", ...}  (required; JSON string in CSV)
    enabled_tools  list of tool names, or list of {"name": ...}  (optional;
                   JSON string in CSV) -- restricts the tools advertised for
                   this task; omit/empty to advertise all
    system_prompt  str   (optional)
    metadata       dict  (optional; emitted verbatim under [metadata];
                   JSON string in CSV)
    image          str   (optional; overrides --image for this row)

Usage:
    python convert_tasks_to_harbor.py --jsonl tasks.jsonl --image <mcp-image> --out ./out
    python convert_tasks_to_harbor.py --csv   tasks.csv   --image <mcp-image> --out ./out
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

csv.field_size_limit(sys.maxsize)

# ── Harbor bundle templates ──────────────────────────────────────────────────

# The environment image is the MCP server image plus this task's tool allowlist:
# the allowlist file is copied in and exposed via MCP_ENABLED_TOOLS_FILE so the
# server advertises only the tools this task needs. Enforcement is server-side,
# independent of the agent or harness.
DOCKERFILE_TEMPLATE = """FROM {image}
COPY enabled_tools.txt /enabled_tools.txt
ENV MCP_ENABLED_TOOLS_FILE=/enabled_tools.txt
"""

TEST_SH = """#!/bin/bash
# Forward LLM auth env vars from the host (default to empty if unset).
export ANTHROPIC_API_KEY="${ANTHROPIC_API_KEY:-}"
export ANTHROPIC_BASE_URL="${ANTHROPIC_BASE_URL:-}"
export LITELLM_API_KEY="${LITELLM_API_KEY:-}"
export LITELLM_BASE_URL="${LITELLM_BASE_URL:-}"
uv run /tests/agent_judge.py
"""

# Tool-use tasks have no canonical reference solution; this placeholder keeps
# Harbor's optional solution/ contract satisfied.
SOLVE_SH = """#!/bin/bash
# No reference solution for this tool-use task; the verifier judges the agent's
# trajectory directly.
echo "no solution"
"""

TASK_TOML_TEMPLATE = r"""version = "1.0"

[metadata]
category = "{category}"
tags = ["mcp"]
task_id = "{task_id}"
image = "{image}"
{extra_metadata}
[agent]
timeout = {agent_timeout}

[environment]
cpus = 4
memory_mb = 8192
storage_mb = 10240

[[environment.mcp_servers]]
name = "mcp-server"
transport = "streamable-http"
url = "http://localhost:{mcp_port}/mcp"
enabled_tools = {enabled_tools_toml}

# Gate the agent launch until the MCP server has finished starting and has
# registered its tools. Without this, the agent can connect before any tools
# are available and run with zero tools. The check performs the MCP startup
# handshake against :{mcp_port}/mcp and only passes once tools/list is non-empty:
#   1. initialize             -> response carries an Mcp-Session-Id header
#   2. notifications/initialized
#   3. tools/list             -> must contain at least one tool name
# Harbor runs this via `bash -c <command>` inside the container.
[environment.healthcheck]
command = '''set -e; URL=http://localhost:{mcp_port}/mcp; H1='Content-Type: application/json'; H2='Accept: application/json, text/event-stream'; INIT='{{"jsonrpc":"2.0","id":1,"method":"initialize","params":{{"protocolVersion":"2024-11-05","capabilities":{{}},"clientInfo":{{"name":"hc","version":"1"}}}}}}'; SID=$(curl -sD - -X POST $URL -H "$H1" -H "$H2" --data-raw "$INIT" 2>/dev/null | tr -d '\r' | awk 'tolower($1)=="mcp-session-id:"{{print $2}}'); [ -n "$SID" ] || exit 1; curl -sf -X POST $URL -H "Mcp-Session-Id: $SID" -H "$H1" -H "$H2" --data-raw '{{"jsonrpc":"2.0","method":"notifications/initialized"}}' -o /dev/null; curl -sf -X POST $URL -H "Mcp-Session-Id: $SID" -H "$H1" -H "$H2" --data-raw '{{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{{}}}}' | grep -q '"name"' '''
interval_sec = 5
timeout_sec = 15
start_period_sec = 30
retries = 36

[verifier]
timeout_sec = 900.0

[verifier.env]
ANTHROPIC_API_KEY = "${{ANTHROPIC_API_KEY:-}}"
ANTHROPIC_BASE_URL = "${{ANTHROPIC_BASE_URL:-}}"
LITELLM_API_KEY = "${{LITELLM_API_KEY:-}}"
LITELLM_BASE_URL = "${{LITELLM_BASE_URL:-}}"
JUDGE_MODEL = "{judge_model}"
"""


# Self-contained LLM-as-judge verifier emitted into each task bundle. Runs with
# `uv run` inside the container, so it declares its own dependency inline.
AGENT_JUDGE_TEMPLATE = '''# /// script
# dependencies = [
#   "claude-agent-sdk>=0.1.45",
# ]
# ///

"""Agent judge for rubrics verification."""

import asyncio
import json
import os
import sys
from pathlib import Path
from string import Template

_api_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
_base_url = os.getenv("ANTHROPIC_BASE_URL", "").strip()
if _api_key:
    os.environ["ANTHROPIC_AUTH_TOKEN"] = _api_key
if not _base_url:
    os.environ.pop("ANTHROPIC_BASE_URL", None)

CRITERIA = {criteria_json}

AGENT_PROMPT = {agent_prompt_repr}

SCORE_AGGREGATOR = "all_pass"

OUTPUT_SCHEMA = {{
  "type": "json_schema",
  "schema": {{
    "type": "object",
    "properties": {{
      "results": {{
        "type": "array",
        "items": {{
          "type": "object",
          "properties": {{
            "id": {{"type": "string"}},
            "score": {{"type": "number"}},
            "justification": {{"type": "string"}}
          }},
          "required": ["id", "score", "justification"]
        }}
      }}
    }},
    "required": ["results"]
  }}
}}


def find_agent_trajectory_filepath():
    agent_dir = Path("/logs/agent")
    if not agent_dir.exists():
        # Raise (not sys.exit) so __main__'s `except Exception` writes a
        # reward.json; SystemExit is a BaseException and would bypass it.
        raise RuntimeError("/logs/agent/ directory not found")
    txt_files = [f for f in agent_dir.iterdir() if f.is_file() and f.suffix == ".txt"]
    if not txt_files:
        raise RuntimeError("No trajectory .txt files found in /logs/agent/")
    return str(max(txt_files, key=lambda f: f.stat().st_size))


def extract_agent_response(trajectory_path):
    agent_response = ""
    for line in Path(trajectory_path).read_text().splitlines():
        try:
            event = json.loads(line)
            if event.get("type") == "result" and event.get("result"):
                agent_response = event["result"]
        except (json.JSONDecodeError, TypeError):
            pass
    return agent_response


EVALUATION_PROMPT_TEMPLATE = Template(
    "An agent was given the following prompt and generated the trajectory and response below. "
    "Analyze the agent's trajectory and response against the following criteria.\\n\\n"
    "## Agent Prompt\\n<agent_prompt>\\n$agent_prompt\\n</agent_prompt>\\n\\n"
    "## Agent Trajectory\\nThe agent's trajectory is available at $trajectory_path\\n\\n"
    "## Agent Response\\n<agent_response>\\n$agent_response\\n</agent_response>\\n\\n"
    "## Criteria\\n$criteria_json\\n\\n"
    "## Instructions\\nFor each criterion, determine whether the agent's behavior PASSES or FAILS.\\n\\n"
    "For each criterion, return a result with:\\n"
    "- \\"id\\": the criterion's id\\n"
    "- \\"score\\": 1.0 if the criterion is met, 0.0 if it is not\\n"
    "- \\"justification\\": a brief explanation of why you chose this score"
)


def aggregate_score(results):
    if SCORE_AGGREGATOR == "all_pass":
        # An incomplete judge result set (fewer rows than criteria) must fail:
        # missing criteria are treated as failures, not silently passed over.
        if not results or len(results) != len(CRITERIA):
            return 0.0
        return 1.0 if all(r.get("score") == 1.0 for r in results) else 0.0
    raise ValueError(f"Unknown aggregator: {{SCORE_AGGREGATOR}}")


def extract_result(messages):
    for msg in reversed(messages):
        if hasattr(msg, "structured_output") and msg.structured_output is not None:
            return msg.structured_output if isinstance(msg.structured_output, str) else json.dumps(msg.structured_output)
        if hasattr(msg, "result") and msg.result:
            return msg.result
    return ""


async def run_judge():
    from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient

    model = os.getenv("JUDGE_MODEL", "claude-sonnet-4-6")
    trajectory_path = find_agent_trajectory_filepath()
    print(f"Found trajectory: {{trajectory_path}} ({{Path(trajectory_path).stat().st_size}} bytes)")
    agent_response = extract_agent_response(trajectory_path)
    eval_prompt = EVALUATION_PROMPT_TEMPLATE.substitute(
        agent_prompt=AGENT_PROMPT,
        agent_response=agent_response,
        criteria_json=json.dumps(CRITERIA, indent=2),
        trajectory_path=trajectory_path,
    )

    print(f"Running judge agent: {{model}} (effort=low)")
    print(f"Criteria count: {{len(CRITERIA)}}")

    options = ClaudeAgentOptions(
        allowed_tools=["Bash", "Read", "Write", "Edit", "MultiEdit", "Glob", "Grep", "TodoWrite", "Task", "ExitPlanMode"],
        permission_mode="acceptEdits",
        model=model,
        max_turns=120,
        output_format=OUTPUT_SCHEMA,
        effort="low",
    )

    messages = []
    async with ClaudeSDKClient(options) as client:
        await client.query(prompt=eval_prompt)
        async for message in client.receive_response():
            messages.append(message)

    print(f"Judge agent finished. Total messages: {{len(messages)}}")

    result_text = extract_result(messages)
    if not result_text:
        raise RuntimeError("Judge agent produced no result")

    text = result_text.strip()
    if text.startswith("```"):
        lines = text.split("\\n")
        lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\\n".join(lines)

    parsed = json.loads(text)
    results = parsed.get("results", []) if isinstance(parsed, dict) else parsed

    if len(results) != len(CRITERIA):
        print(f"WARNING: Judge returned {{len(results)}} results but expected {{len(CRITERIA)}}", file=sys.stderr)

    agg_score = aggregate_score(results)

    criteria_by_id = {{c["id"]: c for c in CRITERIA}}
    verified_results = []
    for r in results:
        criterion = criteria_by_id.get(r.get("id"), {{}})
        verified_results.append({{
            **criterion,
            "score": r.get("score", 0.0),
            "result": r.get("score") == 1.0,
            "justification": r.get("justification", ""),
        }})

    Path("/logs/verifier").mkdir(parents=True, exist_ok=True)
    # Harbor parses reward.json into VerifierResult(rewards: dict[str, float | int]);
    # it must be a flat scalar dict keyed by Harbor's default "reward" key.
    Path("/logs/verifier/reward.txt").write_text(str(agg_score))
    Path("/logs/verifier/reward.json").write_text(json.dumps({{"reward": agg_score}}, indent=2))
    # Preserve the full rubric breakdown for analysis/debugging (not read by Harbor).
    Path("/logs/verifier/rubric_breakdown.json").write_text(
        json.dumps({{"score": agg_score, "results": verified_results}}, indent=2)
    )

    print(f"\\nResults:")
    for r in results:
        status = "PASS" if r.get("score") == 1.0 else "FAIL"
        print(f"  [{{status}}] {{r.get('id', '?')[:8]}}: {{r.get('justification', '')[:100]}}")
    print(f"\\nAggregate score: {{agg_score}}")


if __name__ == "__main__":
    try:
        asyncio.run(run_judge())
    except Exception as e:
        import traceback
        print(f"Judge failed: {{e}}", file=sys.stderr)
        traceback.print_exc()
        Path("/logs/verifier").mkdir(parents=True, exist_ok=True)
        Path("/logs/verifier/reward.txt").write_text("0.0")
        Path("/logs/verifier/reward.json").write_text(json.dumps({{"reward": 0.0}}, indent=2))
        Path("/logs/verifier/rubric_breakdown.json").write_text(json.dumps({{
            "score": 0.0,
            "error": str(e),
            "results": [],
        }}, indent=2))
'''


# ── Rendering ─────────────────────────────────────────────────────────────────

DEFAULT_AGENT_TIMEOUT = 1800
DEFAULT_MCP_PORT = 18765
DEFAULT_CATEGORY = "mcp-tool-use"
DEFAULT_JUDGE_MODEL = "claude-sonnet-4-6"


def _toml_escape(s: str) -> str:
    """Escape a string for use inside a TOML basic string (``"..."``).

    Handles backslash, double-quote, and the control characters TOML basic
    strings forbid as literals (newline, carriage return, tab, and other C0
    control chars).
    """
    out: list[str] = []
    for ch in s:
        if ch == "\\":
            out.append("\\\\")
        elif ch == '"':
            out.append('\\"')
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\r":
            out.append("\\r")
        elif ch == "\t":
            out.append("\\t")
        elif ord(ch) < 0x20:
            out.append(f"\\u{ord(ch):04x}")
        else:
            out.append(ch)
    return "".join(out)


def _toml_value(v) -> str:
    """Render a Python value as a TOML literal (str/int/float/bool/list/dict)."""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, list):
        return "[" + ", ".join(_toml_value(x) for x in v) + "]"
    if isinstance(v, dict):
        # Render a nested dict as a TOML inline table ({k = v, ...}) rather than
        # str()-ing it into a string that holds a Python dict repr.
        inner = ", ".join(f'"{_toml_escape(str(k))}" = {_toml_value(val)}' for k, val in v.items())
        return "{" + inner + "}"
    return f'"{_toml_escape(str(v))}"'


def _toml_str_array_multiline(items: list[str], indent: int = 2) -> str:
    """Render a list of strings as a multi-line TOML array (one item per line)."""
    if not items:
        return "[]"
    pad = " " * indent
    inner = ",\n".join(f"{pad}{_toml_value(x)}" for x in items)
    return f"[\n{inner},\n]"


# Keys the template already writes into [metadata]; a row's metadata dict must
# not re-emit them or task.toml would have duplicate keys (a TOML parse error).
_RESERVED_METADATA_KEYS = frozenset({"category", "tags", "task_id", "image"})


def _build_extra_metadata(meta) -> str:
    """Render an optional per-row metadata dict into [metadata] TOML lines.

    Keys are emitted verbatim; reserved keys already written by the template and
    empty values are skipped. Anything that isn't a dict is ignored.
    """
    if not isinstance(meta, dict):
        return ""
    lines = [
        f'"{_toml_escape(str(k))}" = {_toml_value(v)}'
        for k, v in meta.items()
        if k not in _RESERVED_METADATA_KEYS and v not in (None, "", [])
    ]
    return ("\n".join(lines) + "\n") if lines else ""


def _enabled_tool_names(enabled_tools) -> list[str]:
    """Extract a stable, de-duplicated list of tool names.

    Accepts a list of names, a list of {"name": ...} dicts, or a JSON string of
    either (as CSV stores it).
    """
    if isinstance(enabled_tools, str):
        try:
            enabled_tools = json.loads(enabled_tools)
        except json.JSONDecodeError:
            return []
    if not isinstance(enabled_tools, list):
        return []
    seen: set[str] = set()
    out: list[str] = []
    for t in enabled_tools:
        if isinstance(t, dict):
            name = t.get("name")
        elif isinstance(t, str):
            name = t
        else:
            name = None
        if name and name not in seen:
            seen.add(name)
            out.append(name)
    return out


def render_task(
    row: dict,
    out_root: Path,
    *,
    image: str,
    category: str = DEFAULT_CATEGORY,
    mcp_port: int = DEFAULT_MCP_PORT,
    judge_model: str = DEFAULT_JUDGE_MODEL,
    agent_timeout: int = DEFAULT_AGENT_TIMEOUT,
) -> tuple[str, str]:
    """Render one task bundle. Returns (task_id, image_used)."""
    task_id = row["task_id"]
    # task_id becomes a directory name under out_root, so reject anything that
    # could escape it (empty, absolute, path separators, or `..` traversal).
    if (not task_id or task_id in (".", "..") or "/" in task_id or "\\" in task_id
            or Path(task_id).is_absolute() or ".." in Path(task_id).parts):
        raise ValueError(f"unsafe or empty task_id: {task_id!r}")
    prompt = row["prompt"]
    system_prompt = row.get("system_prompt", "") or ""
    rubrics = row["rubrics"]
    if isinstance(rubrics, str):
        rubrics = json.loads(rubrics)
    image_used = row.get("image") or image
    # image_used is written verbatim into the Dockerfile `FROM` line; a newline
    # would let a per-row image inject extra build instructions, so reject it.
    if "\n" in image_used or "\r" in image_used:
        raise ValueError(f"image contains newline characters: {image_used!r}")

    task_dir = out_root / task_id
    (task_dir / "environment").mkdir(parents=True, exist_ok=True)
    (task_dir / "tests").mkdir(parents=True, exist_ok=True)
    (task_dir / "solution").mkdir(parents=True, exist_ok=True)

    # instruction.md
    if system_prompt:
        instruction = f"{system_prompt}\n\n---\n\n{prompt}\n"
    else:
        instruction = f"{prompt}\n"
    (task_dir / "instruction.md").write_text(instruction, encoding="utf-8")

    # task.toml — the per-task tool allowlist is mirrored here for visibility;
    # the actual enforcement is via environment/enabled_tools.txt + the
    # Dockerfile ENV below.
    tool_names = _enabled_tool_names(row.get("enabled_tools", []))
    (task_dir / "task.toml").write_text(
        TASK_TOML_TEMPLATE.format(
            category=_toml_escape(category),
            task_id=_toml_escape(task_id),
            image=_toml_escape(image_used),
            extra_metadata=_build_extra_metadata(row.get("metadata", {})),
            agent_timeout=agent_timeout,
            mcp_port=mcp_port,
            enabled_tools_toml=_toml_str_array_multiline(tool_names),
            judge_model=_toml_escape(judge_model),
        ),
        encoding="utf-8",
    )

    # environment/ — the allowlist file is COPYd into the image and read via
    # MCP_ENABLED_TOOLS_FILE so the server advertises only these tools.
    (task_dir / "environment" / "enabled_tools.txt").write_text(
        "\n".join(tool_names) + ("\n" if tool_names else ""),
        encoding="utf-8",
    )
    (task_dir / "environment" / "Dockerfile").write_text(
        DOCKERFILE_TEMPLATE.format(image=image_used), encoding="utf-8"
    )

    # tests/ + solution/ — the shell scripts have shebangs and may be executed
    # directly, so make them executable (0o755) rather than the default 0o644.
    test_sh = task_dir / "tests" / "test.sh"
    test_sh.write_text(TEST_SH, encoding="utf-8")
    test_sh.chmod(0o755)
    solve_sh = task_dir / "solution" / "solve.sh"
    solve_sh.write_text(SOLVE_SH, encoding="utf-8")
    solve_sh.chmod(0o755)
    agent_prompt_text = f"{system_prompt}\n\n{prompt}" if system_prompt else prompt
    # repr() (not json.dumps) so the embedded CRITERIA literal is valid Python:
    # JSON true/false/null in a rubric field would be undefined names otherwise.
    judge = AGENT_JUDGE_TEMPLATE.format(
        criteria_json=repr(rubrics),
        agent_prompt_repr=repr(agent_prompt_text),
    )
    (task_dir / "tests" / "agent_judge.py").write_text(judge, encoding="utf-8")

    return task_id, image_used


# ── Input adapters ───────────────────────────────────────────────────────────


def iter_jsonl(path: Path):
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            # Skip (don't abort on) a malformed line — matches iter_csv, and keeps
            # the error inside this generator rather than escaping main()'s per-row
            # try/except via the `for row in rows` iteration.
            try:
                yield json.loads(line)
            except json.JSONDecodeError as e:
                print(f"Warning: skipping malformed JSONL line: {e}", file=sys.stderr)


def iter_csv(path: Path):
    with path.open(encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            # CSV stores rubrics/metadata as JSON strings; parse so the rest of
            # the pipeline sees the same shapes as JSONL input.
            for field in ("rubrics", "metadata", "enabled_tools"):
                if isinstance(row.get(field), str) and row[field].strip():
                    try:
                        row[field] = json.loads(row[field])
                    except json.JSONDecodeError:
                        pass
            yield row


# ── CLI ──────────────────────────────────────────────────────────────────────


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--jsonl", type=Path, help="Input tasks as JSONL (one object per line)")
    g.add_argument("--csv", type=Path, help="Input tasks as CSV (rubrics/metadata as JSON strings)")
    p.add_argument("--out", type=Path, required=True, help="Output directory for the bundles")
    p.add_argument("--image", required=True,
                   help="MCP server image the bundle environment builds FROM "
                        "(per-row 'image' field overrides this)")
    p.add_argument("--mcp-port", type=int, default=DEFAULT_MCP_PORT,
                   help=f"Port the MCP server speaks streamable-http on (default {DEFAULT_MCP_PORT})")
    p.add_argument("--category", default=DEFAULT_CATEGORY,
                   help=f"task.toml [metadata].category (default {DEFAULT_CATEGORY!r})")
    p.add_argument("--judge-model", default=DEFAULT_JUDGE_MODEL,
                   help=f"Default JUDGE_MODEL for the verifier (default {DEFAULT_JUDGE_MODEL!r})")
    p.add_argument("--agent-timeout", type=int, default=DEFAULT_AGENT_TIMEOUT,
                   help=f"Agent timeout seconds (default {DEFAULT_AGENT_TIMEOUT})")
    p.add_argument("--limit", type=int, default=0, help="Only render the first N tasks (0 = all)")
    args = p.parse_args()

    args.out.mkdir(parents=True, exist_ok=True)
    rows = iter_jsonl(args.jsonl) if args.jsonl else iter_csv(args.csv)

    by_image: dict[str, int] = {}
    skipped: list[tuple[str, str]] = []
    n = 0
    for row in rows:
        if args.limit and n >= args.limit:
            break
        try:
            if not row.get("task_id") or not row.get("prompt") or not row.get("rubrics"):
                skipped.append((row.get("task_id") or "<missing>", "empty task_id, prompt, or rubrics"))
                continue
            _, image_used = render_task(
                row, args.out,
                image=args.image,
                category=args.category,
                mcp_port=args.mcp_port,
                judge_model=args.judge_model,
                agent_timeout=args.agent_timeout,
            )
            by_image[image_used] = by_image.get(image_used, 0) + 1
            n += 1
            if n % 200 == 0:
                print(f"  rendered {n} tasks...")
        except Exception as e:
            skipped.append((row.get("task_id", "<missing>"), f"{type(e).__name__}: {e}"))

    print(f"\nDone. Rendered {n} task bundles into {args.out}")
    if by_image:
        print("By image:")
        for img, c in sorted(by_image.items()):
            print(f"  {img}: {c}")
    if skipped:
        print(f"\nSkipped {len(skipped)} tasks:")
        for tid, reason in skipped[:10]:
            print(f"  {tid}: {reason}")
        if len(skipped) > 10:
            print(f"  ... and {len(skipped) - 10} more")
    return 0


if __name__ == "__main__":
    sys.exit(main())
