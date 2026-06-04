#!/usr/bin/env python3
"""End-to-end smoke test for Harbor task bundles (Tier-2/3 pre-delivery gate).

Runs a *sample* of bundles through real Harbor under one or more conditions and
asserts each trial produced a valid reward — i.e. the environment builds, the
healthcheck passes, the verifier runs, and **Harbor ingests the reward without a
schema error or exception**. This is the live counterpart to
``validate_harbor_bundles.py`` (which is static); use both before a delivery.

A "condition" is a (Harbor binary, agent) pair. Pass each repeatably to cover the
combinations that matter — at minimum the latest **json-first** Harbor, which is
what surfaces the reward.json schema bug:

    # cheap: every sampled bundle on latest Harbor with the nop agent
    python harbor_smoke.py OUT/ --sample 5 \\
        --harbor ~/harbor-smoke/venv_013/bin/harbor --agent nop

    # belt-and-suspenders: also a pinned (txt-first) Harbor, and a real agent
    python harbor_smoke.py OUT/ --sample 5 \\
        --harbor ~/harbor-smoke/venv_013/bin/harbor \\
        --harbor ~/harbor-smoke/venv_0145/bin/harbor \\
        --agent nop --agent claude-code -m claude-sonnet-4-6

The same sampled bundles are reused across every condition (comparability).
Requires Docker + the bundles' images reachable, and the LLM auth env vars
(ANTHROPIC_*/LITELLM_*) exported for the verifier (and for a real agent).

`nop` exercises env build + healthcheck + verifier + Harbor's reward read with no
LLM cost (the judge fast-fails on the missing trajectory and writes reward 0.0);
`claude-code` additionally exercises the success-path reward write.

A PASS = the trial's result.json has a non-null ``verifier_result`` with a numeric
``reward`` in [0, 1] and ``exception_info == null``. Exit code is non-zero if any
(bundle, condition) cell fails.
"""

from __future__ import annotations

import argparse
import json
import random
import subprocess
import sys
import tempfile
from pathlib import Path


def _iter_bundles(root: Path):
    if (root / "task.toml").is_file():
        yield root
        return
    for child in sorted(p for p in root.iterdir() if p.is_dir()):
        if (child / "task.toml").is_file():
            yield child


def _find_trial_result(out_dir: Path) -> dict | None:
    """Return the trial-level result.json (the one with verifier_result/exception_info)."""
    best = None
    for rj in out_dir.rglob("result.json"):
        try:
            data = json.loads(rj.read_text())
        except Exception:  # noqa: BLE001
            continue
        if isinstance(data, dict) and ("verifier_result" in data or "exception_info" in data):
            best = data  # prefer the deepest/last (trial-level) over job-level
    return best


def _verdict(result: dict | None) -> tuple[bool, str]:
    if result is None:
        return False, "no trial result.json produced"
    exc = result.get("exception_info")
    if exc:
        etype = exc.get("exception_type") if isinstance(exc, dict) else exc
        return False, f"exception: {etype}"
    vr = result.get("verifier_result")
    if not vr or not isinstance(vr, dict):
        return False, "verifier_result is null/empty"
    rewards = vr.get("rewards")
    if not isinstance(rewards, dict) or "reward" not in rewards:
        return False, f"no scalar 'reward' in verifier_result.rewards ({rewards!r})"
    val = rewards["reward"]
    if not isinstance(val, (int, float)) or not (0.0 <= float(val) <= 1.0):
        return False, f"reward out of range/!numeric: {val!r}"
    return True, f"reward={val}"


def run_one(harbor: str, agent: str, model: str, bundle: Path, out_root: Path) -> tuple[bool, str]:
    out = out_root / f"{bundle.name}__{Path(harbor).parent.parent.name}__{agent}"
    out.mkdir(parents=True, exist_ok=True)
    cmd = [harbor, "run", "-p", str(bundle), "-a", agent, "-k", "1", "-n", "1", "-y", "-o", str(out)]
    if model:
        cmd += ["-m", model]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    result = _find_trial_result(out)
    ok, detail = _verdict(result)
    if not ok and result is None and proc.returncode != 0:
        # surface the harbor error tail when nothing was produced
        tail = (proc.stderr or proc.stdout).strip().splitlines()[-1:] or [""]
        detail = f"{detail} | harbor: {tail[0][:120]}"
    return ok, detail


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("path", type=Path, help="A bundle dir, or a parent dir of bundles")
    p.add_argument("--harbor", action="append", required=True, metavar="HARBOR_BIN",
                   help="Path to a harbor binary (repeat for multiple versions)")
    p.add_argument("--agent", action="append", required=True, metavar="AGENT",
                   help="Agent name, e.g. nop or claude-code (repeat for multiple)")
    p.add_argument("-m", "--model", default="", help="Model for the agent (e.g. claude-sonnet-4-6)")
    p.add_argument("--sample", type=int, default=3, help="Number of bundles to sample (0 = all)")
    p.add_argument("--seed", type=int, default=0, help="Sampling seed (for a reproducible sample)")
    p.add_argument("--out", type=Path, default=None, help="Output dir for harbor runs (default: temp)")
    args = p.parse_args()

    bundles = list(_iter_bundles(args.path))
    if not bundles:
        print(f"error: no bundles under {args.path}", file=sys.stderr)
        return 2
    random.seed(args.seed)
    if args.sample and args.sample < len(bundles):
        bundles = random.sample(bundles, args.sample)
    bundles.sort(key=lambda b: b.name)

    out_root = args.out or Path(tempfile.mkdtemp(prefix="harbor_smoke_"))
    conditions = [(h, a) for h in args.harbor for a in args.agent]
    print(f"Smoke: {len(bundles)} bundle(s) x {len(conditions)} condition(s)  ->  out={out_root}\n")

    failures = 0
    for bundle in bundles:
        print(f"== {bundle.name} ==")
        for harbor, agent in conditions:
            ver = Path(harbor).parent.parent.name  # e.g. venv_013
            ok, detail = run_one(harbor, agent, args.model, bundle, out_root)
            print(f"   [{ver} / {agent}] {'PASS' if ok else 'FAIL'} — {detail}")
            if not ok:
                failures += 1
        print()

    total = len(bundles) * len(conditions)
    print(f"{total - failures}/{total} (bundle x condition) cells passed", file=sys.stderr)
    if failures:
        print(f"RESULT: FAILED ({failures} cell(s))", file=sys.stderr)
        return 1
    print("RESULT: PASSED", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
