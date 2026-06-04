#!/usr/bin/env python3
"""Validate Harbor task bundles before delivery (deterministic; no Docker/LLM).

Checks each bundle directory against the structural and verifier contracts Harbor
relies on, so format regressions are caught here instead of at run time (or by a
customer). Stdlib-only, so it runs anywhere — including CI.

It understands two bundle shapes and applies the right structural checks to each:

  * **rl**       — single MCP server image: ``environment/Dockerfile`` (``FROM
                   <image>``) + ``environment/enabled_tools.txt``; one
                   ``[[environment.mcp_servers]]`` named ``mcp-server``.
  * **advanced** — multi-service gateway: ``environment/docker-compose.yaml``
                   (+ a ``Dockerfile`` for the ``main`` service); a ``gateway``
                   mcp_server.

The **verifier checks are shared** (both shapes embed the same LLM-as-judge):

  * ``tests/agent_judge.py`` compiles and its module body execs cleanly — catches
    the JSON-literal ``NameError`` class (``CRITERIA``/``OUTPUT_SCHEMA`` embedded
    via ``json.dumps`` so ``true``/``false``/``null`` become undefined names);
  * it defines a non-empty list ``CRITERIA``, a dict ``OUTPUT_SCHEMA``, a callable
    ``aggregate_score`` (full pass -> 1.0, incomplete / empty -> 0.0);
  * the ``reward.json`` it writes is a **flat scalar dict** — Harbor parses it into
    ``VerifierResult(rewards: dict[str, float | int])``, so a list/dict value (even
    via a variable like ``verified_results``) breaks verification. AST + light
    dataflow; ``--harbor-check`` also validates against Harbor's real model.

Errors fail the bundle (non-zero exit); warnings are reported but don't fail.

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

# Files every bundle must have, regardless of shape.
COMMON_REQUIRED = [
    "task.toml",
    "instruction.md",
    "environment/Dockerfile",
    "tests/test.sh",
    "tests/agent_judge.py",
]
EXECUTABLE_SCRIPTS = ["tests/test.sh", "solution/solve.sh"]
REWARD_JSON_PATH = "/logs/verifier/reward.json"


class Report:
    def __init__(self) -> None:
        self.errors: list[str] = []
        self.warnings: list[str] = []

    def err(self, m: str) -> None:
        self.errors.append(m)

    def warn(self, m: str) -> None:
        self.warnings.append(m)


def _detect_shape(bundle: Path) -> str:
    if (bundle / "environment" / "docker-compose.yaml").is_file():
        return "advanced"
    if (bundle / "environment" / "enabled_tools.txt").is_file():
        return "rl"
    return "unknown"


def _check_files(bundle: Path, shape: str, r: Report) -> None:
    required = list(COMMON_REQUIRED)
    if shape == "rl":
        required.append("environment/enabled_tools.txt")
    elif shape == "advanced":
        required.append("environment/docker-compose.yaml")
    for rel in required:
        if not (bundle / rel).is_file():
            r.err(f"missing file: {rel}")
    inst = bundle / "instruction.md"
    if inst.is_file() and not inst.read_text(encoding="utf-8").strip():
        r.err("instruction.md is empty")
    # solution/ is optional in Harbor; note if absent, don't fail.
    if not (bundle / "solution" / "solve.sh").is_file():
        r.warn("no solution/solve.sh (optional, but most bundles ship a stub)")


def _check_scripts(bundle: Path, r: Report) -> None:
    for rel in EXECUTABLE_SCRIPTS:
        f = bundle / rel
        if not f.is_file():
            continue
        text = f.read_text(encoding="utf-8")
        if not text.startswith("#!"):
            r.err(f"{rel} has no shebang (Harbor execs it directly)")
        # Harbor chmods +x before exec, so a missing exec bit is a warning, not a failure.
        if not (f.stat().st_mode & 0o111):
            r.warn(f"{rel} is not executable (mode {oct(f.stat().st_mode & 0o777)}); Harbor chmods it, but +x is cleaner")


def _check_task_toml(bundle: Path, shape: str, r: Report) -> None:
    f = bundle / "task.toml"
    if not f.is_file():
        return
    if tomllib is None:
        r.warn("task.toml structural checks skipped: no tomllib/tomli available (need Python 3.11+)")
        return
    try:
        data = tomllib.loads(f.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        r.err(f"task.toml does not parse: {type(e).__name__}: {str(e).splitlines()[0]}")
        return
    if "verifier" not in data:
        r.err("task.toml: no [verifier] section")
    env = data.get("environment", {})
    servers = env.get("mcp_servers") or []
    if not servers:
        r.err("task.toml: no [[environment.mcp_servers]] entry")
    if shape == "rl":
        if not data.get("metadata", {}).get("task_id"):
            r.err("task.toml: [metadata].task_id missing/empty")
        toml_tools = servers[0].get("enabled_tools", []) if servers else []
        et_file = bundle / "environment" / "enabled_tools.txt"
        if et_file.is_file():
            file_tools = [ln for ln in et_file.read_text(encoding="utf-8").splitlines() if ln.strip()]
            if file_tools != list(toml_tools):
                r.err("enabled_tools.txt does not match task.toml [[environment.mcp_servers]].enabled_tools")


# ── shared agent_judge.py checks ──────────────────────────────────────────────

_CONTAINER_LITERALS = (ast.List, ast.Dict, ast.Set, ast.Tuple,
                       ast.ListComp, ast.DictComp, ast.SetComp)
_CONTAINER_METHODS = {"append", "extend", "insert", "update", "add"}


def _container_names(tree: ast.AST) -> set[str]:
    """Names bound to a container anywhere in the module (literal assignment or
    grown via .append/.update/etc.) — so a reward value like ``verified_results``
    (a list variable) is flagged, not just inline container literals."""
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
    try:
        tree = ast.parse(src)
    except SyntaxError as e:
        return [f"agent_judge.py does not parse: {e}"]
    container_names = _container_names(tree)
    problems: list[str] = []
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


def _exec_judge(src: str) -> dict:
    ns: dict = {"__name__": "_harbor_bundle_validate"}
    saved = dict(os.environ)
    try:
        exec(compile(src, "<agent_judge>", "exec"), ns)
    finally:
        os.environ.clear()
        os.environ.update(saved)
    return ns


def _harbor_verifier_check(reward_value) -> list[str]:
    try:
        from harbor.models.verifier.result import VerifierResult
    except Exception as e:  # noqa: BLE001
        return [f"(--harbor-check skipped: harbor not importable: {e})"]
    try:
        VerifierResult(rewards={"reward": reward_value})
    except Exception as e:  # noqa: BLE001
        return [f"reward {{'reward': {reward_value!r}}} rejected by Harbor VerifierResult: {e}"]
    return []


def _check_agent_judge(bundle: Path, harbor_check: bool, r: Report) -> None:
    f = bundle / "tests" / "agent_judge.py"
    if not f.is_file():
        return
    src = f.read_text(encoding="utf-8")
    for p in _reward_json_violations(src):
        (r.warn if p.startswith("(") else r.err)(p)
    try:
        ns = _exec_judge(src)
    except Exception as e:  # noqa: BLE001 - any import/NameError here is a real defect
        r.err(f"agent_judge.py module body fails to exec: {type(e).__name__}: {e}")
        return
    crit = ns.get("CRITERIA")
    if not isinstance(crit, list):
        r.err("agent_judge.py: CRITERIA is not a list")
    elif not crit:
        r.err("agent_judge.py: CRITERIA is empty (task can never pass)")
    if not isinstance(ns.get("OUTPUT_SCHEMA"), dict):
        r.err("agent_judge.py: OUTPUT_SCHEMA is not a dict")
    agg = ns.get("aggregate_score")
    if not callable(agg):
        r.err("agent_judge.py: aggregate_score is not callable")
    elif isinstance(crit, list) and crit:
        n = len(crit)
        full = [{"score": 1.0}] * n
        if agg(full) != 1.0:
            r.err("aggregate_score: a full pass did not score 1.0")
        if agg(full[:-1]) != 0.0:
            r.err("aggregate_score: an incomplete result set did not score 0.0")
        if agg([]) != 0.0:
            r.err("aggregate_score: an empty result set did not score 0.0")
        if harbor_check:
            for p in _harbor_verifier_check(agg(full)):
                (r.warn if p.startswith("(") else r.err)(p)


def validate_bundle(bundle: Path, harbor_check: bool = False) -> tuple[str, Report]:
    r = Report()
    shape = _detect_shape(bundle)
    if shape == "unknown":
        r.warn("could not detect bundle shape (no docker-compose.yaml or enabled_tools.txt); "
               "running shared checks only")
    _check_files(bundle, shape, r)
    _check_scripts(bundle, r)
    _check_task_toml(bundle, shape, r)
    _check_agent_judge(bundle, harbor_check, r)
    return shape, r


def _iter_bundles(root: Path):
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
        shape, r = validate_bundle(bundle, harbor_check=args.harbor_check)
        if r.errors:
            failed += 1
            print(f"FAIL [{shape}] {bundle.name}")
            for m in r.errors:
                print(f"      - {m}")
            for m in r.warnings:
                print(f"      ~ {m}")
        else:
            tail = f"  ({len(r.warnings)} warning(s))" if r.warnings else ""
            print(f"ok   [{shape}] {bundle.name}{tail}")
            for m in r.warnings:
                print(f"      ~ {m}")

    print(f"\n{len(bundles) - failed}/{len(bundles)} bundles valid", file=sys.stderr)
    if failed:
        print(f"RESULT: FAILED ({failed} bundle(s) with errors)", file=sys.stderr)
        return 1
    print("RESULT: PASSED", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
