# mcp_eval_scores.py
#
# Description:
# This script processes, evaluates, and analyzes model performance based on ground truth data.
# Uses LiteLLM for all providers (Gemini, OpenAI, Claude, etc.) - unified interface.
#
# Example Usage from command line:
#
# uv run mcp_evals_scores.py \
#   --input-file="completion_results/sample_4o_results.csv" \
#   --model-name="gpt4o" \
#   --evaluator-model="gemini/gemini-2.5-pro" \  # optional
#   --num-tasks=10  # optional

import pandas as pd
import numpy as np
import asyncio
import os
import json
import ast
import json
import logging
import argparse
from typing import List, Dict, Any, Optional, Tuple, Set
from dataclasses import dataclass
from enum import Enum
from datetime import datetime
from collections import defaultdict, Counter
from abc import ABC, abstractmethod

# Third-party libraries
import litellm
from tenacity import retry, wait_random_exponential, stop_after_attempt
from tqdm.asyncio import tqdm as async_tqdm
from tqdm import tqdm
import matplotlib.pyplot as plt
import nest_asyncio
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

# Apply nest_asyncio to allow nested event loops
nest_asyncio.apply()

# Configure LiteLLM - suppress verbose logging
litellm.set_verbose = False
logging.getLogger("LiteLLM").setLevel(logging.WARNING)


# =========================================================================
# 1. CONFIGURATION AND SETUP
# =========================================================================

@dataclass
class EvaluatorConfig:
    """Configuration for the evaluator and analyzer."""
    model_name: str
    semaphore_limit: int
    request_delay: float = 0.2
    verbose: bool = True
    save_partial_on_error: bool = True
    strict_evaluation: bool = True
    num_tasks: Optional[int] = None

def setup_logging(verbose: bool = True):
    """Set up the logging configuration."""
    level = logging.INFO if verbose else logging.WARNING
    logging.basicConfig(
        level=level,
        format='%(asctime)s - %(levelname)s - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    return logging.getLogger(__name__)

def get_litellm_config():
    """Get LiteLLM configuration from environment variables."""
    api_key = os.getenv("EVAL_LLM_API_KEY") or os.getenv("LLM_API_KEY")
    if not api_key:
        raise ValueError("LiteLLM API key not found. Set EVAL_LLM_API_KEY or LLM_API_KEY env var.")
    
    api_base = os.getenv("EVAL_LLM_BASE_URL") or os.getenv("LLM_BASE_URL", "")
    return api_key, api_base


# =========================================================================
# 2. CORE EVALUATION FRAMEWORK (SCORING) - GEMINI VERSION
# =========================================================================

import json
import ast
import re
from typing import List, Union

def extract_claims(claim_blob: Union[str, List, None]) -> List[str]:
    """
    Extracts and cleans individual claims from various input formats.
    
    Args:
        claim_blob: Can be:
            - A list of strings (direct claims)
            - A list of dicts with 'claim' key (e.g., [{"claim": "text", "essential": "yes"}])
            - A JSON string representing a list
            - A multi-line text with various separators
            - None or empty input
    
    Returns:
        A list of cleaned claim strings
    """
    
    # Handle None or empty inputs
    if claim_blob is None:
        return []
    
    # If it's already a list, process based on content type
    if isinstance(claim_blob, list):
        cleaned_claims = []
        for item in claim_blob:
            # Handle object format: {"claim": "text", "essential": "yes"}
            if isinstance(item, dict) and 'claim' in item:
                claim_text = item['claim']
                cleaned = clean_claim_text(str(claim_text))
                if cleaned and len(cleaned) > 3:
                    cleaned_claims.append(cleaned)
            # Handle string format
            else:
                cleaned = clean_claim_text(str(item))
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
    # This handles cases like: '["claim1", "claim2"]' or '[{"claim": "text", "essential": "yes"}]'
    if claim_blob.startswith('[') and claim_blob.endswith(']'):
        try:
            parsed_list = json.loads(claim_blob)
            if isinstance(parsed_list, list):
                # Clean and filter the parsed claims
                cleaned_claims = []
                for item in parsed_list:
                    # Handle object format: {"claim": "text", "essential": "yes"}
                    if isinstance(item, dict) and 'claim' in item:
                        claim_text = item['claim']
                        cleaned = clean_claim_text(str(claim_text))
                        if cleaned and len(cleaned) > 3:
                            cleaned_claims.append(cleaned)
                    # Handle string format
                    else:
                        cleaned = clean_claim_text(str(item))
                        if cleaned and len(cleaned) > 3:
                            cleaned_claims.append(cleaned)
                return cleaned_claims
        except (json.JSONDecodeError, ValueError) as json_error:
            # If JSON parsing fails, try ast.literal_eval as fallback
            # ast.literal_eval is more forgiving with Python string literals
            try:
                parsed_list = ast.literal_eval(claim_blob)
                if isinstance(parsed_list, list):
                    cleaned_claims = []
                    for item in parsed_list:
                        # Handle object format: {"claim": "text", "essential": "yes"}
                        if isinstance(item, dict) and 'claim' in item:
                            claim_text = item['claim']
                            cleaned = clean_claim_text(str(claim_text))
                            if cleaned and len(cleaned) > 3:
                                cleaned_claims.append(cleaned)
                        # Handle string format
                        else:
                            cleaned = clean_claim_text(str(item))
                            if cleaned and len(cleaned) > 3:
                                cleaned_claims.append(cleaned)
                    return cleaned_claims
            except (ValueError, SyntaxError):
                # If both parsing methods fail, log the issue and continue to text-splitting logic
                # This might happen with malformed JSON like '["text "inner" text"]'
                # where CSV double-quotes were converted incorrectly
                import logging
                logging.debug(f"Failed to parse claim_blob as JSON or Python literal: {claim_blob[:100]}")
                pass
    
    # Try to detect numbered list pattern (1. 2. 3. etc.) using regex
    # This handles patterns like "1. claim\n2. claim\n3. claim"
    numbered_pattern = r'(?:^|\n)(\d+)\.\s+'
    if re.search(numbered_pattern, claim_blob):
        # Split by numbered pattern
        parts = re.split(numbered_pattern, claim_blob)
        
        # parts will be like: ['', '1', 'claim text', '2', 'claim text', ...]
        # We need to pair up the numbers with their text
        claims = []
        i = 1
        while i < len(parts):
            # Skip the number itself, take the text
            if i + 1 < len(parts):
                claim_text = parts[i + 1].strip()
                if claim_text and len(claim_text) > 3:
                    # Clean up the claim text
                    claim_text = claim_text.rstrip('\n').strip()
                    claims.append(claim_text)
                i += 2
            else:
                break
        
        if claims:
            return claims
    
    # Fallback to original text-splitting logic for backward compatibility
    # This handles plain text with various separators
    separators = ["\n•", "\n-", "\n*", ";", "||"]
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


def clean_claim_text(text: str) -> str:
    """
    Cleans individual claim text by removing unwanted characters and formatting.
    
    Args:
        text: Raw claim text
    
    Returns:
        Cleaned claim text
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
    
    # Clean up any trailing punctuation issues (like ." or .")
    text = re.sub(r'[.\s]*["\']+$', '', text)  # Remove trailing quotes with dots
    text = re.sub(r'["\']+\.*$', '', text)     # Remove trailing quotes and dots
    
    # Final strip of whitespace and basic punctuation
    text = text.strip(' \t\n\r')
    
    return text


# Define Gemini schemas - Modified for single claim evaluation
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

# Separate schemas for trajectory comparison components
def get_diagnosis_schema():
    """Define the response schema for diagnosis only"""
    return {
        "type": "object",
        "properties": {
            "primary_failure_mode": {
                "type": "string",
                "enum": [
                    "no_tools_called",
                    "missing_tool_calls", 
                    "wrong_tool_selection",
                    "incorrect_tool_parameters",
                    "tool_execution_order",
                    "redundant_tool_calls",
                    "misunderstood_task",
                    "partial_task_completion",
                    "skipped_steps",
                    "incorrect_conclusion",
                    "auth_failure",
                    "capability_discovery_missing",
                    "env_constraint_violation",
                    "rate_limit_handling"
                ]
            },
            "secondary_failures": {
                "type": "array",
                "items": {
                    "type": "string",
                    "enum": [
                        "no_tools_called",
                        "missing_tool_calls",
                        "wrong_tool_selection",
                        "incorrect_tool_parameters",
                        "tool_execution_order",
                        "redundant_tool_calls",
                        "misunderstood_task",
                        "partial_task_completion",
                        "skipped_steps",
                        "incorrect_conclusion",
                        "auth_failure",
                        "capability_discovery_missing",
                        "env_constraint_violation",
                        "rate_limit_handling"
                    ]
                }
            },
            "confidence_score": {
                "type": "number"
            },
            "brief_explanation": {"type": "string"}
        },
        "required": ["primary_failure_mode", "secondary_failures", "confidence_score", "brief_explanation"]
    }
def get_trajectory_analysis_schema():
    action = {
        "type": "object",
        "properties": {
            "tool": {"type": "string"},         # make optional by removing from "required" if you want
            "operation": {"type": "string"},
            # IMPORTANT: don't use empty OBJECT; use string (you can JSON-dump params here)
            "parameters": {"type": "string"},
        },
        "required": ["tool", "operation", "parameters"],
    }

    divergence_item = {
        "type": "object",
        "properties": {
            "step_number": {"type": "integer"},
            "expected_action": action,
            "actual_action": action,
            "impact": {"type": "string"},
            "severity": {"type": "string", "enum": ["critical", "high", "medium", "low"]},
        },
        "required": ["step_number", "expected_action", "actual_action", "impact", "severity"],
    }

    mismatch = {
        "type": "object",
        "properties": {
            "tool": {"type": "string"},
            "parameter": {"type": "string"},
            "expected_value": {"type": "string"},
            "actual_value": {"type": "string"},
            "consequence": {"type": "string"},
        },
        "required": ["tool", "parameter", "expected_value", "actual_value", "consequence"],
    }

    return {
        "type": "object",
        "properties": {
            "critical_divergence_points": {"type": "array", "items": divergence_item},
            "missing_tool_calls": {"type": "array", "items": {"type": "string"}},
            "incorrect_tool_calls": {"type": "array", "items": {"type": "string"}},
            "parameter_mismatches": {"type": "array", "items": mismatch},
        },
        "required": [
            "critical_divergence_points",
            "missing_tool_calls",
            "incorrect_tool_calls",
            "parameter_mismatches",
        ],
    }


def get_root_cause_analysis_schema():
    """Define the response schema for root cause analysis"""
    return {
        "type": "object",
        "properties": {
            "reasoning_breakdown": {"type": "string"},
            "causal_chain": {
                "type": "array",
                "items": {"type": "string"}
            },
            "missed_claims_mapping": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "claim": {"type": "string"},
                        "root_cause": {"type": "string"},
                        "fix_required": {"type": "string"}
                    },
                    "required": ["claim", "root_cause", "fix_required"]
                }
            }
        },
        "required": ["reasoning_breakdown", "causal_chain", "missed_claims_mapping"]
    }


# =========================================================================
# 3. LITELLM CLIENT (Unified Interface for All Providers)
# =========================================================================

class AsyncLLMClient(ABC):
    """Abstract base class for LLM clients"""
    
    @abstractmethod
    async def generate_structured_content(self, prompt: str, response_schema: Dict, temperature: float = 0.0) -> Dict:
        """Generate structured content with retry logic."""
        pass
    
    @abstractmethod
    def get_stats(self) -> Dict[str, int]:
        """Get request statistics"""
        pass


class AsyncLiteLLMClient(AsyncLLMClient):
    """Manages async LiteLLM requests with rate limiting - supports all providers via LiteLLM"""
    
    def __init__(self, config: EvaluatorConfig):
        self.config = config
        self.semaphore = asyncio.Semaphore(config.semaphore_limit)
        self.logger = logging.getLogger(__name__)
        self.request_count = 0
        self.error_count = 0
        
        # Initialize LiteLLM configuration from environment
        api_key, api_base = get_litellm_config()
        litellm.api_key = api_key
        if api_base:
            litellm.api_base = api_base
    
    @retry(
        wait=wait_random_exponential(min=1, max=60),
        stop=stop_after_attempt(6),
        reraise=True
    )
    async def generate_structured_content(self, prompt: str, response_schema: Dict, temperature: float = 0.0) -> Dict:
        """Generate structured content using LiteLLM with retry logic."""
        async with self.semaphore:
            try:
                self.request_count += 1
                
                # LiteLLM uses OpenAI-compatible format
                # Request JSON mode (supported by most providers)
                response = await litellm.acompletion(
                    model=self.config.model_name,
                    messages=[
                        {"role": "system", "content": "You are a helpful assistant that returns JSON responses matching the requested schema. Always respond with valid JSON only."},
                        {"role": "user", "content": prompt}
                    ],
                    response_format={"type": "json_object"},
                    temperature=1 if self.config.model_name == "gpt-5" else temperature, # gpt-5 only supports temperature=1
                    api_key=litellm.api_key,
                    api_base=litellm.api_base if hasattr(litellm, 'api_base') and litellm.api_base else None,
                )
                
                # Rate limiting delay
                await asyncio.sleep(self.config.request_delay)
                
                # Parse JSON response
                content = response.choices[0].message.content
                return json.loads(content)
                
            except Exception as e:
                self.error_count += 1
                self.logger.error(f"LiteLLM API error: {e}")
                raise
    
    def get_stats(self) -> Dict[str, int]:
        """Get request statistics"""
        return {
            "total_requests": self.request_count,
            "errors": self.error_count,
            "success_rate": (self.request_count - self.error_count) / max(self.request_count, 1)
        }




# =========================================================================
# 4. COVERAGE EVALUATOR
# =========================================================================

class CoverageEvaluator:
    """Evaluates claim coverage with continuous scoring (0-1) - one claim at a time."""
    def __init__(self, client: AsyncLLMClient, config: EvaluatorConfig):
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
            self.logger.warning(f"Single claim evaluation failed: {e}")
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

    def _create_fallback_result(self, claims: List[str], response: str, error_msg: str) -> Dict[str, Any]:
        """Simple heuristic fallback"""
        return {
            "per_claim": [{"claim": c, "covered": False, "score": 0.0, "reason": "Fallback due to error"} for c in claims],
            "coverage_score": 0.0,
            "total_claims": len(claims),
            "fully_covered_claims": 0,
            "partially_covered_claims": 0,
            "explanation": f"Fallback evaluation used: {error_msg}",
            "confidence": 0.1
        }

async def evaluate_dataframe_async(df: pd.DataFrame, evaluator: CoverageEvaluator) -> pd.DataFrame:
    """Asynchronously evaluates all rows in a dataframe."""
    logger = logging.getLogger(__name__)
    
    async def safe_evaluate(row_idx, row):
        try:
            claims = extract_claims(row.get("GTFA_CLAIMS", ""))
            # Determine the correct response column
            response_col = next((col for col in [f"{evaluator.config.model_name}_response", "script_model_response", "response"] if col in row and pd.notna(row[col])), None)
            response = row.get(response_col, "") if response_col else ""
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

# =========================================================================
# 3. STATISTICAL ANALYSIS AND PLOTTING
# =========================================================================

def generate_statistics_and_plots(scored_csv_path: str, model_name: str, output_dir: str):
    """Generates a summary stats CSV and a histogram plot of coverage scores."""
    logger = logging.getLogger(__name__)
    logger.info(f"Step 4: Generating statistics and plots for '{scored_csv_path}'...")
    
    try:
        df = pd.read_csv(scored_csv_path)
        if "coverage_score" not in df.columns:
            raise KeyError("'coverage_score' column missing.")

        # --- Generate and save statistics ---
        stats_df = df["coverage_score"].describe().to_frame(name="value").reset_index().rename(columns={"index": "stat"})
        
        # Rename "mean" to "mean coverage score"
        stats_df.loc[stats_df["stat"] == "mean", "stat"] = "mean coverage score"
        
        # Calculate pass rate (% of tasks where coverage_score >= 0.75)
        valid_scores = df["coverage_score"].dropna()
        pass_count = (valid_scores >= 0.75).sum()
        total_count = len(valid_scores)
        pass_rate = pass_count / total_count if total_count > 0 else 0.0
        
        # Insert pass rate row right after "mean coverage score"
        mean_idx = stats_df[stats_df["stat"] == "mean coverage score"].index[0]
        pass_rate_row = pd.DataFrame({"stat": ["pass rate"], "value": [pass_rate]})
        stats_df = pd.concat([
            stats_df.iloc[:mean_idx + 1],
            pass_rate_row,
            stats_df.iloc[mean_idx + 1:]
        ]).reset_index(drop=True)
        
        stats_path = os.path.join(output_dir, f"coverage_stats_{model_name}.csv")
        stats_df.to_csv(stats_path, index=False)
        logger.info(f"Saved summary statistics to '{stats_path}'")
        print("\nCoverage Score Summary:")
        print(stats_df)

        # --- Generate and save histogram plot ---
        scores = df["coverage_score"].dropna().to_numpy()
        
        # Only create plot if we have data
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
# 4. FAILURE ANALYSIS FRAMEWORK - GEMINI VERSION (SPLIT INTO 3 CALLS)
# =========================================================================

class LLMTrajectoryComparator:
    """Uses an LLM to compare trajectories and identify failure modes - split into 3 separate calls."""
    def __init__(self, config: EvaluatorConfig):
        self.config = config
        self.client = AsyncLiteLLMClient(config)
        self.logger = logging.getLogger(__name__)

    def _get_diagnosis_prompt(self, 
                             task_id: str,
                             prompt: str,
                             expected_trajectory: str,
                             actual_trajectory: str,
                             model_response: str,
                             gtfa_claims: str,
                             coverage_score: float,
                             missed_claims: List[Dict]) -> str:
        return f"""Diagnose the primary failure mode by comparing expected vs actual execution.

FAILURE CATEGORY DEFINITIONS:

TOOL EXECUTION FAILURES:
- no_tools_called: Model completely failed to invoke any tools despite them being required for the task
- missing_tool_calls: Model called some tools but omitted critical ones needed for task completion
- wrong_tool_selection: Model selected inappropriate or incorrect tools for the given task requirements
- incorrect_tool_parameters: Model used the right tool but with malformed, missing, or incorrect parameters
- tool_execution_order: Model called tools in wrong sequence, violating dependencies or logical flow
- redundant_tool_calls: Model made unnecessary repeated calls to the same tool, causing inefficiency

REASONING FAILURES:
- misunderstood_task: Model fundamentally misinterpreted the task requirements or user intent
- partial_task_completion: Model understood the task but only completed a subset of requirements
- skipped_steps: Model omitted critical intermediate operations needed for correct execution
- incorrect_conclusion: Model followed correct process but arrived at wrong final answer or output

SYSTEM FAILURES:
- auth_failure: Authentication or authorization errors prevented successful tool execution
- capability_discovery_missing: Model failed to discover or identify available tools and capabilities
- env_constraint_violation: Model exceeded rate limits, token limits, or other environmental constraints
- rate_limit_handling: Model failed to properly handle API throttling or rate limiting responses

CONTEXT:
Task: {task_id}
Coverage Score: {coverage_score:.2%}

Number of Missed Claims: {len(missed_claims)}

TRAJECTORIES TO COMPARE:
Expected: {expected_trajectory[:500]}...
Actual: {actual_trajectory[:500]}...

Identify the single most critical failure mode that best explains why the model failed to achieve full coverage."""

    def _get_trajectory_analysis_prompt(self,
                                       expected_trajectory: str,
                                       actual_trajectory: str,
                                       missed_claims: List[Dict]) -> str:
        return f"""Perform detailed trajectory analysis comparing expected vs actual execution paths.

EXPECTED TRAJECTORY:
{expected_trajectory}

ACTUAL TRAJECTORY:
{actual_trajectory}

MISSED CLAIMS CONTEXT:
Total missed: {len(missed_claims)}
Examples: {json.dumps(missed_claims[:3], indent=2) if missed_claims else "None"}

ANALYSIS TASKS:
1. Identify critical divergence points where trajectories differ
2. For each divergence, specify the step number, expected vs actual actions
3. Assess the severity (critical/high/medium/low) based on impact to task completion
4. List specific missing tool calls that should have been made
5. List incorrect tool calls that were made but shouldn't have been
6. Identify parameter mismatches where the right tool was called with wrong arguments

Focus on technical differences in execution paths, not interpretation."""

    def _get_root_cause_prompt(self,
                              task: str,
                              prompt: str,
                              model_response: str,
                              gtfa_claims: str,
                              missed_claims: List[Dict],
                              diagnosis: Dict,
                              trajectory_analysis: Dict) -> str:
        return f"""Analyze the root cause of failures and map them to specific missed claims.

TASK CONTEXT:
Task: {task}
Original Prompt: {prompt}

EXPECTED CLAIMS:
{gtfa_claims}

MISSED CLAIMS:
{json.dumps(missed_claims, indent=2)}

DIAGNOSIS RESULT:
Primary Failure: {diagnosis.get('primary_failure_mode', 'unknown')}

TRAJECTORY ANALYSIS SUMMARY:
Missing Tools: {trajectory_analysis.get('missing_tool_calls', [])}
Wrong Tools: {trajectory_analysis.get('incorrect_tool_calls', [])}
Critical Divergences: {len(trajectory_analysis.get('critical_divergence_points', []))} found

ROOT CAUSE ANALYSIS TASKS:
1. Explain the reasoning breakdown - why did the model fail in its approach?
2. Trace the causal chain from initial error to final impact (step-by-step progression)
3. For each missed claim, identify the specific root cause and what fix would address it

Be specific about how trajectory failures led to missed claims."""

    async def get_diagnosis(self,
                           expected_traj: str,
                           actual_traj: str,
                           task: str,
                           prompt: str,
                           model_response: str,
                           gtfa_claims: str,
                           coverage_score: float,
                           missed_claims: List[Dict]) -> Dict:
        """Get diagnosis of primary failure mode"""
        prompt_text = self._get_diagnosis_prompt(
            task, prompt, expected_traj, actual_traj,
            model_response, gtfa_claims, coverage_score, missed_claims
        )
        
        try:
            result = await self.client.generate_structured_content(
                prompt=prompt_text,
                response_schema=get_diagnosis_schema(),
                temperature=0.0
            )
            return result
        except Exception as e:
            self.logger.error(f"Diagnosis failed: {e}")
            return {
                "primary_failure_mode": "analysis_error",
                "secondary_failures": [],
                "confidence_score": 0.1,
                "brief_explanation": f"Diagnosis failed: {e}"
            }

    async def get_trajectory_analysis(self,
                                     expected_traj: str,
                                     actual_traj: str,
                                     missed_claims: List[Dict]) -> Dict:
        """Get detailed trajectory analysis"""
        prompt_text = self._get_trajectory_analysis_prompt(
            expected_traj, actual_traj, missed_claims
        )
        
        try:
            result = await self.client.generate_structured_content(
                prompt=prompt_text,
                response_schema=get_trajectory_analysis_schema(),
                temperature=0.0
            )
            return result
        except Exception as e:
            self.logger.error(f"Trajectory analysis failed: {e}")
            return {
                "critical_divergence_points": [],
                "missing_tool_calls": [],
                "incorrect_tool_calls": [],
                "parameter_mismatches": []
            }

    async def get_root_cause_analysis(self,
                                     task: str,
                                     prompt: str,
                                     model_response: str,
                                     gtfa_claims: str,
                                     missed_claims: List[Dict],
                                     diagnosis: Dict,
                                     trajectory_analysis: Dict) -> Dict:
        """Get root cause analysis"""
        prompt_text = self._get_root_cause_prompt(
            task, prompt, model_response, gtfa_claims,
            missed_claims, diagnosis, trajectory_analysis
        )
        
        try:
            result = await self.client.generate_structured_content(
                prompt=prompt_text,
                response_schema=get_root_cause_analysis_schema(),
                temperature=0.0
            )
            return result
        except Exception as e:
            self.logger.error(f"Root cause analysis failed: {e}")
            return {
                "reasoning_breakdown": f"Analysis failed: {e}",
                "causal_chain": [],
                "missed_claims_mapping": []
            }

    async def compare_trajectories(self, 
                                   expected_traj: str, 
                                   actual_traj: str, 
                                   task: str, 
                                   prompt: str,
                                   model_response: str,
                                   gtfa_claims: str,
                                   coverage_details: Dict,
                                   coverage_score: float) -> Dict:
        """
        Compare trajectories with 3 separate API calls for diagnosis, trajectory analysis, and root cause.
        """
        
        # Extract missed claims from coverage details
        missed_claims = []
        if coverage_details and isinstance(coverage_details, dict):
            per_claim = coverage_details.get("per_claim", [])
            for claim_detail in per_claim:
                if claim_detail.get("score", 0) < 0.7:
                    missed_claims.append({
                        "claim": claim_detail.get("claim", ""),
                        "justification": claim_detail.get("reason", "Not adequately covered")
                    })
        
        # Make 3 separate API calls
        diagnosis = await self.get_diagnosis(
            expected_traj, actual_traj, task, prompt,
            model_response, gtfa_claims, coverage_score, missed_claims
        )
        
        trajectory_analysis = await self.get_trajectory_analysis(
            expected_traj, actual_traj, missed_claims
        )
        
        root_cause = await self.get_root_cause_analysis(
            task, prompt, model_response, gtfa_claims,
            missed_claims, diagnosis, trajectory_analysis
        )
        
        # Combine results into expected format
        return {
            "primary_failure": diagnosis.get("primary_failure_mode", "analysis_error"),
            "all_failures": diagnosis.get("secondary_failures", []),
            "confidence": diagnosis.get("confidence_score", 0.5),
            "specific_issues": [
                {
                    "issue": f"Divergence at step {div['step_number']}",
                    "where": f"Step {div['step_number']}",
                    "severity": div["severity"]
                }
                for div in trajectory_analysis.get("critical_divergence_points", [])
            ],
            "tool_param_diffs": [
                {
                    "step": f"Step {div['step_number']}",
                    "expected": div["expected_action"],
                    "actual": div["actual_action"]
                }
                for div in trajectory_analysis.get("critical_divergence_points", [])
            ],
            "missing_tools": trajectory_analysis.get("missing_tool_calls", []),
            "wrong_tools": trajectory_analysis.get("incorrect_tool_calls", []),
            "reasoning_explanation": root_cause.get("reasoning_breakdown", "Analysis incomplete")
        }

class EnhancedLLMFailureAnalyzer:
    """Orchestrates the LLM-based failure analysis."""
    def __init__(self, df: pd.DataFrame, config: EvaluatorConfig, model_name: str, coverage_threshold: float = 0.7):
        self.df = df.copy()
        self.config = config
        self.model_name = model_name
        self.coverage_threshold = coverage_threshold
        self.comparator = LLMTrajectoryComparator(config)
        self.logger = logging.getLogger(__name__)

    async def analyze(self) -> pd.DataFrame:
        low_coverage_mask = (self.df['coverage_score'] < self.coverage_threshold) & (self.df['coverage_score'].notna())
        rows_to_analyze = self.df[low_coverage_mask]

        if rows_to_analyze.empty:
            self.logger.info("No low-coverage rows to analyze.")
            return self.df

        self.logger.info(f"Analyzing {len(rows_to_analyze)} rows with coverage < {self.coverage_threshold}...")

        async def analyze_row(idx, row):
            actual_traj_col = f"{self.model_name}_trajectory"
            
            # Parse coverage details from JSON if it's a string
            coverage_details = {}
            if 'coverage_details_json' in row and pd.notna(row['coverage_details_json']):
                try:
                    coverage_details = json.loads(row['coverage_details_json'])
                except json.JSONDecodeError:
                    coverage_details = {}
            
            # Get model response column
            response_col = next((col for col in [f"{self.model_name}_response", "script_model_response", "response"] 
                                if col in row and pd.notna(row[col])), None)
            model_response = row.get(response_col, "") if response_col else ""
            
            comparison = await self.comparator.compare_trajectories(
                expected_traj=str(row.get('TRAJECTORY', '')),
                actual_traj=str(row.get(actual_traj_col, '')),
                task=str(row.get('TASK', '')),
                prompt=str(row.get('PROMPT', '')),
                model_response=model_response,
                gtfa_claims=str(row.get('GTFA_CLAIMS', '')),
                coverage_details=coverage_details,
                coverage_score=row.get('coverage_score', 0)
            )
            return idx, comparison

        tasks = [analyze_row(idx, row) for idx, row in rows_to_analyze.iterrows()]
        results = [await f for f in tqdm(asyncio.as_completed(tasks), total=len(tasks), desc="Analyzing Failures")]

        # Add new columns for all the analysis results
        self.df['llm_primary_failure'] = pd.NA
        self.df['llm_all_failures'] = pd.NA
        self.df['llm_confidence'] = pd.NA
        self.df['llm_reasoning'] = pd.NA
        self.df['llm_specific_issues'] = pd.NA
        self.df['llm_tool_param_diffs'] = pd.NA
        self.df['llm_missing_tools'] = pd.NA
        self.df['llm_wrong_tools'] = pd.NA

        for idx, comp_res in results:
            self.df.loc[idx, 'llm_primary_failure'] = comp_res.get('primary_failure')
            self.df.loc[idx, 'llm_all_failures'] = json.dumps(comp_res.get('all_failures', []))
            self.df.loc[idx, 'llm_confidence'] = comp_res.get('confidence')
            self.df.loc[idx, 'llm_reasoning'] = comp_res.get('reasoning_explanation')
            self.df.loc[idx, 'llm_specific_issues'] = json.dumps(comp_res.get('specific_issues', []))
            self.df.loc[idx, 'llm_tool_param_diffs'] = json.dumps(comp_res.get('tool_param_diffs', []))
            self.df.loc[idx, 'llm_missing_tools'] = json.dumps(comp_res.get('missing_tools', []))
            self.df.loc[idx, 'llm_wrong_tools'] = json.dumps(comp_res.get('wrong_tools', []))
            
        return self.df


# =========================================================================
# 4. MAIN EXECUTION
# =========================================================================

async def main(args):
    """Main function to run the entire pipeline."""
    setup_logging()
    logger = logging.getLogger(__name__)
    
    # Log if running on limited tasks
    if args.num_tasks:
        logger.info(f"🔬 Running evaluation on first {args.num_tasks} tasks only")
    
    # Create output directory if it doesn't exist
    output_dir = args.output_dir
    os.makedirs(output_dir, exist_ok=True)
    
    # Define file path for scored results
    scored_path = os.path.join(output_dir, f"scored_{args.model_name}.csv")

    try:
        # --- Create Evaluator Configuration ---
        logger.info(f"Using evaluator model: {args.evaluator_model}")
        config = EvaluatorConfig(
            model_name=args.evaluator_model,
            semaphore_limit=args.concurrency,
            strict_evaluation=True,
            num_tasks=args.num_tasks
        )

        # --- Pipeline Execution ---
        # 1. Load input file (already contains both ground truth and completion data)
        logger.info(f"Loading input file: {args.input_file}")
        df_input = pd.read_csv(args.input_file)
        
        # Apply task limit if specified
        if args.num_tasks is not None and args.num_tasks > 0:
            original_size = len(df_input)
            df_input = df_input.head(args.num_tasks)
            logger.info(f"Limited dataset from {original_size} to {len(df_input)} tasks")
        
        # Verify required columns exist
        required_cols = ['TASK', 'PROMPT', 'TRAJECTORY', 'GTFA_CLAIMS']
        missing_cols = [col for col in required_cols if col not in df_input.columns]
        if missing_cols:
            logger.error(f"Missing required columns in {args.input_file}: {missing_cols}")
            raise KeyError(f"Missing required columns: {missing_cols}")
        
        logger.info(f"Successfully loaded {len(df_input)} tasks")

        # 2. Run scoring evaluation
        client = AsyncLiteLLMClient(config)
        evaluator = CoverageEvaluator(client, config)
        df_scored = await evaluate_dataframe_async(df_input, evaluator)
        df_scored.to_csv(scored_path, index=False)
        
        logger.info(f"✅ Saved scored file to '{scored_path}'")
        valid_scores = df_scored["coverage_score"].dropna()
        logger.info(f"Evaluation complete. Average coverage: {valid_scores.mean():.3f}")

        # 3. Generate statistics and plots
        generate_statistics_and_plots(scored_path, args.model_name, output_dir)

        logger.info(f"\n🚀 Pipeline finished successfully!")
        logger.info(f"Results available in: {output_dir}")
        
        if args.num_tasks:
            logger.info(f"📊 Note: Results are based on {args.num_tasks} tasks only")

    except (FileNotFoundError, KeyError) as e:
        logger.error(f"Pipeline stopped due to an error: {e}")
    except Exception as e:
        logger.error(f"An unexpected error occurred: {e}", exc_info=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run model evaluation pipeline with coverage scoring.")
    
    parser.add_argument("--input-file", type=str, required=True, 
                       help="Path to the completion results CSV file containing both ground truth and model outputs.")
    parser.add_argument("--model-name", type=str, required=True, 
                       help="Short name for the model being evaluated (e.g., 'gpt4o'). Used for naming output files.")
    parser.add_argument("--evaluator-model", type=str, 
                       default=os.getenv("EVAL_LLM_MODEL", "gemini/gemini-2.5-pro"),
                       help="Model name in LiteLLM format. Default: EVAL_LLM_MODEL env var or 'gemini/gemini-2.5-pro'")
    parser.add_argument("--output-dir", type=str, default="evaluation_results", 
                       help="Directory to save all output files.")
    parser.add_argument("--concurrency", type=int, default=5, 
                       help="Number of concurrent requests to the LLM API.")
    parser.add_argument("--num-tasks", type=int, default=None, 
                       help="Limit evaluation to first N tasks (useful for testing). If not specified, processes all tasks.")
    
    args = parser.parse_args()
    
    # Run the main async function
    asyncio.run(main(args))