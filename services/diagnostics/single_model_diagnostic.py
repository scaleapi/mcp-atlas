# single_model_diagnostic.py
#
# Description:
# Single-model diagnosis pipeline with enriched trajectory and a failure
# taxonomy.
#   - Uses enriched agent trajectory (turn structure, tool status, output summaries)
#   - 11 failure modes in 2 categories: tool call issues (4) + cognitive issues (7)
#   - Single-turn LLM diagnosis per failed task
#   - Programmatic aggregation with public-split examples
#   - LLM-generated model-level narrative
#
# Failure taxonomy defined in mcp_failure_taxonomy.py (single source of truth).
#
# Example Usage:
# python single_model_diagnostic.py \
#   --scored-file="scored_outputs.csv" \
#   --verbose

from dotenv import load_dotenv
load_dotenv()

import pandas as pd
import numpy as np
import asyncio
import os
import json
import ast
import logging
import argparse
import random
from typing import List, Dict, Any, Optional, Union
from dataclasses import dataclass
from datetime import datetime
from collections import defaultdict

# Third-party libraries
import aiohttp
from tenacity import retry, wait_random_exponential, stop_after_attempt
from tqdm.asyncio import tqdm as async_tqdm
from tqdm import tqdm
import nest_asyncio

# Apply nest_asyncio to allow nested event loops
nest_asyncio.apply()


# =========================================================================
# 1. CONFIGURATION AND SETUP
# =========================================================================

@dataclass
class DiagnosisConfig:
    """Configuration for the diagnosis process."""
    model_name: str = "gemini/gemini-3.1-pro-preview"
    max_retries: int = 6
    request_delay: float = 0.2
    semaphore_limit: int = 15
    api_key: Optional[str] = None
    base_url: Optional[str] = None
    verbose: bool = True


def setup_logging(verbose: bool = True):
    """Set up the logging configuration."""
    level = logging.INFO if verbose else logging.WARNING
    logging.basicConfig(
        level=level,
        format='%(asctime)s - %(levelname)s - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    # Silence noisy loggers
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("google.genai").setLevel(logging.WARNING)
    logging.getLogger("google_genai").setLevel(logging.WARNING)
    return logging.getLogger(__name__)


def extract_model_name(scored_file: str) -> str:
    """Extract model name from scored_{model}.csv filename."""
    basename = os.path.basename(scored_file)
    stem = os.path.splitext(basename)[0]
    if stem.startswith("scored_"):
        return stem[len("scored_"):]
    return stem


# =========================================================================
# 2. ERROR ANALYSIS (identical to v1)
# =========================================================================

def analyze_error_distribution(df: pd.DataFrame, logger: logging.Logger) -> dict:
    """Analyze error distribution from the 'errors' column."""
    total_tasks = len(df)

    response_col = next(
        (col for col in ["script_model_response", "model_response", "response"]
         if col in df.columns),
        None
    )
    empty_responses = 0
    error_responses = 0
    if response_col:
        empty_mask = df[response_col].isna() | (df[response_col].astype(str).str.strip() == '')
        error_mask = df[response_col].astype(str).str.startswith('ERROR:')
        empty_responses = int(empty_mask.sum())
        error_responses = int(error_mask.sum())

    error_distribution = defaultdict(int)
    tasks_with_errors = 0
    error_details = []

    if 'errors' not in df.columns:
        logger.info("No 'errors' column found in scored CSV")
        return {
            'total_tasks': total_tasks,
            'empty_responses': empty_responses,
            'error_responses': error_responses,
            'tasks_with_errors': 0,
            'error_distribution': {},
            'error_details': []
        }

    for idx, row in df.iterrows():
        task_id = row.get('TASK', f'task_{idx}')
        errors = row.get('errors', '[]')

        if pd.isna(errors) or str(errors).strip() in ('[]', '', 'nan'):
            continue

        try:
            error_list = json.loads(str(errors)) if isinstance(errors, str) else errors
            if not isinstance(error_list, list):
                continue

            if error_list:
                tasks_with_errors += 1

                for err in error_list:
                    if isinstance(err, dict):
                        msg = err.get('message', err.get('reason', 'Unknown error'))
                    else:
                        msg = str(err)

                    if 'ENOTFOUND' in msg or 'getaddrinfo' in msg:
                        error_type = 'DNS/Network Error'
                    elif 'Timeout' in msg or 'timeout' in msg:
                        error_type = 'Timeout Error'
                    elif '410' in msg or 'Gone' in msg:
                        error_type = 'External API Gone (410)'
                    elif '404' in msg or 'Not Found' in msg:
                        error_type = 'Resource Not Found (404)'
                    elif 'MCP error' in msg:
                        error_type = 'MCP Tool Error'
                    elif 'API error' in msg:
                        error_type = 'External API Error'
                    elif 'Failed to call tool' in msg:
                        error_type = 'Tool Execution Failed'
                    elif 'max_tool_calls_reached' in msg or 'max_tool_calls_reached' in str(err):
                        error_type = 'Max Tool Calls Reached'
                    elif 'max_turns_reached' in msg or 'max_turns_reached' in str(err):
                        error_type = 'Max Turns Reached'
                    else:
                        error_type = msg[:60]

                    error_distribution[error_type] += 1
                    error_details.append({
                        'task_id': task_id,
                        'error_type': error_type,
                        'full_message': msg
                    })

        except (json.JSONDecodeError, ValueError, TypeError):
            pass

    return {
        'total_tasks': total_tasks,
        'empty_responses': empty_responses,
        'error_responses': error_responses,
        'tasks_with_errors': tasks_with_errors,
        'error_distribution': dict(error_distribution),
        'error_details': error_details
    }


def print_error_report(stats: dict, logger: logging.Logger) -> None:
    """Print formatted error analysis report."""
    total = stats['total_tasks']

    logger.info("=" * 70)
    logger.info("ERROR ANALYSIS")
    logger.info("=" * 70)
    logger.info(f"  Total Tasks:          {total}")
    logger.info(f"  Empty Responses:      {stats['empty_responses']}")
    logger.info(f"  ERROR Responses:      {stats['error_responses']}")
    logger.info(f"  Tasks with Errors:    {stats['tasks_with_errors']}")

    if stats['error_distribution']:
        logger.info("-" * 70)
        logger.info("  Error Distribution:")
        sorted_errors = sorted(stats['error_distribution'].items(), key=lambda x: x[1], reverse=True)
        for error_type, count in sorted_errors:
            pct = (count / stats['tasks_with_errors'] * 100) if stats['tasks_with_errors'] > 0 else 0
            logger.info(f"    {error_type[:60]:<60} {count:>4} ({pct:>5.1f}%)")

    if stats['error_details']:
        task_error_counts = defaultdict(int)
        for detail in stats['error_details']:
            task_error_counts[detail['task_id']] += 1
        top_tasks = sorted(task_error_counts.items(), key=lambda x: x[1], reverse=True)[:10]
        logger.info("-" * 70)
        logger.info("  Top 10 Tasks with Most Errors:")
        for task_id, error_count in top_tasks:
            logger.info(f"    {task_id:<40} {error_count:>4} errors")

    logger.info("=" * 70)


# =========================================================================
# 3. CLAIM EXTRACTION (identical to v1)
# =========================================================================

def extract_claims(claim_blob) -> List[str]:
    """Extracts individual claims from various input formats."""
    if claim_blob is None:
        return []

    if isinstance(claim_blob, list):
        return [str(claim).strip() for claim in claim_blob if str(claim).strip() and len(str(claim).strip()) > 3]

    if not isinstance(claim_blob, str):
        claim_blob = str(claim_blob)

    claim_blob = claim_blob.strip()
    if not claim_blob:
        return []

    if claim_blob.startswith('[') and claim_blob.endswith(']'):
        try:
            parsed_list = json.loads(claim_blob)
            if isinstance(parsed_list, list):
                return [str(claim).strip() for claim in parsed_list
                       if str(claim).strip() and len(str(claim).strip()) > 3]
        except (json.JSONDecodeError, ValueError):
            try:
                parsed_list = ast.literal_eval(claim_blob)
                if isinstance(parsed_list, list):
                    return [str(claim).strip() for claim in parsed_list
                           if str(claim).strip() and len(str(claim).strip()) > 3]
            except (ValueError, SyntaxError):
                pass

    separators = ["\n\u2022", "\n-", "\n*", "\n1.", "\n2.", ";", "||"]
    for sep in separators:
        if sep in claim_blob:
            parts = claim_blob.split(sep)
            claims = [p.strip(" -*\u2022\t\n.") for p in parts
                     if p.strip(" -*\u2022\t\n.") and len(p.strip()) > 3]
            if claims:
                return claims

    lines = claim_blob.strip().split('\n')
    return [line.strip(" -*\u2022\t\n.") for line in lines
            if line.strip(" -*\u2022\t\n.") and len(line.strip()) > 3]


# =========================================================================
# 4. DIAGNOSIS FRAMEWORK (v2: taxonomy from mcp_failure_taxonomy.py)
# =========================================================================

from mcp_failure_taxonomy import (
    ALL_MODES, MODE_TO_CATEGORY, FAILURE_TAXONOMY,
    get_taxonomy_prompt_text, get_diagnosis_schema,
)
from extract_enriched_trajectory import parse_conversation, build_enriched_trajectory


def format_enriched_trajectory_for_judge(turns: list, max_chars: int = 60000) -> str:
    """Render the enriched trajectory as a readable text block for the LLM judge.

    Each turn is shown with: turn number, assistant reasoning (if any), per-tool-call
    name + parameters + status + error_message + output_summary, parallel flag, and
    the final answer when no tool calls were made.
    """
    if not turns:
        return "(no turns recorded)"
    lines = []
    for t in turns:
        if not isinstance(t, dict):
            continue
        turn_num = t.get("turn", "?")
        reasoning = (t.get("assistant_reasoning") or "").strip()
        tool_calls = t.get("tool_calls") or []
        final_answer = (t.get("final_answer") or "").strip()
        parallel = " (parallel)" if t.get("parallel") else ""

        lines.append(f"--- Turn {turn_num}{parallel} ---")
        if reasoning and reasoning != final_answer:
            r = reasoning if len(reasoning) <= 1200 else reasoning[:1200] + " …[reasoning truncated]"
            lines.append(f"  Assistant reasoning: {r}")
        if not tool_calls and final_answer:
            fa = final_answer if len(final_answer) <= 4000 else final_answer[:4000] + " …[final answer truncated]"
            lines.append(f"  Final answer: {fa}")
        for tc in tool_calls:
            name = tc.get("name", "?")
            params = tc.get("parameters", {})
            try:
                params_str = json.dumps(params, separators=(", ", ": "), ensure_ascii=False)
            except Exception:
                params_str = str(params)
            if len(params_str) > 800:
                params_str = params_str[:800] + " …[params truncated]"
            status = tc.get("status", "?")
            err = tc.get("error_message")
            out = tc.get("output_summary")
            lines.append(f"  Tool call: {name}({params_str}) → status={status}")
            if err:
                err_s = str(err)
                if len(err_s) > 600:
                    err_s = err_s[:600] + " …[error truncated]"
                lines.append(f"    Error: {err_s}")
            elif out:
                out_s = str(out)
                if len(out_s) > 600:
                    out_s = out_s[:600] + " …[output truncated]"
                lines.append(f"    Output: {out_s}")
    rendered = "\n".join(lines)
    if len(rendered) > max_chars:
        rendered = rendered[:max_chars] + "\n\n… [trajectory truncated]"
    return rendered


class AsyncLiteLLMClient:
    """Manages async LiteLLM proxy requests with rate limiting."""

    def __init__(self, config: DiagnosisConfig):
        self.config = config
        self.semaphore = asyncio.Semaphore(config.semaphore_limit)
        self.logger = logging.getLogger(__name__)
        self.request_count = 0
        self.error_count = 0
        self.total_prompt_tokens = 0
        self.total_completion_tokens = 0

        self.base_url = (
            config.base_url
            or os.getenv("EVAL_LLM_BASE_URL")
            or os.getenv("LLM_BASE_URL", "")
        ).rstrip("/")
        raw_keys = (
            config.api_key
            or os.getenv("EVAL_LLM_API_KEY")
            or os.getenv("LLM_API_KEY", "")
        )
        self.api_keys = [k.strip() for k in raw_keys.split(",") if k.strip()]
        if not self.api_keys:
            raise ValueError("API key not found. Set EVAL_LLM_API_KEY (or LLM_API_KEY) env var, or pass --api-key.")
        if not self.base_url:
            raise ValueError("Base URL not found. Set EVAL_LLM_BASE_URL (or LLM_BASE_URL) env var, or pass --base-url.")

    @retry(
        wait=wait_random_exponential(min=60, max=120),
        stop=stop_after_attempt(8),
        reraise=True,
        before_sleep=lambda retry_state: logging.getLogger(__name__).debug(f"Retry {retry_state.attempt_number}/8 after {retry_state.outcome.exception().__class__.__name__}: {str(retry_state.outcome.exception())[:150]}... waiting {retry_state.next_action.sleep:.1f}s")
    )
    async def generate_content(self, messages: List[Dict[str, str]], response_schema: Optional[Dict] = None, temperature: float = 0.0, timeout: int = 120) -> Union[str, Dict]:
        """Generate content via LiteLLM proxy. Returns raw string if no schema, parsed dict if schema provided."""
        async with self.semaphore:
            try:
                self.request_count += 1

                url = f"{self.base_url}/v1/chat/completions"
                api_key = random.choice(self.api_keys)
                headers = {
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                }
                payload = {
                    "model": self.config.model_name,
                    "messages": messages,
                    "temperature": temperature,
                }

                if response_schema:
                    payload["response_format"] = {
                        "type": "json_schema",
                        "json_schema": {
                            "name": "failure_diagnosis",
                            "schema": response_schema,
                        },
                    }

                async with aiohttp.ClientSession() as session:
                    async with session.post(
                        url, headers=headers, json=payload, timeout=aiohttp.ClientTimeout(total=timeout)
                    ) as resp:
                        if resp.status != 200:
                            body = await resp.text()
                            raise RuntimeError(f"LiteLLM API returned {resp.status}: {body}")
                        data = await resp.json()

                await asyncio.sleep(self.config.request_delay)

                usage = data.get("usage", {})
                self.total_prompt_tokens += usage.get("prompt_tokens", 0)
                self.total_completion_tokens += usage.get("completion_tokens", 0)

                content = data["choices"][0]["message"]["content"]

                if response_schema:
                    return json.loads(content)
                return content

            except Exception as e:
                self.error_count += 1
                self.logger.debug(f"LiteLLM API error (will retry): {e}")
                raise

    def get_stats(self) -> Dict[str, Any]:
        """Get request statistics."""
        return {
            "total_requests": self.request_count,
            "errors": self.error_count,
            "success_rate": (self.request_count - self.error_count) / max(self.request_count, 1),
            "total_prompt_tokens": self.total_prompt_tokens,
            "total_completion_tokens": self.total_completion_tokens,
            "total_tokens": self.total_prompt_tokens + self.total_completion_tokens,
        }


class DiagnosticAnalyzer:
    """Single-turn diagnosis using enriched trajectory and v2 failure taxonomy."""

    def __init__(self, client: AsyncLiteLLMClient, temperature: float = 0.0):
        self.client = client
        self.temperature = temperature
        self.logger = logging.getLogger(__name__)

    def _create_diagnosis_prompt(self, task_id: str, prompt: str, agent_trace: str, expected_trajectory: str, gtfa_claims: str, coverage_score: float, coverage_details: Dict, final_response: str = "", errors_str: str = "") -> str:
        """Single-turn prompt with v2 taxonomy."""
        traj_text = expected_trajectory[:2000000]
        traj_suffix = " [truncated]" if len(expected_trajectory) > 2000000 else ""
        taxonomy_text = get_taxonomy_prompt_text()

        # Build per-claim missed-claims block from the scorer's reasoning.
        # The scorer already explained why each claim passed/failed — pass that
        # signal to the judge so it can ground its diagnosis in scorer evidence.
        per_claim = []
        if isinstance(coverage_details, dict):
            per_claim = coverage_details.get("per_claim", []) or []
        missed_blocks = []
        for c in per_claim:
            score = c.get("score", 0)
            if score >= 0.9:
                continue
            claim = str(c.get("claim", "")).strip()
            reason = str(c.get("reason", "")).strip()
            band = "FAILED" if score < 0.5 else "PARTIAL"
            missed_blocks.append(
                f"- [{band} score={score:.2f}] {claim}\n  Scorer reason: {reason}"
            )
        missed_text = "\n".join(missed_blocks[:25]) if missed_blocks else "(no missed claims found in coverage details)"

        final_response_text = (final_response or "").strip()
        if not final_response_text:
            final_response_text = "(no final response captured)"
        elif len(final_response_text) > 8000:
            final_response_text = final_response_text[:8000] + " … [truncated]"

        # Context flag for tasks that hit tool/turn limits
        limit_context = ""
        if 'max_tool_calls_reached' in errors_str:
            limit_context = "\nIMPORTANT CONTEXT: This task was TERMINATED because the model reached the tool call limit (100 calls). The model did not choose to stop — it was cut off. Do NOT classify this as early_termination. Diagnose what went wrong in the work the model DID complete — why was it burning through 100+ tool calls without resolving the task?\n"
        elif 'max_turns_reached' in errors_str:
            limit_context = "\nIMPORTANT CONTEXT: This task was TERMINATED because the model reached the turn limit. The model did not choose to stop — it was cut off. Do NOT classify this as early_termination. Diagnose what went wrong in the work the model DID complete.\n"

        return f"""You are diagnosing why a model failed on an MCP (Model Context Protocol) tool-use evaluation task.

The model was connected to live MCP tool servers in sandboxed Docker environments and had to use tools to answer a multi-step question. Its final text response was scored against ground-truth claims.

TASK CONTEXT:
Task ID: {task_id}
Original Prompt: {prompt}
Coverage Score Achieved: {coverage_score:.2%}
{limit_context}
=== STEP 1. ANCHOR ON WHAT SHOULD HAVE HAPPENED ===

EXPECTED BEHAVIOR (Ground Truth Trajectory):
{traj_text}{traj_suffix}

EXPECTED CLAIMS TO ADDRESS:
{gtfa_claims}

=== STEP 2. EXAMINE WHAT THE MODEL ACTUALLY DID ===

MODEL'S ACTUAL BEHAVIOR (Enriched Agent Trajectory):
Each turn shows: assistant reasoning, tool calls with parameters/status/errors/output summaries, parallel call detection, and final answer.
Examine the trajectory turn-by-turn and assess PROGRESSION:
- Is the agent making forward progress, or looping on the same call?
- Does it retry identically after a tool error, or adapt parameters?
- Does it switch tools unproductively when a single correct tool was available?
- Does it stop while required data is still missing?
{agent_trace}

MODEL'S FINAL TEXT RESPONSE (this is what was scored against the claims):
{final_response_text}

=== STEP 3. CONSIDER WHICH CLAIMS WERE MISSED AND WHY ===

MISSED OR PARTIAL CLAIMS (with scorer reasoning):
The scorer below has already evaluated each claim against the model's final text response. Use the scorer's reason for each missed/partial claim as direct evidence when picking the failure mode.

{missed_text}

=== STEP 4. MAP TO A FAILURE MODE ===

FAILURE MODE DEFINITIONS:

{taxonomy_text}

JUDGE REASONING PROCESS (do this internally before selecting a mode):
(a) Locate the specific turn or sentence in the final response where the model went off-track.
(b) State what should have happened instead (using the ground-truth trajectory and claims as reference).
(c) Decide whether the failure is in tool interaction (Tool Call family) or reasoning/synthesis (Cognitive family).
(d) Pick the most specific mode from that family.

JUDGE RULES:
1. Pick the most specific mode that fits. Common tricky boundaries:
   - `response_misparsing` vs `faulty_synthesis`: misparsing = read the wrong field/row from a tool output; faulty_synthesis = read everything right but combined/aggregated wrong.
   - `logical_error` vs `faulty_synthesis`: logical_error = the multi-step reasoning chain itself was flawed (wrong conditional, wrong filter); faulty_synthesis = combined correct outputs incorrectly without a clear logic error.
   - `hallucinated_fact` vs `no_tool_use`: hallucinated_fact = stated a fact not present in any tool output; no_tool_use = bypassed tools entirely for a fact that required a tool.
   - `malformed_call` vs `err_recovery`: malformed_call = first incorrect parameter usage; err_recovery = same error reproduced repeatedly without adapting.
2. Identify the PRIMARY failure: the single mode that best explains the coverage gap.
3. Identify ALL failures in the trajectory (primary + contributing). Secondary failures use the same 11-mode vocabulary.
4. For each failure, mark is_root_cause=true only if it caused other failures downstream. If there is only one failure, mark it root cause.
5. `faulty_synthesis` should only be used when the error is NOT better explained by `logical_error`, `response_misparsing`, or `hallucinated_fact`.
6. Confidence calibration: 0.9-1.0 when the trajectory and scorer evidence both clearly point to one mode; 0.6-0.8 when the mode is the best fit but a secondary mode is close; below 0.6 when the trajectory is ambiguous.
7. Write a 1-2 sentence summary stating what the model did wrong and why a claim was missed. Cite the specific turn or final-response sentence.

IMPORTANT: This task scored below 1.0, meaning at least one claim was not satisfied. You MUST select a failure mode from the 11 listed above."""

    async def diagnose_failure(self, task_id: str, prompt: str, expected_trajectory: str, raw_conversation: str, gtfa_claims: str, coverage_score: float, coverage_details: Dict, enriched_trajectory: str = "", errors_str: str = "", final_response: str = "") -> Dict:
        """Single-turn diagnosis with v2 taxonomy."""
        has_enriched = enriched_trajectory and len(enriched_trajectory.strip()) > 10 and enriched_trajectory.strip() != '[]'
        agent_trace = enriched_trajectory if has_enriched else raw_conversation[:2000000]

        diagnosis_prompt = self._create_diagnosis_prompt(
            task_id=task_id,
            prompt=prompt,
            agent_trace=agent_trace,
            expected_trajectory=expected_trajectory,
            gtfa_claims=gtfa_claims,
            coverage_score=coverage_score,
            coverage_details=coverage_details,
            final_response=final_response,
            errors_str=errors_str,
        )

        try:
            diagnosis = await self.client.generate_content(
                messages=[{"role": "user", "content": diagnosis_prompt}],
                response_schema=get_diagnosis_schema(),
                temperature=self.temperature,
                timeout=180,
            )
            return diagnosis
        except Exception as e:
            self.logger.error(f"Diagnosis failed for task {task_id}: {e}")
            return {
                "primary_failure": {"mode": "analysis_error", "category": "tool_call", "explanation": f"Diagnosis LLM call failed: {str(e)}"},
                "all_failures": [],
                "confidence": 0.1,
                "summary": f"Diagnosis failed: {str(e)}",
            }


# =========================================================================
# 5. MAIN DIAGNOSIS PIPELINE
# =========================================================================

def _get_error_explanation(errors_val) -> str:
    """Derive a human-readable explanation from the errors column value."""
    if pd.isna(errors_val) or str(errors_val).strip() in ('[]', '', 'nan'):
        return "Task timed out — no response received after exhausting all retries (silent timeout)."
    err_str = str(errors_val)
    if 'ContextWindowExceeded' in err_str or 'context length' in err_str:
        return "Task failed due to context window exceeded — input tokens exceeded model limit."
    if 'max_tool_calls_reached' in err_str:
        return "Task stopped — reached the maximum tool call limit."
    if 'max_turns_reached' in err_str:
        return "Task stopped — reached the maximum turn limit."
    if 'fetch failed' in err_str:
        return "Task failed due to sandbox/network error — MCP tool fetch failed mid-trajectory."
    return f"Task failed due to infrastructure error: {err_str[:200]}"


async def run_diagnosis_pipeline(df: pd.DataFrame, config: DiagnosisConfig, failure_threshold: float = 1.0, temperature: float = 0.0) -> pd.DataFrame:
    """Run 2-turn diagnosis on all rows."""
    logger = logging.getLogger(__name__)

    perfect_mask = df['coverage_score'] >= failure_threshold
    null_mask = df['coverage_score'].isna()
    needs_llm_mask = ~perfect_mask & ~null_mask

    logger.info(f"Diagnosis breakdown:")
    logger.info(f"  Perfect score (>= {failure_threshold}): {perfect_mask.sum()}")
    logger.info(f"  Null/empty trajectory:                  {null_mask.sum()}")
    logger.info(f"  Needs LLM diagnosis:                    {needs_llm_mask.sum()}")

    diagnosis_columns = [
        'diagnosis_primary_mode',
        'diagnosis_primary_category',
        'diagnosis_primary_explanation',
        'diagnosis_all_failures',
        'diagnosis_confidence',
        'diagnosis_summary',
    ]
    for col in diagnosis_columns:
        df[col] = pd.NA

    # Bucket 1: Perfect scores — no diagnosis needed
    for idx in df[perfect_mask].index:
        df.loc[idx, 'diagnosis_all_failures'] = json.dumps([])

    # Bucket 2: Null scores (empty/timed-out trajectories) — programmatic label.
    # These are infra/scraping failures, not model behavior. We tag them with
    # `analysis_error` so they are kept in the CSV for inspection, but the summary
    # stats below exclude them from `tasks_diagnosed` and the failure distribution.
    for idx, row in df[null_mask].iterrows():
        explanation = _get_error_explanation(row.get('errors'))
        df.loc[idx, 'diagnosis_primary_mode'] = 'analysis_error'
        df.loc[idx, 'diagnosis_primary_category'] = 'programmatic'
        df.loc[idx, 'diagnosis_primary_explanation'] = explanation
        df.loc[idx, 'diagnosis_all_failures'] = json.dumps([])
        df.loc[idx, 'diagnosis_confidence'] = 1.0
        df.loc[idx, 'diagnosis_summary'] = explanation

    # Bucket 3: Partial/failed scores — LLM diagnosis
    failed_rows = df[needs_llm_mask]
    if failed_rows.empty:
        logger.info("No tasks require LLM diagnosis.")
        return df, None

    client = AsyncLiteLLMClient(config)
    analyzer = DiagnosticAnalyzer(client, temperature=temperature)

    # Trajectory enrichment is always done in-process from raw_conversation_history
    # so the judge sees full tool responses, errors, and assistant reasoning — not the
    # thin parameter-only trajectory that some scoring pipelines save.
    logger.info("Trajectory enrichment: in-process from raw_conversation_history (ignoring any saved *_trajectory columns)")

    async def diagnose_row(idx, row):
        """Diagnose a single row via single-turn LLM with v2 taxonomy."""
        coverage_details = {}
        if 'coverage_details_json' in row and pd.notna(row['coverage_details_json']):
            try:
                coverage_details = json.loads(row['coverage_details_json'])
            except json.JSONDecodeError:
                coverage_details = {}

        raw_conversation = row.get('raw_conversation_history', '')
        if pd.isna(raw_conversation):
            raw_conversation = ''

        # Build the enriched trajectory in-process from the raw conversation.
        # This guarantees the judge sees tool responses, errors, and assistant
        # reasoning — not the stripped version some pipelines save.
        enriched_trajectory = ''
        if str(raw_conversation).strip():
            try:
                msgs = parse_conversation(str(raw_conversation))
                turns = build_enriched_trajectory(msgs)
                enriched_trajectory = format_enriched_trajectory_for_judge(turns)
            except Exception as e:
                logger.warning(f"Trajectory enrichment failed for row {idx}: {e}")
                enriched_trajectory = ''

        errors_str = str(row.get('errors', ''))

        # Short-circuit: no conversation at all → programmatic label, not a judge call
        if not str(raw_conversation).strip() and not enriched_trajectory.strip():
            return idx, {
                "primary_failure": {"mode": "analysis_error", "category": "tool_call", "explanation": _get_error_explanation(row.get('errors'))},
                "all_failures": [],
                "confidence": 1.0,
                "summary": _get_error_explanation(row.get('errors')),
            }

        diagnosis = await analyzer.diagnose_failure(
            task_id=str(row.get('TASK', '')),
            prompt=str(row.get('PROMPT', '')),
            expected_trajectory=str(row.get('TRAJECTORY', '')),
            raw_conversation=str(raw_conversation),
            gtfa_claims=str(row.get('GTFA_CLAIMS', '')),
            coverage_score=row.get('coverage_score', 0),
            coverage_details=coverage_details,
            enriched_trajectory=enriched_trajectory,
            errors_str=errors_str,
            final_response=str(row.get('script_model_response', '')),
        )

        return idx, diagnosis

    tasks = [diagnose_row(idx, row) for idx, row in failed_rows.iterrows()]
    results = []
    for future in tqdm(asyncio.as_completed(tasks), total=len(tasks), desc="Diagnosing failures"):
        results.append(await future)

    for idx, diagnosis in results:
        pf = diagnosis.get('primary_failure', {})
        mode = pf.get('mode')
        df.loc[idx, 'diagnosis_primary_mode'] = mode
        # Always derive category from the canonical taxonomy mapping,
        # not the judge's category field. Keeps category_split consistent.
        df.loc[idx, 'diagnosis_primary_category'] = MODE_TO_CATEGORY.get(mode, pf.get('category'))
        df.loc[idx, 'diagnosis_primary_explanation'] = pf.get('explanation')
        df.loc[idx, 'diagnosis_all_failures'] = json.dumps(diagnosis.get('all_failures', []))
        df.loc[idx, 'diagnosis_confidence'] = diagnosis.get('confidence')
        df.loc[idx, 'diagnosis_summary'] = diagnosis.get('summary')

    stats = client.get_stats()
    logger.info(f"Diagnosis complete (v2 taxonomy). API stats: {stats}")

    if 'diagnosis_primary_failure' in df.columns:
        failure_dist = df['diagnosis_primary_failure'].value_counts()
        logger.info("Primary Failure Mode Distribution:")
        for mode, count in failure_dist.items():
            if pd.notna(mode):
                logger.info(f"  {mode}: {count}")

    return df, client


# =========================================================================
# 6. SUMMARY GENERATION (v2: category split, examples, LLM narrative)
# =========================================================================

def _build_programmatic_narrative(summary: dict) -> str:
    """Build a programmatic narrative from the summary stats."""
    parts = []
    scoring = summary.get('scoring', {})
    diagnosis = summary.get('diagnosis', {})
    run_health = summary.get('run_health', {})
    parts.append(f"Model {summary['model_name']} was evaluated on {scoring.get('total_tasks', '?')} tasks.")
    parts.append(f"Mean coverage: {scoring.get('mean_coverage')}, median: {scoring.get('median_coverage')}.")
    parts.append(f"{diagnosis.get('tasks_diagnosed', '?')} tasks ({diagnosis.get('diagnosis_rate_pct', '?')}%) scored below the {summary.get('diagnosis_threshold', '?')} threshold.")
    parts.append(f"Clean runs: {run_health.get('clean_runs', '?')}, max tool calls errors: {run_health.get('max_tool_calls_errors', 0)}, max turns errors: {run_health.get('max_turns_errors', 0)}, timeouts: {run_health.get('timeout_errors', 0)}.")

    cat_split = diagnosis.get('category_split', {})
    if cat_split:
        parts.append(f"Failure categories: tool_call={cat_split.get('tool_call_count', 0)} ({cat_split.get('tool_call_pct', 0)}%), cognitive={cat_split.get('cognitive_count', 0)} ({cat_split.get('cognitive_pct', 0)}%).")

    dist = diagnosis.get('primary_failure_distribution', {})
    if dist:
        top3 = sorted(dist.items(), key=lambda x: x[1], reverse=True)[:3]
        top3_str = ", ".join(f"{m} ({c})" for m, c in top3)
        parts.append(f"Top failure modes: {top3_str}.")

    return " ".join(parts)


async def _generate_llm_narrative(client: 'AsyncLiteLLMClient', summary: dict, df: pd.DataFrame) -> str:
    """Generate an LLM narrative summary (Phase 3: 1 call per model)."""
    summaries = df.loc[df['diagnosis_primary_mode'].notna() & (df['diagnosis_primary_mode'] != ''),
                       'diagnosis_summary'].dropna().tolist()
    sample = summaries[:30]

    prompt = f"""You are writing a summary report for an MCP (Model Context Protocol) tool-use evaluation of model "{summary['model_name']}".

EVALUATION SETUP:
- Models are connected to live MCP tool servers (sandboxed Docker environments) and must use tools to answer multi-step questions.
- Tasks require cross-source reasoning: querying databases, APIs, web search, file systems, etc.
- The model's final text response is scored against ground-truth claims for coverage.

SCORING:
{json.dumps(summary.get('scoring', {}), indent=2)}

RUN HEALTH:
{json.dumps(summary.get('run_health', {}), indent=2)}

FAILURE ANALYSIS:
- Tasks diagnosed: {summary.get('diagnosis', {}).get('tasks_diagnosed', '?')} ({summary.get('diagnosis', {}).get('diagnosis_rate_pct', '?')}%)
- Category split: {json.dumps(summary.get('diagnosis', {}).get('category_split', {}), indent=2)}
- Primary failure distribution: {json.dumps(summary.get('diagnosis', {}).get('primary_failure_distribution', {}), indent=2)}

SAMPLE DIAGNOSIS SUMMARIES (up to 30):
{chr(10).join(f'- {s[:300]}' for s in sample)}

Write a 5-7 sentence narrative for a customer report:
1. Overall performance characterization
2. Dominant failure patterns — what goes wrong most and why
3. Tool call vs cognitive breakdown with insight
4. One specific actionable recommendation
5. What distinguishes this model's failure profile

Be precise with numbers. Write for a technical audience. Do not speculate beyond what the data shows."""

    try:
        result = await client.generate_content(
            messages=[{"role": "user", "content": prompt}],
            response_schema={
                "type": "object",
                "properties": {"narrative": {"type": "string"}},
                "required": ["narrative"]
            },
            temperature=0.0
        )
        return result.get("narrative", "")
    except Exception as e:
        return f"LLM narrative generation failed: {e}"


def _source_examples(df: pd.DataFrame, n_per_mode: int = 3) -> dict:
    """Source 2-3 public-split example tasks per failure mode (highest confidence)."""
    examples = {}
    if 'SPLIT' not in df.columns or 'diagnosis_primary_mode' not in df.columns:
        return examples

    public_df = df[df['SPLIT'] == 'public'].copy()
    if public_df.empty:
        return examples

    for mode in ALL_MODES:
        mode_df = public_df[public_df['diagnosis_primary_mode'] == mode]
        if mode_df.empty:
            continue
        # Sort by confidence descending
        if 'diagnosis_confidence' in mode_df.columns:
            mode_df = mode_df.sort_values('diagnosis_confidence', ascending=False)
        top = mode_df.head(n_per_mode)
        examples[mode] = []
        for _, row in top.iterrows():
            examples[mode].append({
                "task_id": str(row.get('TASK', '')),
                "prompt_snippet": str(row.get('PROMPT', ''))[:200],
                "coverage_score": row.get('coverage_score'),
                "diagnosis_summary": str(row.get('diagnosis_summary', '')),
            })

    return examples


async def create_diagnosis_summary(df: pd.DataFrame, model_name: str, output_path: str, error_stats: dict, failure_threshold: float, client: Optional['AsyncLiteLLMClient'] = None) -> dict:
    """Create and save a JSON summary with v2 taxonomy, category split, and examples."""
    logger = logging.getLogger(__name__)

    total_tasks = len(df)
    valid_scores = df['coverage_score'].dropna()
    failed_mask = (df['coverage_score'] < failure_threshold) & (df['coverage_score'].notna())

    # Separate model-attributable diagnoses (judge-assigned) from infra/programmatic
    # skips (analysis_error, etc.). Skips are tracked separately and excluded from
    # `tasks_diagnosed`, the primary_failure_distribution, and the category_split.
    PROGRAMMATIC_SKIPS = ('analysis_error',)
    if 'diagnosis_primary_mode' in df.columns:
        infra_skip_mask = df['diagnosis_primary_mode'].isin(PROGRAMMATIC_SKIPS)
    else:
        infra_skip_mask = pd.Series([False] * len(df), index=df.index)
    diagnosed_mask = (failed_mask | df.get('diagnosis_primary_mode', pd.Series([pd.NA]*len(df))).notna()) & ~infra_skip_mask
    # A "diagnosis" is a row with a judge-assigned (non-skip) primary_mode.
    diagnosed_mask = df.get('diagnosis_primary_mode', pd.Series([pd.NA]*len(df))).notna() & ~infra_skip_mask
    num_diagnosed = int(diagnosed_mask.sum())
    num_skipped_infra = int(infra_skip_mask.sum())

    # Primary failure distribution — only over model-attributable diagnoses
    primary_dist = {}
    if 'diagnosis_primary_mode' in df.columns:
        dist = df.loc[diagnosed_mask, 'diagnosis_primary_mode'].value_counts()
        primary_dist = {str(k): int(v) for k, v in dist.items() if pd.notna(k)}

    # Category split — derived from canonical mode→category mapping over diagnosed rows only.
    # Always sums to num_diagnosed.
    tool_call_count = 0
    cognitive_count = 0
    if 'diagnosis_primary_mode' in df.columns:
        mode_counts = df.loc[diagnosed_mask, 'diagnosis_primary_mode'].value_counts()
        for mode, count in mode_counts.items():
            cat = MODE_TO_CATEGORY.get(mode)
            if cat == 'tool_call':
                tool_call_count += int(count)
            elif cat == 'cognitive':
                cognitive_count += int(count)
    total_diagnosed = tool_call_count + cognitive_count
    category_split = {
        "tool_call_count": tool_call_count,
        "tool_call_pct": round(tool_call_count / total_diagnosed * 100, 1) if total_diagnosed > 0 else 0,
        "cognitive_count": cognitive_count,
        "cognitive_pct": round(cognitive_count / total_diagnosed * 100, 1) if total_diagnosed > 0 else 0,
    }
    num_failed = num_diagnosed  # legacy alias used downstream in summary JSON

    # Run health
    errors_col = df['errors'].astype(str) if 'errors' in df.columns else pd.Series([''] * total_tasks)
    has_err = (errors_col != '') & (errors_col != '[]') & (errors_col != 'nan')

    context_window_errors = 0
    max_turns_errors = 0
    max_tool_calls_errors = 0
    timeout_errors = 0
    sandbox_errors = 0
    endpoint_errors = 0
    other_errors = 0

    for e in errors_col[has_err]:
        if 'context length' in e:
            context_window_errors += 1
        elif 'max_turns_reached' in e:
            max_turns_errors += 1
        elif 'max_tool_calls_reached' in e:
            max_tool_calls_errors += 1
        elif 'Sandbox' in e and ('failed' in e or 'fetch failed' in e):
            sandbox_errors += 1
        elif 'timeout' in e.lower():
            timeout_errors += 1
        elif 'Engine encountered' in e or 'orchestrator error' in e or 'malformed' in e.lower():
            endpoint_errors += 1
        else:
            other_errors += 1

    clean_runs = int((~has_err).sum())
    total_errors = int(has_err.sum())
    pass_at_075 = round((df['coverage_score'] >= 0.75).sum() / total_tasks * 100, 1) if total_tasks > 0 else 0

    # Source examples from public split
    examples = _source_examples(df)

    summary = {
        "model_name": model_name,
        "timestamp": datetime.now().isoformat(),
        "diagnosis_threshold": failure_threshold,
        "diagnosis_version": "v2-taxonomy",

        "scoring": {
            "total_tasks": total_tasks,
            "mean_coverage": round(float(valid_scores.mean()), 4) if len(valid_scores) > 0 else None,
            "median_coverage": round(float(valid_scores.median()), 4) if len(valid_scores) > 0 else None,
            "pass_rate_0.75": pass_at_075,
        },

        "run_health": {
            "clean_runs": clean_runs,
            "total_errors": total_errors,
            "context_window_errors": context_window_errors,
            "max_turns_errors": max_turns_errors,
            "max_tool_calls_errors": max_tool_calls_errors,
            "timeout_errors": timeout_errors,
            "sandbox_infra_errors": sandbox_errors,
            "model_endpoint_errors": endpoint_errors,
            "other_errors": other_errors,
            "empty_responses": error_stats.get('empty_responses', 0),
        },

        "diagnosis": {
            "tasks_diagnosed": num_failed,
            "diagnosis_rate_pct": round(num_failed / total_tasks * 100, 2) if total_tasks > 0 else 0,
            "avg_coverage_diagnosed": round(float(df.loc[failed_mask, 'coverage_score'].mean()), 4) if num_failed > 0 else None,
            "category_split": category_split,
            "primary_failure_distribution": primary_dist,
            "examples": examples,
        },
    }

    summary["programmatic_narrative"] = _build_programmatic_narrative(summary)

    if client:
        summary["llm_narrative"] = await _generate_llm_narrative(client, summary, df)
    else:
        summary["llm_narrative"] = "Skipped — no LLM client provided."

    with open(output_path, 'w') as f:
        json.dump(summary, f, indent=2)
    logger.info(f"Saved diagnosis summary to '{output_path}'")

    return summary


# =========================================================================
# 7. MAIN EXECUTION
# =========================================================================

async def main(args):
    """Main function to run the v2 diagnosis pipeline."""
    setup_logging(args.verbose)
    logger = logging.getLogger(__name__)

    logger.info("=" * 60)
    logger.info("SINGLE MODEL DIAGNOSIS (v2: enriched trajectory + new taxonomy)")
    logger.info("=" * 60)

    model_name = extract_model_name(args.scored_file)
    logger.info(f"Model name: {model_name}")

    output_dir = args.output_dir if args.output_dir else os.path.dirname(os.path.abspath(args.scored_file))
    os.makedirs(output_dir, exist_ok=True)
    logger.info(f"Output directory: {output_dir}")

    evaluator_safe = args.evaluator_model.replace('/', '-')
    ts = datetime.now().strftime('%Y%m%d_%H%M%S')
    diagnosis_csv = os.path.join(output_dir, f"diagnosis_{model_name}_{evaluator_safe}_{ts}.csv")
    summary_json = os.path.join(output_dir, f"diagnosis_summary_{model_name}_{evaluator_safe}_{ts}.json")

    if args.num_tasks:
        logger.info(f"Running on first {args.num_tasks} tasks only")

    try:
        logger.info(f"Loading scored file: {args.scored_file}")
        df = pd.read_csv(args.scored_file)
        logger.info(f"Loaded {len(df)} rows")

        if args.num_tasks and args.num_tasks > 0:
            original_size = len(df)
            df = df.head(args.num_tasks)
            logger.info(f"Limited from {original_size} to {len(df)} tasks")

        # Step 1: Error analysis
        logger.info("Step 1: Error analysis...")
        error_stats = analyze_error_distribution(df, logger)
        print_error_report(error_stats, logger)

        # Step 2: Failure diagnosis with v2 taxonomy
        logger.info(f"Step 2: Failure diagnosis (threshold: {args.failure_threshold}, taxonomy: v2)...")
        config = DiagnosisConfig(
            model_name=args.evaluator_model,
            api_key=args.api_key,
            base_url=args.base_url,
            semaphore_limit=args.concurrency,
            verbose=args.verbose
        )

        df_diagnosed, diagnosis_client = await run_diagnosis_pipeline(
            df, config, args.failure_threshold, temperature=args.temperature
        )
        config_client_stats = diagnosis_client.get_stats() if diagnosis_client else {"total_prompt_tokens": 0, "total_completion_tokens": 0, "total_tokens": 0}

        # Save full diagnosis CSV
        traj_col = next((c for c in df_diagnosed.columns if c.endswith('_trajectory') and c != 'TRAJECTORY'), None)
        if traj_col:
            df_diagnosed = df_diagnosed.rename(columns={traj_col: 'model_trajectory'})

        output_columns = [
            'TASK', 'PROMPT', 'ENABLED_TOOLS', 'TRAJECTORY', 'GTFA', 'GTFA_CLAIMS', 'SPLIT',
            'script_model_response', 'raw_conversation_history', 'model_trajectory',
            'errors', 'trajectory_time', 'num_retry',
            'coverage_score', 'fully_covered_claims', 'partially_covered_claims',
            'total_claims', 'coverage_details_json', 'evaluation_confidence',
            'diagnosis_primary_mode', 'diagnosis_primary_category', 'diagnosis_primary_explanation',
            'diagnosis_all_failures', 'diagnosis_confidence', 'diagnosis_summary',
        ]
        output_columns = [c for c in output_columns if c in df_diagnosed.columns]
        df_diagnosed[output_columns].to_csv(diagnosis_csv, index=False)
        logger.info(f"Saved diagnosis CSV: {diagnosis_csv}")

        # Step 3: Create summary JSON with examples and narrative
        logger.info("Step 3: Creating summary with examples and narrative...")
        try:
            llm_client = AsyncLiteLLMClient(config)
        except ValueError:
            llm_client = None
        summary = await create_diagnosis_summary(
            df_diagnosed, model_name, summary_json,
            error_stats, args.failure_threshold, client=llm_client
        )

        # Print final summary
        scoring = summary.get('scoring', {})
        run_health = summary.get('run_health', {})
        diagnosis = summary.get('diagnosis', {})
        cat_split = diagnosis.get('category_split', {})
        logger.info("=" * 60)
        logger.info("ANALYSIS COMPLETE (v2 taxonomy)")
        logger.info("=" * 60)
        logger.info(f"  Total Tasks:        {scoring.get('total_tasks')}")
        logger.info(f"  Mean Coverage:      {scoring.get('mean_coverage')}")
        logger.info(f"  Pass Rate @0.75:    {scoring.get('pass_rate_0.75')}%")
        logger.info(f"  Clean Runs:         {run_health.get('clean_runs')}")
        logger.info(f"  Max Tool Calls Err: {run_health.get('max_tool_calls_errors')}")
        logger.info(f"  Max Turns Errors:   {run_health.get('max_turns_errors')}")
        logger.info(f"  Tasks Diagnosed:    {diagnosis.get('tasks_diagnosed')}")
        logger.info(f"  Category Split:     tool_call={cat_split.get('tool_call_count', 0)} ({cat_split.get('tool_call_pct', 0)}%) | cognitive={cat_split.get('cognitive_count', 0)} ({cat_split.get('cognitive_pct', 0)}%)")
        logger.info(f"  Top Failures:       {dict(sorted(diagnosis.get('primary_failure_distribution', {}).items(), key=lambda x: -x[1])[:5])}")
        logger.info(f"  Examples sourced:   {sum(len(v) for v in diagnosis.get('examples', {}).values())} across {len(diagnosis.get('examples', {}))} modes")
        logger.info("-" * 60)
        logger.info(f"Narrative: {summary.get('llm_narrative', 'N/A')}")
        logger.info("=" * 60)

        # Cost estimate
        prompt_tok = config_client_stats.get("total_prompt_tokens", 0)
        completion_tok = config_client_stats.get("total_completion_tokens", 0)
        total_tok = config_client_stats.get("total_tokens", 0)
        PRICING = {
            "gemini/gemini-3.1-pro-preview": (1.25, 10.00),
            "gemini/gemini-2.5-pro": (1.25, 10.00),
            "gemini/gemini-2.5-flash": (0.15, 0.60),
        }
        pricing = PRICING.get(args.evaluator_model)
        if pricing:
            cost = (prompt_tok / 1_000_000 * pricing[0]) + (completion_tok / 1_000_000 * pricing[1])
            logger.info(f"Token usage:    {prompt_tok:,} prompt + {completion_tok:,} completion = {total_tok:,} total")
            logger.info(f"Estimated cost: ${cost:.2f} (at ${pricing[0]}/{pricing[1]} per 1M input/output tokens)")
        else:
            logger.info(f"Token usage:    {prompt_tok:,} prompt + {completion_tok:,} completion = {total_tok:,} total")
            logger.info(f"Estimated cost: unknown (no pricing for {args.evaluator_model})")
        logger.info(f"Output files:")
        logger.info(f"  - {diagnosis_csv}")
        logger.info(f"  - {summary_json}")

    except Exception as e:
        logger.error(f"Pipeline failed: {e}", exc_info=True)
        raise


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Single-model diagnosis with the failure taxonomy (11 modes, 2 categories)."
    )

    parser.add_argument("--scored-file", type=str, required=True, help="Path to scored CSV file")
    parser.add_argument("--evaluator-model", type=str, default=os.getenv("EVAL_LLM_MODEL", "gemini/gemini-3.1-pro-preview"), help="Judge model (LiteLLM format). Defaults to $EVAL_LLM_MODEL, else gemini/gemini-3.1-pro-preview.")
    parser.add_argument("--api-key", type=str, default=None, help="API key (or set EVAL_LLM_API_KEY / LLM_API_KEY env var)")
    parser.add_argument("--base-url", type=str, default=None, help="Proxy base URL (or set EVAL_LLM_BASE_URL / LLM_BASE_URL env var)")
    parser.add_argument("--failure-threshold", type=float, default=1.0, help="Tasks below this threshold get LLM diagnosis (default: 1.0)")
    parser.add_argument("--concurrency", type=int, default=15, help="Number of concurrent API requests (default: 15)")
    parser.add_argument("--output-dir", type=str, default=None, help="Output directory for diagnosis files (default: same as scored file)")
    parser.add_argument("--num-tasks", type=int, default=None, help="Limit to first N tasks (useful for testing)")
    parser.add_argument("--temperature", type=float, default=0.0, help="Sampling temperature for the judge LLM (default: 0.0). Use >0 for std-dev measurements.")
    parser.add_argument("--verbose", action="store_true", help="Enable verbose logging")

    args = parser.parse_args()
    asyncio.run(main(args))
