#!/usr/bin/env python3
"""
Error Analysis Tool for MCP Evaluation Results

Analyzes error distribution and generates statistics from evaluation CSV outputs.

Usage:
    python analyze_errors.py <output_csv_file>
"""

import pandas as pd
import sys
import json
from collections import defaultdict
from datetime import datetime


def analyze_error_distribution(csv_path):
    """
    Analyze error distribution from evaluation results CSV.

    Args:
        csv_path: Path to the evaluation output CSV file

    Returns:
        dict: Statistics including error counts, types, and distribution
    """
    try:
        df = pd.read_csv(csv_path)
    except Exception as e:
        print(f"❌ Error reading CSV file: {e}")
        sys.exit(1)

    total_tasks = len(df)

    # Count tasks with responses
    has_script_response = df['script_model_response'].notna().sum() if 'script_model_response' in df.columns else 0

    # Analyze errors
    error_distribution = defaultdict(int)
    tasks_with_errors = 0
    error_details = []

    for idx, row in df.iterrows():
        task_id = row.get('task_id', f'task_{idx}')
        errors = row.get('errors', '[]')

        if errors and errors != '[]':
            try:
                # Parse errors (handle both string and actual list)
                error_list = eval(errors) if isinstance(errors, str) else errors

                if error_list:
                    tasks_with_errors += 1

                    for err in error_list:
                        if isinstance(err, dict):
                            msg = err.get('message', 'Unknown error')

                            # Categorize errors
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
                            else:
                                # Keep first 60 chars of unique errors
                                error_type = msg[:60]

                            error_distribution[error_type] += 1
                            error_details.append({
                                'task_id': task_id,
                                'error_type': error_type,
                                'full_message': msg
                            })
            except Exception as e:
                # Skip unparseable errors
                pass

    return {
        'total_tasks': total_tasks,
        'has_script_response': has_script_response,
        'tasks_with_errors': tasks_with_errors,
        'error_distribution': dict(error_distribution),
        'error_details': error_details
    }


def print_analysis_report(stats, csv_path, logger=None):
    """Print formatted analysis report to console and optionally to logger.

    Args:
        stats: Error statistics dictionary
        csv_path: Path to the CSV file
        logger: Optional logging.Logger instance to also write to log file
    """
    def output(msg):
        """Output to both console and logger if provided"""
        print(msg)
        if logger:
            logger.info(msg)

    total = stats['total_tasks']
    successful = stats['has_script_response']
    failed = total - successful
    success_rate = (successful / total * 100) if total > 0 else 0

    output(f"\n{'='*80}")
    output(f"  MCP EVALUATION ERROR ANALYSIS")
    output(f"{'='*80}")
    output(f"  File:               {csv_path}")
    output(f"  Analysis Time:      {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    output(f"{'='*80}")
    output(f"  Total Tasks:        {total}")
    output(f"  Successful:         {successful} ({success_rate:.1f}%)")
    output(f"  Failed:             {failed} ({100-success_rate:.1f}%)")
    output(f"  Tasks with Errors:  {stats['tasks_with_errors']}")
    output(f"{'='*80}\n")

    # Error distribution
    if stats['error_distribution']:
        output(f"  ERROR DISTRIBUTION:")
        output(f"  {'-'*76}")

        # Sort by count descending
        sorted_errors = sorted(
            stats['error_distribution'].items(),
            key=lambda x: x[1],
            reverse=True
        )

        for error_type, count in sorted_errors:
            pct = (count / stats['tasks_with_errors'] * 100) if stats['tasks_with_errors'] > 0 else 0
            output(f"  {error_type[:60]:<60} {count:>4} ({pct:>5.1f}%)")

        output(f"  {'-'*76}\n")
    else:
        output("  ✅ No errors found!\n")

    output(f"{'='*80}\n")


def main():
    if len(sys.argv) < 2:
        print("Usage: python analyze_errors.py <output_csv_file>")
        sys.exit(1)

    csv_path = sys.argv[1]

    print(f"🔍 Analyzing errors from: {csv_path}")

    stats = analyze_error_distribution(csv_path)
    print_analysis_report(stats, csv_path)

    # Optionally save detailed report
    if '--save-json' in sys.argv:
        json_path = csv_path.replace('.csv', '_error_analysis.json')
        with open(json_path, 'w') as f:
            json.dump(stats, f, indent=2)
        print(f"💾 Detailed report saved to: {json_path}")


if __name__ == '__main__':
    main()
