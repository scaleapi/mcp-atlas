#!/usr/bin/env python3
"""
Batch runner for the MCP-Atlas eval.

Iterates over the dataset, posts each task to the running agent-harness
(default: http://localhost:3001/v2/mcp_eval/run_agent), and writes results
to a CSV in the format expected by services/scoring/score_claims.py
(columns: task_id, raw_conversation_history, response). The
raw_conversation_history column is the full OpenAI-format message list and is
consumed as-is by services/diagnostics/single_model_diagnostic.py.

The default dataset source is HuggingFace (ScaleAI/MCP-Atlas, 500 public
tasks). A local CSV can be supplied via --input.

Usage:
    # Default: full 500-task HuggingFace run
    python run_eval.py --model openai/gpt-4o --output outputs.csv

    # Quick test with 5 tasks
    python run_eval.py --model openai/gpt-4o --output outputs.csv --num-tasks 5

    # Use a local input CSV instead of HuggingFace
    python run_eval.py --model openai/gpt-4o --output outputs.csv \\
        --input path/to/my_tasks.csv

The script resumes safely: rerunning against an existing output CSV will
skip task_ids that are already present.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import os
import sys
from typing import Any

import aiohttp

# Trajectories (raw_conversation_history) routinely exceed Python's default CSV
# field-size limit; raise it so resume — which reads the existing output CSV to
# skip already-done task_ids — doesn't crash on large trajectory fields.
csv.field_size_limit(sys.maxsize)


HARNESS_URL = os.getenv("HARNESS_URL", "http://localhost:3001")
SANDBOX_URL = os.getenv("MCP_SANDBOX_URL", "http://localhost:1984")
DEFAULT_DATASET = "ScaleAI/MCP-Atlas"
DEFAULT_SANDBOX_IMAGE = "ghcr.io/scaleapi/mcp-atlas:1.2.5"


def _tool_names(items: list[Any]) -> list[str]:
    """Normalize tool entries to name strings.

    ENABLED_TOOLS entries may be plain tool-name strings ("fetch_fetch") or
    tool-definition objects ({"name": ..., "requiredParams": ...}). The harness
    expects a list of names, so pull .name out of any dict entries.
    """
    names: list[str] = []
    for t in items:
        if isinstance(t, str):
            names.append(t)
        elif isinstance(t, dict) and t.get("name"):
            names.append(t["name"])
    return names


def parse_enabled_tools(value: Any) -> list[str]:
    """ENABLED_TOOLS may be a JSON-encoded list (of names or tool-definition
    objects) or a comma-separated string."""
    if isinstance(value, list):
        return _tool_names(value)
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
            if isinstance(parsed, list):
                return _tool_names(parsed)
        except json.JSONDecodeError:
            pass
        return [t.strip() for t in value.split(",") if t.strip()]
    return []


def load_tasks(input_path: str | None, num_tasks: int | None) -> list[dict[str, Any]]:
    """Load tasks from a CSV file or HuggingFace dataset."""
    if input_path:
        import pandas as pd
        df = pd.read_csv(input_path)
        rows: list[dict[str, Any]] = df.to_dict(orient="records")
        print(f"Loaded {len(rows)} tasks from {input_path}")
    else:
        from datasets import load_dataset
        print(f"Loading {DEFAULT_DATASET} from HuggingFace...")
        ds = load_dataset(DEFAULT_DATASET, split="train")
        rows = list(ds)
        print(f"Loaded {len(rows)} tasks from {DEFAULT_DATASET}")
    if num_tasks:
        rows = rows[:num_tasks]
        print(f"Limiting to first {num_tasks} tasks")
    return rows


def existing_task_ids(output_path: str) -> set[str]:
    """Read existing task_ids from output file (for resume)."""
    if not os.path.exists(output_path):
        return set()
    done: set[str] = set()
    with open(output_path, newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            tid = (row.get("task_id") or "").strip()
            if tid:
                done.add(tid)
    return done


async def run_one_task(
    session: aiohttp.ClientSession,
    task: dict[str, Any],
    args: argparse.Namespace,
    sem: asyncio.Semaphore,
) -> dict[str, str]:
    """Post one task to the harness and shape the response into a CSV row."""
    async with sem:
        task_id = str(task.get("TASK") or task.get("task_id") or "").strip()
        prompt = task.get("PROMPT") or ""
        enabled_tools = parse_enabled_tools(task.get("ENABLED_TOOLS", "[]"))
        image = task.get("IMAGE") or args.image

        body: dict[str, Any] = {
            "task_id": task_id,
            "model": args.model,
            "messages": [{"role": "user", "content": prompt}],
            "enabledTools": enabled_tools,
            "image": image,
            "tags": {"task_id": task_id},
        }
        if args.max_turns is not None:
            body["max_turns"] = args.max_turns
        if args.max_tool_calls is not None:
            body["max_tool_calls"] = args.max_tool_calls
        if args.tool_output_cap is not None:
            body["tool_output_cap"] = args.tool_output_cap
        if args.context_window_management:
            body["context_window_management"] = args.context_window_management
        if args.extra_llm_params:
            body["extra_llm_params"] = args.extra_llm_params
        if args.system_prompt:
            body["messages"] = [
                {"role": "system", "content": args.system_prompt},
                {"role": "user", "content": prompt},
            ]

        try:
            async with session.post(
                f"{HARNESS_URL}/v2/mcp_eval/run_agent",
                json=body,
                timeout=aiohttp.ClientTimeout(total=args.timeout),
            ) as resp:
                if resp.status != 200:
                    text = (await resp.text())[:300]
                    return {
                        "task_id": task_id,
                        "raw_conversation_history": "",
                        "response": f"ERROR: HTTP {resp.status}: {text}",
                    }
                data = await resp.json()
        except asyncio.TimeoutError:
            return {
                "task_id": task_id,
                "raw_conversation_history": "",
                "response": f"ERROR: timeout after {args.timeout}s",
            }
        except Exception as exc:
            return {
                "task_id": task_id,
                "raw_conversation_history": "",
                "response": f"ERROR: {exc.__class__.__name__}: {exc}",
            }

        # data is a list of {type, data} items. Pull out the trajectory and
        # final assistant message.
        trajectory_msgs = [item["data"] for item in data if item.get("type") == "message"]
        final = ""
        for msg in reversed(trajectory_msgs):
            if msg.get("role") == "assistant" and msg.get("content"):
                final = msg["content"]
                break

        return {
            "task_id": task_id,
            "raw_conversation_history": json.dumps(trajectory_msgs),
            "response": final,
        }


async def check_sandbox_health() -> None:
    """Pre-flight check: confirm the MCP sandbox is up and report server status.

    Aborts if the sandbox is unreachable; warns (but continues) if some servers
    are offline. Bypass with --skip-health-check.
    """
    url = SANDBOX_URL.rstrip("/") + "/enabled-servers"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                data = await resp.json()
    except Exception as exc:
        print(
            f"ERROR: MCP sandbox not reachable at {SANDBOX_URL} "
            f"({exc.__class__.__name__}: {exc}). Start it with `make run-docker` "
            f"(or set MCP_SANDBOX_URL), or pass --skip-health-check to bypass.",
            file=sys.stderr,
        )
        sys.exit(1)
    total, online, offline = data.get("total"), data.get("online"), data.get("offline")
    print(f"Health check: {online}/{total} MCP servers online ({offline} offline).")
    if offline:
        print("  WARNING: some servers are offline — tasks needing them may fail. Continuing.")


def write_run_config(args: argparse.Namespace) -> None:
    """Write run_config.json next to the output CSV so the scoring step can
    embed which knobs were set into its coverage_stats output."""
    extra = args.extra_llm_params if isinstance(args.extra_llm_params, dict) else {}
    run_config = {
        "model": args.model,
        "strategy": None,
        "max_turns": args.max_turns,
        "max_tool_calls": args.max_tool_calls,
        "tool_output_cap": args.tool_output_cap,
        "context_window_management": args.context_window_management,
        "reasoning_effort": extra.get("reasoning_effort"),
        "extra_llm_params": args.extra_llm_params,
        "concurrency": args.concurrency,
        "num_tasks": args.num_tasks,
        "image": args.image,
    }
    out_dir = os.path.dirname(os.path.abspath(args.output))
    path = os.path.join(out_dir, "run_config.json")
    with open(path, "w") as f:
        json.dump(run_config, f, indent=2)
    print(f"Wrote run config to {path}")


async def run_all(args: argparse.Namespace) -> None:
    tasks = load_tasks(args.input, args.num_tasks)

    if not args.skip_health_check:
        await check_sandbox_health()

    write_run_config(args)

    done = existing_task_ids(args.output)
    if done:
        print(f"Resuming — {len(done)} task_ids already in {args.output}")

    pending = [
        t for t in tasks
        if str(t.get("TASK") or t.get("task_id") or "").strip() not in done
    ]
    print(
        f"Posting {len(pending)} task(s) to {HARNESS_URL} "
        f"(model={args.model}, concurrency={args.concurrency})"
    )
    if not pending:
        print("Nothing to do.")
        return

    sem = asyncio.Semaphore(args.concurrency)
    fieldnames = ["task_id", "raw_conversation_history", "response"]
    write_header = not os.path.exists(args.output)
    out_lock = asyncio.Lock()

    async with aiohttp.ClientSession() as session:
        with open(args.output, "a", newline="") as fout:
            writer = csv.DictWriter(fout, fieldnames=fieldnames)
            if write_header:
                writer.writeheader()
                fout.flush()

            async def task_and_write(task: dict[str, Any]) -> dict[str, str]:
                result = await run_one_task(session, task, args, sem)
                async with out_lock:
                    writer.writerow(result)
                    fout.flush()
                return result

            completed = 0
            failed = 0
            for fut in asyncio.as_completed([task_and_write(t) for t in pending]):
                result = await fut
                completed += 1
                tag = "OK" if not result["response"].startswith("ERROR:") else "FAIL"
                if tag == "FAIL":
                    failed += 1
                print(
                    f"[{completed}/{len(pending)}] {tag} {result['task_id']}",
                    flush=True,
                )

    print(
        f"\nDone. Wrote {args.output}. "
        f"Completed={completed}, failed={failed}, succeeded={completed - failed}."
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run the agent harness over the MCP-Atlas dataset.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--model", required=True,
        help="LLM model name (e.g. openai/gpt-4o)",
    )
    parser.add_argument(
        "--output", required=True,
        help="Output CSV path (will be resumed if it already exists)",
    )
    parser.add_argument(
        "--input", default=None,
        help="Local CSV instead of HuggingFace dataset (must have TASK, PROMPT, ENABLED_TOOLS columns)",
    )
    parser.add_argument(
        "--num-tasks", type=int, default=None,
        help="Limit to first N tasks (useful for testing)",
    )
    parser.add_argument(
        "--concurrency", type=int, default=5,
        help="Max parallel tasks (default: 5)",
    )
    parser.add_argument(
        "--timeout", type=int, default=1800,
        help="Per-task timeout in seconds (default: 1800 = 30 min)",
    )
    parser.add_argument(
        "--image", default=DEFAULT_SANDBOX_IMAGE,
        help=f"Docker image identifier for the sandbox (default: {DEFAULT_SANDBOX_IMAGE})",
    )
    parser.add_argument(
        "--max-turns", type=int, default=None,
        help="Override the harness's default max_turns",
    )
    parser.add_argument(
        "--max-tool-calls", type=int, default=None,
        help="Override the harness's default max_tool_calls",
    )
    parser.add_argument(
        "--tool-output-cap", type=int, default=None,
        help="Truncate each tool result to at most N characters before feeding "
             "it back to the model (default: uncapped)",
    )
    parser.add_argument(
        "--context-window-management", choices=["compact"], default=None,
        help="Context-window strategy when the conversation grows large: "
             "'compact' summarizes older turns (default: off)",
    )
    parser.add_argument(
        "--system-prompt", default=None,
        help="Optional system prompt to prepend to every task",
    )
    parser.add_argument(
        "--extra-llm-params", default=None,
        help='JSON object of extra params forwarded verbatim to the model, e.g. '
             '\'{"reasoning_effort": "high"}\' (set the reasoning/thinking level for '
             'your provider) or \'{"temperature": 0.2}\'.',
    )
    parser.add_argument(
        "--skip-health-check", action="store_true",
        help="Skip the pre-flight MCP-sandbox health check before the run.",
    )
    args = parser.parse_args()

    if args.extra_llm_params:
        try:
            args.extra_llm_params = json.loads(args.extra_llm_params)
        except json.JSONDecodeError as e:
            parser.error(f"--extra-llm-params must be valid JSON: {e}")

    try:
        asyncio.run(run_all(args))
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        sys.exit(130)


if __name__ == "__main__":
    main()
