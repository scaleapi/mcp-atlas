#!/usr/bin/env python3
"""Validate Harbor task bundles before delivery (deterministic; no Docker/LLM).

Checks each bundle directory — the output of ``convert_tasks_to_harbor.py``, or
any Harbor task bundle — against the structural and verifier contracts Harbor
relies on, so format regressions are caught here instead of at run time (or by a
customer). It is stdlib-only so it runs anywhere, including CI.

Per bundle (``<task_id>/``):
  * required files are present
  * ``task.toml`` parses and has the required keys (needs Python 3.11+ ``tomllib``
    or ``tomli``; otherwise the TOML-structural checks are skipped with a note)
  * ``environment/enabled_tools.txt`` matches ``task.toml``'s ``enabled_tools``
  * ``tests/test.sh`` and ``solution/solve.sh`` are executable and start with a
    ``#!`` shebang
  * ``tests/agent_judge.py``:
      - compiles
      - its module body imports/execs cleanly — catches the JSON-literal
        ``NameError`` class (e.g. ``CRITERIA``/``OUTPUT_SCHEMA`` embedded via
        ``json.dumps`` so ``true``/``false``/``null`` become undefined names)
      - defines a non-empty list ``CRITERIA``, a dict ``OUTPUT_SCHEMA``, and a
        callable ``aggregate_score``
      - ``aggregate_score``: full pass -> 1.0, incomplete / empty -> 0.0
      - the ``reward.json`` it writes is a flat scalar dict — Harbor parses it
        into ``VerifierResult(rewards: dict[str, float | int])``, so a list/dict
        value breaks verification. AST-checked here; cross-checked against
        Harbor's real model when ``--harbor-check`` is given.

Exit code is non-zero if any bundle fails.

Usage:
  python validate_harbor_bundles.py OUT/                 # every bundle under OUT/
  python validate_harbor_bundles.py OUT/<task_id>        # a single bundle
  python validate_harbor_bundles.py OUT/ --harbor-check  # also import harbor + check
"""

from __future__ import annotations

import argparse
import ast
import os
import sys
from pathlib import Path

try:
    import tomllib  # Python 3.11+
except ModuleNotFoundError:  # pragma: no cover - environment dependent
    try:
        import tomli as tomllib  # type: ignore
    except ModuleNotFoundError:
        tomllib = None  # type: ignore

REQUIRED_FILES = [
    "task.toml",
    "instruction.md",
    "environment/Dockerfile",
    "environment/enabled_tools.txt",
    "tests/test.sh",
    "tests/agent_judge.py",
    "solution/solve.sh",
]
EXECUTABLE_SCRIPTS = ["tests/test.sh", "solution/solve.sh"]
REWARD_JSON_PATH = "/logs/verifier/reward.json"


def _check_files(bundle: Path) -> list[str]:
    problems = []
    for rel in REQUIRED_FILES:
        if not (bundle / rel).is_file():
            problems.append(f"missing file: {rel}")
    if (bundle / "instruction.md").is_file() and not (bundle / "instruction.md").read_text(encoding="utf-8").strip():
        problems.append("instruction.md is empty")
    return problems


def _check_scripts_executable(bundle: Path) -> list[str]:
    problems = []
    for rel in EXECUTABLE_SCRIPTS:
        f = bundle / rel
        if not f.is_file():
            continue
        if not (f.stat().st_mode & 0o111):
            problems.append(f"{rel} is not executable (mode {oct(f.stat().st_mode & 0o777)})")
        if not f.read_text(encoding="utf-8").startswith("#!"):
            problems.append(f"{rel} has no shebang")
    return problems


def _check_task_toml(bundle: Path) -> list[str]:
    f = bundle / "task.toml"
    if not f.is_file():
        return []  # already reported by _check_files
    if tomllib is None:
        return ["(task.toml structural checks skipped: no tomllib/tomli available)"]
    try:
        data = tomllib.loads(f.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        return [f"task.toml does not parse: {type(e).__name__}: {str(e).splitlines()[0]}"]
    problems = []
    meta = data.get("metadata", {})
    if not meta.get("task_id"):
        problems.append("task.toml: [metadata].task_id missing/empty")
    env = data.get("environment", {})
    servers = env.get("mcp_servers") or []
    if not servers:
        problems.append("task.toml: no [[environment.mcp_servers]] entry")
    if "verifier" not in data:
        problems.append("task.toml: no [verifier] section")
    # enabled_tools.txt must match the task.toml allowlist (the file is what the
    # image actually enforces; drift means the two disagree about scope).
    toml_tools = servers[0].get("enabled_tools", []) if servers else []
    et_file = bundle / "environment" / "enabled_tools.txt"
    if et_file.is_file():
        file_tools = [ln for ln in et_file.read_text(encoding="utf-8").splitlines() if ln.strip()]
        if file_tools != list(toml_tools):
            problems.append("enabled_tools.txt does not match task.toml [[environment.mcp_servers]].enabled_tools")
    return problems


def _exec_judge(src: str) -> dict:
    """Exec the generated judge's module body in a throwaway namespace.

    __name__ is set so the `if __name__ == "__main__"` guard does not run
    run_judge (which would need claude-agent-sdk / a live trajectory). os.environ
    is saved/restored because the script mutates it at import time.
    """
    ns: dict = {"__name__": "_harbor_bundle_validate"}
    saved = dict(os.environ)
    try:
        exec(compile(src, "<agent_judge>", "exec"), ns)
    finally:
        os.environ.clear()
        os.environ.update(saved)
    return ns


_CONTAINER_LITERALS = (ast.List, ast.Dict, ast.Set, ast.Tuple,
                       ast.ListComp, ast.DictComp, ast.SetComp)
_CONTAINER_METHODS = {"append", "extend", "insert", "update", "add"}


def _container_names(tree: ast.AST) -> set[str]:
    """Names bound to a container anywhere in the module.

    Covers both ``x = [...]`` / ``x = {...}`` (and comprehensions) and names
    that are grown via ``x.append(...)`` / ``x.update(...)`` etc. Lets the
    reward.json check flag a value like ``verified_results`` (a list variable),
    not just inline container literals.
    """
    names: set[str] = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Assign) and isinstance(n.value, _CONTAINER_LITERALS):
            for tgt in n.targets:
                if isinstance(tgt, ast.Name):
                    names.add(tgt.id)
        if (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                and n.func.attr in _CONTAINER_METHODS
                and isinstance(n.func.value, ast.Name)):
            names.add(n.func.value.id)
    return names


def _reward_json_violations(src: str) -> list[str]:
    """AST-check that agent_judge.py writes reward.json as a flat scalar dict."""
    try:
        tree = ast.parse(src)
    except SyntaxError as e:
        return [f"agent_judge.py does not parse: {e}"]
    container_names = _container_names(tree)
    problems = []
    found = False
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "write_text" and node.args):
            continue
        recv = node.func.value
        if not (isinstance(recv, ast.Call) and isinstance(recv.func, ast.Name)
                and recv.func.id == "Path" and recv.args
                and isinstance(recv.args[0], ast.Constant)
                and recv.args[0].value == REWARD_JSON_PATH):
            continue
        found = True
        arg = node.args[0]
        if not (isinstance(arg, ast.Call) and isinstance(arg.func, ast.Attribute)
                and arg.func.attr == "dumps" and arg.args):
            problems.append("reward.json write_text arg is not json.dumps(...)")
            continue
        payload = arg.args[0]
        if not isinstance(payload, ast.Dict):
            problems.append("reward.json payload is not a dict literal")
            continue
        for k, v in zip(payload.keys, payload.values):
            key = ast.literal_eval(k) if isinstance(k, ast.Constant) else "<expr>"
            if isinstance(v, _CONTAINER_LITERALS):
                problems.append(f"reward.json key {key!r} has a non-scalar ({type(v).__name__}) value")
            elif isinstance(v, ast.Name) and v.id in container_names:
                problems.append(f"reward.json key {key!r} is bound to a container variable ({v.id!r}); "
                                "Harbor requires scalar reward values")
    if not found:
        problems.append(f"no write to {REWARD_JSON_PATH} found in agent_judge.py")
    return problems


def _check_agent_judge(bundle: Path, harbor_check: bool) -> list[str]:
    f = bundle / "tests" / "agent_judge.py"
    if not f.is_file():
        return []
    src = f.read_text(encoding="utf-8")
    problems = list(_reward_json_violations(src))
    try:
        ns = _exec_judge(src)
    except Exception as e:  # noqa: BLE001 - any import/NameError here is a real defect
        problems.append(f"agent_judge.py module body fails to exec: {type(e).__name__}: {e}")
        return problems
    crit = ns.get("CRITERIA")
    if not isinstance(crit, list):
        problems.append("agent_judge.py: CRITERIA is not a list")
    elif not crit:
        problems.append("agent_judge.py: CRITERIA is empty (task can never pass)")
    if not isinstance(ns.get("OUTPUT_SCHEMA"), dict):
        problems.append("agent_judge.py: OUTPUT_SCHEMA is not a dict")
    agg = ns.get("aggregate_score")
    if not callable(agg):
        problems.append("agent_judge.py: aggregate_score is not callable")
    elif isinstance(crit, list) and crit:
        n = len(crit)
        full = [{"score": 1.0}] * n
        if agg(full) != 1.0:
            problems.append("aggregate_score: a full pass did not score 1.0")
        if agg(full[:-1]) != 0.0:
            problems.append("aggregate_score: an incomplete result set did not score 0.0")
        if agg([]) != 0.0:
            problems.append("aggregate_score: an empty result set did not score 0.0")
        if harbor_check:
            problems.extend(_harbor_verifier_check(agg(full)))
    return problems


def _harbor_verifier_check(reward_value) -> list[str]:
    """Cross-check the emitted reward against Harbor's real VerifierResult."""
    try:
        from harbor.models.verifier.result import VerifierResult
    except Exception as e:  # noqa: BLE001
        return [f"(--harbor-check skipped: harbor not importable: {e})"]
    try:
        VerifierResult(rewards={"reward": reward_value})
    except Exception as e:  # noqa: BLE001
        return [f"reward {{'reward': {reward_value!r}}} rejected by Harbor VerifierResult: {e}"]
    return []


def validate_bundle(bundle: Path, harbor_check: bool = False) -> list[str]:
    problems = _check_files(bundle)
    problems += _check_scripts_executable(bundle)
    problems += _check_task_toml(bundle)
    problems += _check_agent_judge(bundle, harbor_check)
    # Notes (parenthesised) are informational, not failures.
    return problems


def _iter_bundles(root: Path):
    """A bundle is a dir containing task.toml. Accept a single bundle or a parent."""
    if (root / "task.toml").is_file():
        yield root
        return
    for child in sorted(p for p in root.iterdir() if p.is_dir()):
        if (child / "task.toml").is_file():
            yield child


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("path", type=Path, help="A bundle dir, or a parent dir of bundles")
    p.add_argument("--harbor-check", action="store_true",
                   help="Also validate the reward against Harbor's real VerifierResult (needs harbor installed)")
    args = p.parse_args()

    if not args.path.exists():
        print(f"error: path not found: {args.path}", file=sys.stderr)
        return 2
    bundles = list(_iter_bundles(args.path))
    if not bundles:
        print(f"error: no bundles (dirs with task.toml) found under {args.path}", file=sys.stderr)
        return 2

    failed = 0
    for bundle in bundles:
        problems = validate_bundle(bundle, harbor_check=args.harbor_check)
        hard = [p for p in problems if not p.startswith("(")]
        notes = [p for p in problems if p.startswith("(")]
        if hard:
            failed += 1
            print(f"FAIL {bundle.name}")
            for pr in hard:
                print(f"      - {pr}")
        else:
            suffix = f"  {notes[0]}" if notes else ""
            print(f"ok   {bundle.name}{suffix}")

    print(f"\n{len(bundles) - failed}/{len(bundles)} bundles valid", file=sys.stderr)
    if failed:
        print(f"RESULT: FAILED ({failed} bundle(s) with problems)", file=sys.stderr)
        return 1
    print("RESULT: PASSED", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
