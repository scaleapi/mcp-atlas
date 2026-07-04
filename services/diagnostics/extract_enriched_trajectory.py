"""Extract enriched trajectory from raw_conversation_history in scored CSVs.

Reads existing scored CSVs and backfills the model_trajectory column with the
enriched format (turn structure, tool calls with status/errors/output_summary,
parallel detection, assistant reasoning, final answer).

Usage:
    python extract_enriched_trajectory.py --input scored_model.csv --output scored_model_enriched.csv
    python extract_enriched_trajectory.py --input scored_model.csv  # overwrites in place
"""

import csv
import sys
import json
import argparse
import logging

csv.field_size_limit(sys.maxsize)

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


def parse_conversation(conv_str: str) -> list:
    """Parse raw_conversation_history handling double-escaped JSON from CSV."""
    if not conv_str or conv_str.strip() in ('', 'nan'):
        return []
    try:
        return json.loads(conv_str)
    except json.JSONDecodeError:
        try:
            fixed = conv_str.replace('\\\\n', '\\n').replace('\\\\"', '\\"')
            return json.loads(fixed)
        except:
            return []


def build_enriched_trajectory(messages: list) -> list:
    """Build enriched trajectory from clean conversation messages.

    Returns list of turn dicts with: turn number, assistant reasoning,
    tool calls (name, params, status, error_message, output_summary),
    parallel flag, and final answer.
    """
    turns = []
    turn_num = 0
    i = 0

    while i < len(messages):
        msg = messages[i]
        if not isinstance(msg, dict):
            i += 1
            continue

        if msg.get('role') == 'assistant':
            turn_num += 1
            tool_calls_in_turn = msg.get('tool_calls') or []
            reasoning = msg.get('content')

            if not tool_calls_in_turn:
                turns.append({
                    "turn": turn_num,
                    "assistant_reasoning": reasoning,
                    "final_answer": reasoning,
                    "tool_calls": [],
                })
                i += 1
                continue

            tc_entries = []
            tc_id_map = {}
            for tc in tool_calls_in_turn:
                func = tc.get('function', {})
                args_str = func.get('arguments', '{}')
                try:
                    params = json.loads(args_str) if isinstance(args_str, str) else args_str
                except (json.JSONDecodeError, TypeError):
                    params = {"_raw": str(args_str)[:200]}

                entry = {
                    "id": tc.get('id'),
                    "name": func.get('name', 'unknown'),
                    "parameters": params,
                    "status": "pending",
                    "error_message": None,
                    "output_summary": None,
                }
                tc_entries.append(entry)
                tc_id_map[tc.get('id')] = entry

            i += 1
            while i < len(messages) and isinstance(messages[i], dict) and messages[i].get('role') == 'tool':
                tool_msg = messages[i]
                tool_call_id = tool_msg.get('tool_call_id')
                content = tool_msg.get('content', '')

                if isinstance(content, list):
                    text = ' '.join(c.get('text', '') for c in content if isinstance(c, dict))
                else:
                    text = str(content)

                is_error = text.startswith('Error:') or text.startswith('ERROR:') or 'error' in text[:80].lower()

                if tool_call_id in tc_id_map:
                    tc_id_map[tool_call_id]['status'] = 'error' if is_error else 'success'
                    if is_error:
                        tc_id_map[tool_call_id]['error_message'] = text[:500]
                    tc_id_map[tool_call_id]['output_summary'] = text[:500] if text else None
                i += 1

            for entry in tc_entries:
                if entry['status'] == 'pending':
                    entry['status'] = 'no_response'
                entry.pop('id', None)

            turns.append({
                "turn": turn_num,
                "assistant_reasoning": reasoning,
                "tool_calls": tc_entries,
                "parallel": len(tc_entries) > 1,
            })
        else:
            i += 1

    return turns


def main():
    parser = argparse.ArgumentParser(description="Extract enriched trajectory from scored CSVs.")
    parser.add_argument("--input", required=True, help="Path to scored CSV with raw_conversation_history")
    parser.add_argument("--output", default=None, help="Output path (default: overwrite input)")
    parser.add_argument("--column", default="model_trajectory", help="Column name to write enriched trajectory to (default: model_trajectory)")
    args = parser.parse_args()

    output_path = args.output or args.input

    logger.info(f"Reading: {args.input}")
    rows = []
    with open(args.input, 'r') as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames
        for row in reader:
            rows.append(row)
    logger.info(f"Loaded {len(rows)} rows")

    # Add column if not present
    if args.column not in fieldnames:
        fieldnames = list(fieldnames) + [args.column]

    enriched_count = 0
    skipped_count = 0
    for i, row in enumerate(rows):
        conv = row.get('raw_conversation_history', '')
        messages = parse_conversation(conv)
        if messages:
            turns = build_enriched_trajectory(messages)
            row[args.column] = json.dumps(turns)
            enriched_count += 1
        else:
            row[args.column] = '[]'
            skipped_count += 1

        if (i + 1) % 100 == 0:
            logger.info(f"Processed {i + 1}/{len(rows)} rows")

    logger.info(f"Enriched: {enriched_count}, Skipped (no conversation): {skipped_count}")
    logger.info(f"Writing: {output_path}")

    with open(output_path, 'w', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    logger.info("Done.")


if __name__ == "__main__":
    main()
