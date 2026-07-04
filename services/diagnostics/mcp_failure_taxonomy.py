"""MCP Failure Taxonomy — Single source of truth for failure mode definitions.

Two families:
- Tool Call (4): how the model interacted with tools
- Cognitive (7): how the model reasoned about the task

11 modes total. Imported by single_model_diagnostic.py and reporting tools.
"""

# ---------------------------------------------------------------------------
# Taxonomy definition
# ---------------------------------------------------------------------------

TOOL_CALL_MODES = {
    "malformed_call": {
        "description": "Right tool, wrong parameters: missing arguments, bad types, or wrong values.",
        "example": "Called mongodb_find with column name 'revenue' when the schema field is 'Revenue_USD'.",
    },
    "wrong_tool": {
        "description": "Picked a tool that cannot answer the subtask, even though a correct tool was available.",
        "example": "Used wikipedia_search for a fact that lives only in the Airtable database.",
    },
    "no_tool_use": {
        "description": "Answered from internal knowledge without calling any tool, even though tools were required.",
        "example": "Reported a historical date directly without querying any source.",
    },
    "err_recovery": {
        "description": "A tool returned an error and the model could not adapt: it retried identically, looped, or gave up.",
        "example": "Retried the same call five times instead of backing off or trying a different server.",
    },
}

COGNITIVE_MODES = {
    "task_misunderstanding": {
        "description": "Answered a different question than the one asked, or missed a key requirement in the prompt.",
        "example": "Prompt asked for 'average revenue in December'; the model returned total revenue for the full year.",
    },
    "faulty_synthesis": {
        "description": "Had the right tool outputs but combined or interpreted them incorrectly. Not a logic error.",
        "example": "Queried the right table, got the right rows, then averaged the wrong column in the final answer.",
    },
    "response_misparsing": {
        "description": "Got valid tool output but misread its structure or extracted the wrong field.",
        "example": "Tool returned a list of 10 records but the model picked the wrong row or read the wrong field.",
    },
    "early_termination": {
        "description": "Understood the task but stopped before completing all the required steps.",
        "example": "Found one half of a two-part answer and produced a final answer without addressing the other half.",
    },
    "hallucinated_fact": {
        "description": "Stated something in the final answer that did not appear in any tool output.",
        "example": "Tool returned a population of 45,000 but the model wrote 54,000 in its answer.",
    },
    "logical_error": {
        "description": "Multi-step reasoning chain was flawed even though the underlying data were correct.",
        "example": "Correctly retrieved the date and database records, then applied the wrong conditional to filter them.",
    },
    "constraint_violation": {
        "description": "Ignored an explicit condition or filter stated in the prompt.",
        "example": "Prompt said 'only premium units built in 2017' but the model queried all units across all years.",
    },
}

# ---------------------------------------------------------------------------
# Derived constants (used by the diagnosis pipeline)
# ---------------------------------------------------------------------------

FAILURE_TAXONOMY = {
    "tool_call": TOOL_CALL_MODES,
    "cognitive": COGNITIVE_MODES,
}

# Flat list of all mode names (for schema enum validation)
ALL_MODES = list(TOOL_CALL_MODES.keys()) + list(COGNITIVE_MODES.keys())

# Mode -> category lookup. Authoritative — used to derive category_split
# from the primary mode, ignoring any category field the judge fills in.
MODE_TO_CATEGORY = {}
for mode in TOOL_CALL_MODES:
    MODE_TO_CATEGORY[mode] = "tool_call"
for mode in COGNITIVE_MODES:
    MODE_TO_CATEGORY[mode] = "cognitive"

# Programmatic labels assigned without LLM calls (e.g. unparseable trajectories).
# Not part of the judge's mode enum.
PROGRAMMATIC_LABELS = ["analysis_error"]


def get_taxonomy_prompt_text() -> str:
    """Generate the failure mode definitions section for the judge prompt, with examples."""
    lines = []
    lines.append("TOOL CALL FAMILY — Problems with how the model interacted with tools:")
    for mode, info in TOOL_CALL_MODES.items():
        lines.append(f"- {mode}: {info['description']} (e.g., {info['example']})")
    lines.append("")
    lines.append("COGNITIVE FAMILY — Problems with how the model reasoned about the task:")
    for mode, info in COGNITIVE_MODES.items():
        lines.append(f"- {mode}: {info['description']} (e.g., {info['example']})")
    return "\n".join(lines)


def get_diagnosis_schema() -> dict:
    """JSON schema for the structured diagnosis output."""
    failure_entry_schema = {
        "type": "object",
        "properties": {
            "mode": {"type": "string", "enum": ALL_MODES},
            "category": {"type": "string", "enum": ["tool_call", "cognitive"]},
            "is_root_cause": {"type": "boolean"},
            "explanation": {"type": "string"},
        },
        "required": ["mode", "category", "is_root_cause", "explanation"],
    }

    return {
        "type": "object",
        "properties": {
            "primary_failure": {
                "type": "object",
                "properties": {
                    "mode": {"type": "string", "enum": ALL_MODES},
                    "category": {"type": "string", "enum": ["tool_call", "cognitive"]},
                    "explanation": {"type": "string"},
                },
                "required": ["mode", "category", "explanation"],
            },
            "all_failures": {
                "type": "array",
                "items": failure_entry_schema,
            },
            "confidence": {"type": "number"},
            "summary": {"type": "string"},
        },
        "required": ["primary_failure", "all_failures", "confidence", "summary"],
    }
