"""Evaluate an existing frozen suite against a target without planning or repair."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import hashlib
import time

from .checks import validate_plan
from .common import RefinerError, digest, manifest, read_json, write_json
from .matrix import Matrix
from .profiles import load_config


def run(args):
    config = load_config(args.config)
    suite, target = Path(args.suite).resolve(), Path(args.target).resolve()
    if not (target / "SKILL.md").is_file():
        raise RefinerError("Comparison target needs SKILL.md")
    plan, acceptance, inputs = [read_json(suite / name) for name in
                                ("test-plan.json", "acceptance-plan.json", "inputs.json")]
    lock = read_json(suite / "oracle.lock.json")
    if digest({"plan": plan, "acceptance": acceptance, "inputs": inputs}) != lock["sha256"]:
        raise RefinerError("Frozen suite integrity check failed")
    helper = Path(__file__).with_name('python_probe.py')
    if any(c.get('helpers') for c in plan['cases'] + acceptance['cases']) and lock.get('python_probe_sha256') != hashlib.sha256(helper.read_bytes()).hexdigest():
        raise RefinerError('Frozen Python observer integrity check failed')
    if plan["requirements"] != acceptance["requirements"]:
        raise RefinerError("Acceptance and development contracts differ")
    output = Path(args.output).resolve() if args.output else target.parent / (
        target.name + "-compare-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ"))
    if output.exists() or output.is_relative_to(target):
        raise RefinerError("Comparison output must be a new directory outside target")
    validate_plan(plan, output, 30)
    validate_plan(acceptance, output, 30)
    output.mkdir(parents=True)
    before, started = manifest(target), time.monotonic()
    state = {"status": "running", "target": str(target), "suite": str(suite), "suite_sha256": lock["sha256"], "config": config}
    write_json(output / "state.json", state)
    lines = ["# Frozen-suite comparison", "", f"Target: `{target}`", f"Suite: `{suite}`", "",
             "The suite is reused unchanged. Results cover its frozen contract, not newly added capabilities.", ""]
    try:
        backend = Matrix(config["executors"], output, args.skill_up, args.timeout, args.parallelism)
        backend.max_evaluations, backend.deadline = args.max_evaluations, started + args.max_seconds
        results = {}
        for phase, cases in (("regression", plan["cases"]), ("acceptance", acceptance["cases"])):
            result = backend.evaluate(target, cases, output / phase)
            results[phase] = result
            lines += ["## " + phase, "", (output / phase / "comparison.md").read_text(encoding="utf-8"), ""]
        if manifest(target) != before:
            raise RefinerError("Comparison target changed during evaluation")
        if any(c.get('helpers') for c in plan['cases'] + acceptance['cases']) and lock.get('python_probe_sha256') != hashlib.sha256(helper.read_bytes()).hexdigest():
            raise RefinerError('Frozen Python observer changed during comparison')
        state.update(status="passed" if all(r["passed"] for r in results.values()) else "failed",
                     target_evaluations=backend.evaluations, results={p: str(output / p / "summary.json") for p in results})
    except Exception as exc:
        state.update(status="error", error=str(exc))
        lines += ["Error: " + str(exc)]
    finally:
        state["elapsed_seconds"] = round(time.monotonic() - started, 2)
        write_json(output / "state.json", state)
        lines.insert(2, "Status: **" + state["status"] + "**")
    (output / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("Report: " + str(output / "report.md"))
    return 0 if state["status"] == "passed" else 2
