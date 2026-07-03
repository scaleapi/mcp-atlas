# score_claims.py
#
# Description:
# Evaluates LLM model outputs against ground-truth claims for MCP-Atlas tasks.
# Merges a unified eval CSV (ground truth + claims) with model outputs and
# scores claim coverage via an LLM-as-judge call (default: Gemini 3.1 Pro).
# All rows are kept (including errors) for a holistic picture.
#
# Required files:
#   - Unified eval CSV with columns: TASK, PROMPT, GTFA, GTFA_CLAIMS (+ others ok)
#   - Model output CSV with columns: task_id, trajectory, response
#
# Example Usage:
# python score_claims.py \
#   --groundtruth-file="mcp_unified_eval.csv" \
#   --model-file="model_outputs.csv" \
#   --num-tasks=100
#
# Model name is auto-extracted from the filename (mcp_eval_{model}_{N}tasks_...)
# or can be overridden with --model-name="custom-name".

from dotenv import load_dotenv
load_dotenv()  # loads .env from current working directory

import pandas as pd
import asyncio
import os
import glob
import json
import ast
import re
import logging
import argparse
from typing import List, Dict, Any, Optional
from dataclasses import dataclass
from datetime import datetime

# Third-party libraries
import aiohttp
import random
from tenacity import retry, wait_random_exponential, stop_after_attempt
from tqdm.asyncio import tqdm as async_tqdm
from tqdm import tqdm
import matplotlib.pyplot as plt
import nest_asyncio

# Apply nest_asyncio to allow nested event loops
nest_asyncio.apply()


# =========================================================================
# 1. CONFIGURATION AND SETUP
# =========================================================================

@dataclass
class EvaluatorConfig:
    """Configuration for the evaluator."""
    model_name: str = "gemini/gemini-3.1-pro-preview"
    max_retries: int = 6
    request_delay: float = 0.05
    semaphore_limit: int = 30
    api_key: Optional[str] = None
    base_url: Optional[str] = None
    verbose: bool = True
    num_tasks: Optional[int] = None


def setup_logging(verbose: bool = True):
    """Set up the logging configuration."""
    level = logging.INFO if verbose else logging.WARNING
    logging.basicConfig(
        level=level,
        format='%(asctime)s - %(levelname)s - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("aiohttp").setLevel(logging.WARNING)
    return logging.getLogger(__name__)


# =========================================================================
# 2. DATA PREPROCESSING AND MERGING
# =========================================================================


def merge_gtfa_with_model_data(gtfa_file: str, model_data_file: str, model_name: str, output_file: str, num_tasks: Optional[int] = None):
    """
    Merges the unified ground truth CSV with the model output CSV.
    Keeps all rows including errors for a holistic picture.
    """
    logger = logging.getLogger(__name__)
    logger.info(f"Merging '{gtfa_file}' with '{model_data_file}'...")
    try:
        df_gtfa = pd.read_csv(gtfa_file)

        # Limit number of ground truth tasks if specified
        if num_tasks is not None and num_tasks > 0:
            original_size = len(df_gtfa)
            df_gtfa = df_gtfa.head(num_tasks)
            logger.info(f"  Limited ground truth from {original_size} to {len(df_gtfa)} tasks")

        df_model = pd.read_csv(model_data_file)

        logger.info(f"  Ground truth rows: {len(df_gtfa)}")
        logger.info(f"  Model output rows: {len(df_model)}")

        # Rename the model's trajectory column to avoid conflicts
        if 'trajectory' in df_model.columns:
            df_model = df_model.rename(columns={'trajectory': f'{model_name}_trajectory'})

        # Ensure the merge keys exist
        if 'TASK' not in df_gtfa.columns:
            raise KeyError("'TASK' column not found in ground truth file.")
        if 'task_id' not in df_model.columns:
            raise KeyError("'task_id' column not found in model data file.")

        df_merged = pd.merge(
            df_gtfa,
            df_model,
            left_on='TASK',
            right_on='task_id',
            how='inner'
        )

        if 'task_id' in df_merged.columns:
            df_merged = df_merged.drop(columns=['task_id'])

        logger.info(f"  Merged rows (inner join): {len(df_merged)}")

        # Log response column stats (no filtering — keep all rows)
        response_col = next(
            (col for col in [f"{model_name}_response", "script_model_response", "response"]
             if col in df_merged.columns),
            None
        )
        if response_col:
            empty_mask = df_merged[response_col].isna() | (df_merged[response_col].str.strip() == '')
            error_mask = df_merged[response_col].str.startswith('ERROR:', na=False)
            logger.info(f"  Response column: '{response_col}'")
            logger.info(f"  Rows with valid response:  {(~empty_mask & ~error_mask).sum()}")
            logger.info(f"  Rows with empty response:  {empty_mask.sum()} (kept)")
            logger.info(f"  Rows with ERROR response:  {error_mask.sum()} (kept)")

        # Check GTFA_CLAIMS is populated
        if 'GTFA_CLAIMS' in df_merged.columns:
            empty_claims = df_merged['GTFA_CLAIMS'].isna() | (df_merged['GTFA_CLAIMS'].str.strip() == '')
            logger.info(f"  Rows with valid GTFA_CLAIMS: {(~empty_claims).sum()}")
            if empty_claims.any():
                logger.warning(f"  {empty_claims.sum()} rows have empty GTFA_CLAIMS — will score as 0")

        df_merged.to_csv(output_file, index=False)
        logger.info(f"  Saved merged data to '{output_file}'. Shape: {df_merged.shape}")
        return output_file
    except FileNotFoundError as e:
        logger.error(f"A required file for merging was not found: {e}")
        raise


# =========================================================================
# 3. CLAIM EXTRACTION UTILITIES
# =========================================================================

def clean_claim_text(text: str) -> str:
    """
    Cleans individual claim text by removing unwanted characters and formatting.
    """
    # Strip whitespace
    text = text.strip()
    
    # Remove common bullet point markers and numbering from the start
    text = re.sub(r'^[-*•·◦‣⁃]\s*', '', text)  # Bullet points
    text = re.sub(r'^\d+[.)]\s*', '', text)     # Numbered lists
    
    # Replace Unicode quotes with standard quotes
    text = text.replace('\u201c', '"')  # Left double quote
    text = re.sub(r'[\u201d"]', '"', text)  # Right double quote
    text = text.replace('\u2018', "'")  # Left single quote
    text = text.replace('\u2019', "'")  # Right single quote
    
    # Remove other problematic Unicode characters
    text = text.replace('\u2013', '-')  # En dash
    text = text.replace('\u2014', '-')  # Em dash
    text = text.replace('\u2026', '...')  # Ellipsis
    
    # Clean up any trailing punctuation issues
    text = re.sub(r'[.\s]*["\']+ $', '', text)
    text = re.sub(r'["\']+\.*$', '', text)
    
    # Final strip
    text = text.strip(' \t\n\r')
    
    return text


def extract_claims(claim_blob) -> List[str]:
    """
    Extracts and cleans individual claims from various input formats.
    
    Args:
        claim_blob: Can be:
            - A list of strings (direct claims)
            - A JSON string representing a list
            - A multi-line text with various separators
            - None or empty input
    
    Returns:
        A list of cleaned claim strings
    """
    # Handle None or empty inputs
    if claim_blob is None:
        return []
    
    # If it's already a list, clean each claim
    if isinstance(claim_blob, list):
        cleaned_claims = []
        for claim in claim_blob:
            cleaned = clean_claim_text(str(claim))
            if cleaned and len(cleaned) > 3:
                cleaned_claims.append(cleaned)
        return cleaned_claims
    
    # Convert to string if not already
    if not isinstance(claim_blob, str):
        claim_blob = str(claim_blob)
    
    # Remove any leading/trailing whitespace
    claim_blob = claim_blob.strip()
    
    # Return empty list for empty strings
    if not claim_blob:
        return []
    
    # Try to parse as JSON/Python list first
    if claim_blob.startswith('[') and claim_blob.endswith(']'):
        try:
            parsed_list = json.loads(claim_blob)
            if isinstance(parsed_list, list):
                cleaned_claims = []
                for claim in parsed_list:
                    cleaned = clean_claim_text(str(claim))
                    if cleaned and len(cleaned) > 3:
                        cleaned_claims.append(cleaned)
                return cleaned_claims
        except (json.JSONDecodeError, ValueError):
            try:
                parsed_list = ast.literal_eval(claim_blob)
                if isinstance(parsed_list, list):
                    cleaned_claims = []
                    for claim in parsed_list:
                        cleaned = clean_claim_text(str(claim))
                        if cleaned and len(cleaned) > 3:
                            cleaned_claims.append(cleaned)
                    return cleaned_claims
            except (ValueError, SyntaxError):
                pass
    
    # Fallback to text-splitting logic
    separators = ["\n•", "\n-", "\n*", "\n1.", "\n2.", ";", "||"]
    for sep in separators:
        if sep in claim_blob:
            parts = claim_blob.split(sep)
            claims = []
            for p in parts:
                cleaned = clean_claim_text(p)
                if cleaned and len(cleaned) > 3:
                    claims.append(cleaned)
            if claims:
                return claims
    
    # Try splitting by newlines as last resort
    lines = claim_blob.strip().split('\n')
    claims = []
    for line in lines:
        cleaned = clean_claim_text(line)
        if cleaned and len(cleaned) > 3:
            claims.append(cleaned)
    return claims


# =========================================================================
# 4. LITELLM CLIENT AND EVALUATION
# =========================================================================

def get_single_claim_evaluation_schema():
    """Define the response schema for single claim evaluation"""
    return {
        "type": "object",
        "properties": {
            "claim_text": {"type": "string"},
            "coverage_outcome": {
                "type": "string",
                "enum": ["fulfilled", "partially_fulfilled", "not_fulfilled"]
            },
            "justification": {"type": "string"},
            "confidence_level": {
                "type": "number"
            }
        },
        "required": ["claim_text", "coverage_outcome", "justification", "confidence_level"]
    }


class AsyncLiteLLMClient:
    """Manages async LiteLLM proxy requests with rate limiting"""

    def __init__(self, config: EvaluatorConfig):
        self.config = config
        self.semaphore = asyncio.Semaphore(config.semaphore_limit)
        self.logger = logging.getLogger(__name__)
        self.request_count = 0
        self.error_count = 0

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
        all_keys = [k.strip() for k in raw_keys.split(",") if k.strip()]
        if not all_keys:
            raise ValueError("API key not found. Set EVAL_LLM_API_KEY (or LLM_API_KEY) env var, or pass --api-key.")
        self.api_keys = all_keys
        self.logger.info(f"Loaded {len(self.api_keys)} API key(s) — rotating per request")
        if not self.base_url:
            raise ValueError("Base URL not found. Set EVAL_LLM_BASE_URL (or LLM_BASE_URL) env var, or pass --base-url.")

    @retry(
        wait=wait_random_exponential(min=60, max=120),
        stop=stop_after_attempt(8),
        reraise=True,
        before_sleep=lambda retry_state: logging.getLogger(__name__).debug(f"Retry {retry_state.attempt_number}/8 after {retry_state.outcome.exception().__class__.__name__}: {str(retry_state.outcome.exception())[:150]}... waiting {retry_state.next_action.sleep:.1f}s")
    )
    async def generate_structured_content(self, prompt: str, response_schema: Dict, temperature: float = 0.0) -> Dict:
        """Generate structured content via LiteLLM proxy with retry logic."""
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
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": temperature,
                    "response_format": {
                        "type": "json_schema",
                        "json_schema": {
                            "name": "claim_evaluation",
                            "schema": response_schema,
                        },
                    },
                }

                async with aiohttp.ClientSession() as session:
                    async with session.post(
                        url, headers=headers, json=payload, timeout=aiohttp.ClientTimeout(total=60)
                    ) as resp:
                        if resp.status != 200:
                            body = await resp.text()
                            raise RuntimeError(f"LiteLLM API returned {resp.status}: {body}")
                        data = await resp.json()

                await asyncio.sleep(self.config.request_delay)

                content = data["choices"][0]["message"]["content"]
                return json.loads(content)

            except Exception as e:
                self.error_count += 1
                self.logger.debug(f"LiteLLM API error (will retry): {e}")
                raise

    def get_stats(self) -> Dict[str, int]:
        """Get request statistics"""
        return {
            "total_requests": self.request_count,
            "errors": self.error_count,
            "success_rate": (self.request_count - self.error_count) / max(self.request_count, 1)
        }


class CoverageEvaluator:
    """Evaluates claim coverage with continuous scoring (0-1) - one claim at a time."""

    def __init__(self, client: AsyncLiteLLMClient, config: EvaluatorConfig):
        self.client = client
        self.config = config
        self.logger = logging.getLogger(__name__)

    def _get_single_claim_evaluation_prompt(self, claim: str, response: str) -> str:
        """Generate prompt for evaluating a single claim"""
        return f"""You are evaluating how well a model's response addresses a specific expert-defined claim.
SCORING CRITERIA:
- fulfilled: Claim is completely and accurately addressed. The response covers all key details.
- partially_fulfilled: Claim is partially addressed. The response covers some but not all key details.
- not_fulfilled: Claim is not addressed. The response does not include any key details.
NUMERICAL COMPARISON GUIDELINES:
- For numerical values, use reasonable approximation thresholds:
  * Exact match NOT required for decimals
  * Values within 5% of the claimed number are considered matching
  * For percentages, ±1 percentage points is acceptable
  * Round to appropriate significant figures based on context
- Consider the precision appropriate to the domain:
  * Scientific measurements may need higher precision
  * General statistics/estimates can have looser matching
  * Financial figures should match to reasonable business precision (e.g., millions/billions don't need exact cents)
- If a number is expressed differently but mathematically equivalent (e.g., "0.5" vs "50%" vs "half"), consider it a match
CLAIM TO EVALUATE:
{claim}
MODEL RESPONSE TO ANALYZE:
{response}
INSTRUCTIONS:
1. Determine if the core requirement of the claim is met in the response
2. Check if all key components from the claim appear substantively in the response
   - For numerical values, apply the flexible matching guidelines above
   - Focus on whether the same magnitude and meaning are conveyed
3. Assign the appropriate coverage_outcome
4. Provide specific justification referencing what was/wasn't covered
   - When numbers differ slightly, note if they're within acceptable range
5. Provide a confidence level (0.0-1.0) for your assessment
Be rigorous but fair in your assessment. Focus on whether the response conveys the same information as the claim, not on exact numerical precision unless precision is critical to the claim's meaning."""

    async def evaluate_single_claim(self, claim: str, response: str) -> Dict[str, Any]:
        """Evaluate a single claim against the response"""
        prompt = self._get_single_claim_evaluation_prompt(claim, response)
        
        try:
            result = await self.client.generate_structured_content(
                prompt=prompt,
                response_schema=get_single_claim_evaluation_schema(),
                temperature=0.0
            )
            return result
        except Exception as e:
            self.logger.error(f"⚠️ CLAIM SKIPPED after all retries exhausted: {e}")
            return {
                "claim_text": claim,
                "coverage_outcome": "not_fulfilled",
                "justification": f"Evaluation failed: {e}",
                "confidence_level": 0.1
            }

    async def evaluate(self, claims: List[str], response: str) -> Dict[str, Any]:
        """Evaluate all claims by making individual API calls for each claim"""
        if not claims:
            return {"per_claim": [], "coverage_score": None, "explanation": "No claims provided", "confidence": 1.0}
        
        # Define coverage outcome to score mapping
        coverage_to_score = {
            "fulfilled": 1.0,
            "partially_fulfilled": 0.5,
            "not_fulfilled": 0.0
        }
        
        # Evaluate each claim individually
        tasks = [self.evaluate_single_claim(claim, response) for claim in claims]
        claim_results = await asyncio.gather(*tasks)
        
        # Aggregate results
        per_claim = []
        total_score = 0
        fulfilled_count = 0
        partially_fulfilled_count = 0
        total_confidence = 0
        
        for result in claim_results:
            coverage_outcome = result.get("coverage_outcome", "not_fulfilled")
            score = coverage_to_score.get(coverage_outcome, 0.0)
            total_score += score
            total_confidence += result.get("confidence_level", 0.5)
            
            if score >= 1.0:
                fulfilled_count += 1
                covered = True
            elif score >= 0.5:
                partially_fulfilled_count += 1
                covered = "partial"
            else:
                covered = False
            
            per_claim.append({
                "claim": result.get("claim_text", ""),
                "score": score,
                "covered": covered,
                "reason": result.get("justification", "")
            })
        
        coverage_score = round(total_score / len(claims), 3) if claims else 0.0
        avg_confidence = total_confidence / len(claims) if claims else 0.5
        
        return {
            "per_claim": per_claim,
            "coverage_score": coverage_score,
            "total_claims": len(claims),
            "fully_covered_claims": fulfilled_count,
            "partially_covered_claims": partially_fulfilled_count,
            "explanation": "Evaluation complete",
            "confidence": avg_confidence
        }


# =========================================================================
# 5. DATAFRAME EVALUATION
# =========================================================================

async def evaluate_dataframe_async(df: pd.DataFrame, evaluator: CoverageEvaluator, model_name: str) -> pd.DataFrame:
    """Asynchronously evaluates all rows in a dataframe."""
    logger = logging.getLogger(__name__)
    
    async def safe_evaluate(row_idx, row):
        try:
            claims = extract_claims(row.get("GTFA_CLAIMS", ""))
            # Determine the correct response column
            response_col = next(
                (col for col in [f"{model_name}_response", "script_model_response", "response"] 
                 if col in row and pd.notna(row[col])), 
                None
            )
            response = row.get(response_col, "") if response_col else ""
            # Skip Gemini call for empty/error responses — score 0 directly
            if not response or not response.strip() or str(response).startswith("ERROR:"):
                return row_idx, {
                    "per_claim": [{"claim": c, "score": 0.0, "covered": False, "reason": "Empty or error response"} for c in claims],
                    "coverage_score": 0.0,
                    "total_claims": len(claims),
                    "fully_covered_claims": 0,
                    "partially_covered_claims": 0,
                    "explanation": "Skipped — empty or error response",
                    "confidence": 1.0,
                }
            # Truncate oversized responses to avoid exceeding evaluator context window (Gemini 1M tokens ~ 4M chars)
            MAX_RESPONSE_CHARS = 500_000
            if len(response) > MAX_RESPONSE_CHARS:
                logger.warning(f"Row {row_idx}: response truncated from {len(response):,} to {MAX_RESPONSE_CHARS:,} chars")
                response = response[:MAX_RESPONSE_CHARS] + "\n\n[TRUNCATED — original response was too long]"
            result = await evaluator.evaluate(claims, response)
            return row_idx, result
        except Exception as e:
            logger.error(f"Error processing row {row_idx}: {e}")
            return row_idx, {"coverage_score": None, "explanation": f"Failed: {e}"}

    tasks = [safe_evaluate(idx, row) for idx, row in df.iterrows()]
    results_list = [await f for f in tqdm(asyncio.as_completed(tasks), total=len(tasks), desc="Scoring Rows")]
    
    results_dict = {idx: res for idx, res in results_list}
    
    out_df = df.copy()
    result_cols = {
        "coverage_score": [], "fully_covered_claims": [], "partially_covered_claims": [],
        "total_claims": [], "coverage_details_json": [], "evaluation_confidence": []
    }
    
    for idx in df.index:
        result = results_dict.get(idx, {})
        result_cols["coverage_score"].append(result.get("coverage_score"))
        result_cols["fully_covered_claims"].append(result.get("fully_covered_claims", 0))
        result_cols["partially_covered_claims"].append(result.get("partially_covered_claims", 0))
        result_cols["total_claims"].append(result.get("total_claims", 0))
        result_cols["coverage_details_json"].append(json.dumps(result))
        result_cols["evaluation_confidence"].append(result.get("confidence", 0.0))
        
    for col, data in result_cols.items():
        out_df[col] = data
        
    return out_df


async def run_scoring_evaluation(input_csv: str, output_csv: str, config: EvaluatorConfig, model_name: str) -> pd.DataFrame:
    """Main async entry point for the scoring process."""
    logger = logging.getLogger(__name__)
    logger.info(f"Step 3: Starting scoring evaluation for '{input_csv}'...")
    
    df_input = pd.read_csv(input_csv)
    
    # Log dataset size
    logger.info(f"Processing {len(df_input)} tasks for scoring")
    
    client = AsyncLiteLLMClient(config)
    evaluator = CoverageEvaluator(client, config)
    
    df_scored = await evaluate_dataframe_async(df_input, evaluator, model_name)
    df_scored.to_csv(output_csv, index=False)
    
    logger.info(f"✅ Saved scored file to '{output_csv}'")
    valid_scores = df_scored["coverage_score"].dropna()
    logger.info(f"Evaluation complete. Average coverage: {valid_scores.mean():.3f}")
    
    # Log API stats
    stats = client.get_stats()
    logger.info(f"API stats: {stats}")
    
    return df_scored


# =========================================================================
# 6. STATISTICAL ANALYSIS AND PLOTTING
# =========================================================================

def _compute_split_stats(df: pd.DataFrame, model_name: str, evaluator_model: str = None) -> Dict[str, Any]:
    """Compute coverage statistics for a dataframe slice."""
    scores = df["coverage_score"].dropna().to_numpy()
    total_rows = len(df)
    valid_count = len(scores)
    empty_count = total_rows - valid_count

    # Count tasks with runtime errors (from scraping) even if they have a response
    tasks_with_errors = 0
    if 'errors' in df.columns:
        for errors_val in df['errors']:
            if pd.notna(errors_val) and str(errors_val).strip() not in ('[]', '', 'nan'):
                tasks_with_errors += 1

    stats = {
        "model_name": model_name,
        "evaluator_model": evaluator_model,
        "total_tasks": total_rows,
        "valid_responses": valid_count,
        "empty_or_error": empty_count,
        "tasks_with_scraping_errors": tasks_with_errors,
        "clean_runs": total_rows - empty_count - tasks_with_errors,
        "mean_coverage": round(float(scores.mean()), 4) if valid_count > 0 else None,
        "pass_rate_0.50": round(float((scores >= 0.50).sum() / valid_count * 100), 2) if valid_count > 0 else None,
        "pass_rate_0.75": round(float((scores >= 0.75).sum() / valid_count * 100), 2) if valid_count > 0 else None,
    }
    return stats


def generate_statistics_and_plots(scored_csv_path: str, model_name: str, output_dir: str, evaluator_model: str = None):
    """Generates summary stats JSON (all/public/private splits) and a histogram plot."""
    logger = logging.getLogger(__name__)
    logger.info(f"Step 4: Generating statistics and plots for '{scored_csv_path}'...")

    try:
        df = pd.read_csv(scored_csv_path)
        if "coverage_score" not in df.columns:
            raise KeyError("'coverage_score' column missing.")

        # --- Compute stats for all 3 splits ---
        splits = {"all": df}
        if "SPLIT" in df.columns:
            splits["public"] = df[df["SPLIT"] == "public"]
            splits["private"] = df[df["SPLIT"] == "private"]
        else:
            logger.warning("No SPLIT column found — reporting 'all' only")

        # Load run config from scraping output if available
        # First check the output dir (new-style runs), then fall back to data/runs/<model>/
        run_config = None
        for search_dir in [output_dir, os.path.join('data', 'runs', model_name)]:
            if run_config:
                break
            if os.path.isdir(search_dir):
                config_files = sorted(glob.glob(os.path.join(search_dir, '*_config.json')), key=os.path.getmtime, reverse=True)
                if config_files:
                    try:
                        with open(config_files[0]) as f:
                            run_config = json.load(f)
                        logger.info(f"Loaded run config from {config_files[0]}")
                    except Exception:
                        pass

        # Always include key run params — show None explicitly so it's clear what was/wasn't set
        CONFIG_KEYS = ('strategy', 'max_turns', 'max_tool_calls', 'tool_output_cap', 'context_window_management', 'reasoning_effort', 'extra_llm_params')
        config_summary = {k: run_config.get(k) for k in CONFIG_KEYS} if run_config else {k: None for k in CONFIG_KEYS}

        for split_name, split_df in splits.items():
            stats = _compute_split_stats(split_df, model_name, evaluator_model)
            stats['run_config'] = config_summary
            print(f"\nCoverage Score Summary [{split_name}] ({len(split_df)} tasks):")
            print(f"  >> mean_coverage: {stats['mean_coverage']}")
            print(f"  >> pass_rate_0.75: {stats['pass_rate_0.75']}")
            print(f"  >> max_turns: {config_summary.get('max_turns')}")
            print(f"  >> max_tool_calls: {config_summary.get('max_tool_calls')}")
            for k, v in stats.items():
                if k != 'run_config':
                    print(f"  {k}: {v}")
            stats_path = os.path.join(output_dir, f"coverage_stats_{model_name}_{split_name}.json")
            with open(stats_path, "w") as f:
                json.dump(stats, f, indent=2)
            logger.info(f"Saved {split_name} statistics to '{stats_path}'")

        # Also save a single combined JSON with all splits
        combined = {}
        for split_name, split_df in splits.items():
            combined[split_name] = _compute_split_stats(split_df, model_name, evaluator_model)
            combined[split_name]['run_config'] = config_summary
        combined_path = os.path.join(output_dir, f"coverage_stats_{model_name}_combined.json")
        with open(combined_path, "w") as f:
            json.dump(combined, f, indent=2)
        logger.info(f"Saved combined statistics to '{combined_path}'")

        # --- Generate and save histogram plot ---
        scores = df["coverage_score"].dropna().to_numpy()
        if len(scores) > 0:
            fig, ax = plt.subplots(figsize=(8, 5))
            ax.hist(scores, bins=min(50, len(scores)), edgecolor="black", alpha=0.7)
            ax.set_title(f"Coverage Score Distribution ({model_name})")
            ax.set_xlabel("Coverage Score")
            ax.set_ylabel("Frequency")
            ax.axvline(scores.mean(), color='red', linestyle='--', label=f'Mean: {scores.mean():.3f}')
            ax.legend()
            plt.tight_layout()

            plot_path = os.path.join(output_dir, f"coverage_histogram_{model_name}.png")
            plt.savefig(plot_path)
            logger.info(f"Saved histogram plot to '{plot_path}'")
            plt.close(fig)
        else:
            logger.warning("No valid scores to plot")

    except FileNotFoundError:
        logger.error(f"Scored file not found at '{scored_csv_path}'")
        raise
    except Exception as e:
        logger.error(f"Failed to generate statistics and plots: {e}")
        raise


# =========================================================================
# 7. MAIN EXECUTION
# =========================================================================

async def main(args):
    """Main function to run the scoring pipeline."""
    setup_logging(args.verbose)
    logger = logging.getLogger(__name__)
    
    logger.info("=" * 60)
    logger.info("CLAIM SCORING PIPELINE")
    logger.info("=" * 60)
    
    # Auto-extract model name from model file if not provided
    if not args.model_name:
        basename = os.path.basename(args.model_file)
        match = re.match(r"mcp_eval_(.+?)_\d+tasks_", basename)
        if match:
            args.model_name = match.group(1)
            logger.info(f"Auto-detected model name: {args.model_name}")
        else:
            args.model_name = os.path.splitext(basename)[0]
            logger.warning(f"Could not parse model name from filename, using: {args.model_name}")

    # Log if running on limited tasks
    if args.num_tasks:
        logger.info(f"Running evaluation on first {args.num_tasks} tasks only")

    # Create output directory inside scoring_results/
    if args.output_dir:
        output_dir = args.output_dir
    else:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        num_str = args.num_tasks if args.num_tasks else "All"
        output_dir = os.path.join("scoring_results", f"{args.model_name}_n{num_str}_{timestamp}")
    os.makedirs(output_dir, exist_ok=True)
    logger.info(f"Output directory: {output_dir}")

    # Define file paths for each stage
    merged_path = os.path.join(output_dir, f"merged_{args.model_name}.csv")
    scored_path = os.path.join(output_dir, f"scored_{args.model_name}.csv")

    try:
        # --- Create Evaluator Configuration ---
        config = EvaluatorConfig(
            model_name=args.evaluator_model,
            api_key=args.api_key,
            base_url=args.base_url,
            semaphore_limit=args.concurrency,
            num_tasks=args.num_tasks
        )

        # --- Pipeline Execution ---
        # 1. Merge unified ground truth CSV with model output data
        merge_gtfa_with_model_data(args.groundtruth_file, args.model_file, args.model_name, merged_path, args.num_tasks)

        # 2. Pre-scoring summary
        df_check = pd.read_csv(merged_path)
        logger.info("=" * 60)
        logger.info("PRE-SCORING SUMMARY")
        logger.info(f"  Tasks to score:    {len(df_check)}")
        logger.info(f"  Groundtruth file:  {args.groundtruth_file}")
        logger.info(f"  Model output file: {args.model_file}")
        logger.info(f"  Model name:        {args.model_name}")
        logger.info(f"  Evaluator model:   {args.evaluator_model}")
        logger.info(f"  Concurrency:       {args.concurrency}")
        logger.info("=" * 60)

        # 3. Run scoring evaluation
        await run_scoring_evaluation(merged_path, scored_path, config, args.model_name)

        # 4. Generate statistics and plots
        generate_statistics_and_plots(scored_path, args.model_name, output_dir, args.evaluator_model)

        logger.info(f"\nScoring pipeline finished successfully!")
        logger.info(f"Scored output: {scored_path}")
        
        if args.num_tasks:
            logger.info(f"📊 Note: Results are based on {args.num_tasks} tasks only")

    except (FileNotFoundError, KeyError) as e:
        logger.error(f"Pipeline stopped due to an error: {e}")
    except Exception as e:
        logger.error(f"An unexpected error occurred: {e}", exc_info=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Score LLM model outputs against ground truth claims."
    )
    
    # Required arguments
    parser.add_argument(
        "--groundtruth-file", 
        type=str, 
        required=True, 
        help="Path to the ground truth CSV file (with TASK, PROMPT, GTFA_CLAIMS columns)."
    )
    parser.add_argument(
        "--model-file", 
        type=str, 
        required=True, 
        help="Path to the model output CSV file (with task_id, response columns)."
    )
    parser.add_argument(
        "--model-name",
        type=str,
        default=None,
        help="Short name for the model. Auto-extracted from --model-file if not provided."
    )

    # Optional arguments
    parser.add_argument(
        "--evaluator-model",
        type=str,
        default=os.getenv("EVAL_LLM_MODEL", "gemini/gemini-3.1-pro-preview"),
        help="Judge model (LiteLLM format). Defaults to $EVAL_LLM_MODEL, else gemini/gemini-3.1-pro-preview."
    )
    parser.add_argument(
        "--api-key",
        type=str,
        default=None,
        help="API key. If not provided, uses EVAL_LLM_API_KEY (or LLM_API_KEY) env var."
    )
    parser.add_argument(
        "--base-url",
        type=str,
        default=None,
        help="Proxy base URL. If not provided, uses EVAL_LLM_BASE_URL (or LLM_BASE_URL) env var."
    )
    parser.add_argument(
        "--output-dir",
        type=str,
        default=None,
        help="Directory to save all output files. Defaults to scoring_results_{model_name}_{timestamp}."
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=None,
        help="Number of concurrent requests to the judge API. Auto-set per model if not specified (pro=12, flash=5, otherwise 10)."
    )
    parser.add_argument(
        "--num-tasks", 
        type=int, 
        default=None, 
        help="Limit evaluation to first N tasks (useful for testing)."
    )
    parser.add_argument(
        "--verbose", 
        action="store_true", 
        help="Enable verbose logging."
    )
    
    args = parser.parse_args()

    # Auto-set concurrency based on evaluator model if not explicitly provided
    if args.concurrency is None:
        model = args.evaluator_model.lower()
        if "pro" in model:
            args.concurrency = 12
        elif "flash" in model:
            args.concurrency = 5
        else:
            args.concurrency = 10

    # Run the main async function
    asyncio.run(main(args))
