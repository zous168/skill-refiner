from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from .bootstrap import PROJECT_ROOT, install
from .common import RefinerError, run_process
from .standards import lint


def workflow_options(parser, *, creating=False):
    parser.add_argument("--spec", action="append", default=[], help="Independent business requirements (repeatable)")
    parser.add_argument("--standard", action="append", default=[],
                        help="Additional standards; project standards are auto-discovered (repeatable)")
    parser.add_argument("--output", help="New evidence workspace; must not already exist")
    parser.add_argument("--config", help="Independent analysis/creation/planning/repair/review and regression backends")
    parser.add_argument("--skill-up", help="Explicit skill-up executable")
    parser.add_argument("--engine", choices=["codex", "claude_code"],
                        help="Legacy skill-up engine; select explicitly when not using --config")
    parser.add_argument("--engine-config", help="Legacy skill-up engine JSON")
    parser.add_argument("--agent-command", help="Legacy controller JSON stdin/stdout command")
    parser.add_argument("--max-rounds", type=int, default=3)
    parser.add_argument("--planning-attempts", type=int, default=4 if creating else 2,
                        help="Bounded plan/review revisions per development or acceptance suite")
    parser.add_argument("--max-cases", type=int, default=8 if creating else 4)
    parser.add_argument("--acceptance-cases", type=int, default=6 if creating else 2)
    parser.add_argument("--parallelism", type=int, default=2)
    parser.add_argument("--timeout", type=int, default=300 if creating else 240)
    parser.add_argument("--max-seconds", type=int, default=3600 if creating else 2400)
    parser.add_argument("--max-agent-calls", type=int, default=24 if creating else 16)
    parser.add_argument("--max-evaluations", type=int, default=100 if creating else 50)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Autonomous Skill planning, repair and real skill-up regression")
    subs = parser.add_subparsers(dest="command", required=True)
    boot = subs.add_parser("bootstrap", help="Install verified skill-up v0.13.0 locally")
    boot.add_argument("--directory")
    doctor = subs.add_parser("doctor", help="Check local agent and evaluation dependencies")
    doctor.add_argument("--config", help="Inspect every configured backend without making model calls")
    check = subs.add_parser("check", help="Run deterministic Skill format/resource/script checks")
    check.add_argument("target")
    verify = subs.add_parser("verify", help="Verify that a run passed every required pipeline gate")
    verify.add_argument("run", help="Completed run workspace created by `run` or `create`")
    execute = subs.add_parser("run", help="Plan, evaluate, repair, regress and independently accept a Skill")
    execute.add_argument("target")
    workflow_options(execute)
    execute.add_argument("--reuse-plan", help="Reuse frozen autonomous plans from an earlier run; rerun baseline and all verification")
    create = subs.add_parser("create", help="Analyze a business request, implement a Skill and run the full refinement pipeline")
    create.add_argument("description", nargs="?", help="Natural-language business request; --spec files may also supply it")
    create.add_argument("--name", help="Optional kebab-case Skill name; otherwise chosen during analysis")
    create.add_argument("--resume-from", help="Reuse an approved creation contract and validated first draft; rerun all execution")
    create.add_argument("--destination", help="New destination populated only after successful multi-engine acceptance")
    workflow_options(create, creating=True)
    compare = subs.add_parser("compare", help="Run one frozen suite against all configured engines; no planning or repair")
    compare.add_argument("target")
    compare.add_argument("--suite", required=True, help="Earlier run directory containing locked plans")
    compare.add_argument("--config", required=True)
    compare.add_argument("--output")
    compare.add_argument("--skill-up")
    compare.add_argument("--parallelism", type=int, default=2)
    compare.add_argument("--timeout", type=int, default=240)
    compare.add_argument("--max-seconds", type=int, default=2400)
    compare.add_argument("--max-evaluations", type=int, default=100)
    args = parser.parse_args(argv)
    try:
        if args.command == "bootstrap":
            print(install(args.directory))
            return 0
        if args.command == "doctor":
            if args.config:
                from .diagnostics import inspect_config
                result = inspect_config(args.config)
                print(json.dumps(result, ensure_ascii=False, indent=2))
                return 0 if result["ready"] else 1
            from .skillup import locate
            try:
                up = locate()
            except RefinerError:
                up = None
            result = {"python": sys.executable, "skill_up": up, "project": str(PROJECT_ROOT),
                      "backend_config_required": True,
                      "note": "No provider is selected by default. Pass --config to check configured backends; "
                              "workflows require --config or an explicit legacy adapter."}
            if up:
                code, output, _, _ = run_process([up, "--version"], timeout=15)
                result["skill_up_version"] = output.strip()
                result["skill_up_works"] = code == 0
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0 if result.get("skill_up_works") else 1
        if args.command == "check":
            findings = lint(Path(args.target).resolve())
            print(json.dumps(findings, ensure_ascii=False, indent=2))
            return int(any(f["severity"] == "error" for f in findings))
        if args.command == "verify":
            from .gate import verify_run
            result = verify_run(args.run)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        if args.command == "compare":
            for key in ("parallelism", "timeout", "max_seconds", "max_evaluations"):
                if getattr(args, key) < 1:
                    raise RefinerError(f"--{key.replace('_', '-')} must be positive")
            from .compare import run as compare_run
            return compare_run(args)
        for key in ("max_rounds", "planning_attempts", "parallelism", "timeout", "max_seconds", "max_agent_calls", "max_evaluations"):
            if getattr(args, key) < 1:
                raise RefinerError(f"--{key.replace('_', '-')} must be positive")
        if not 2 <= args.max_cases <= 30 or not 2 <= args.acceptance_cases <= 30:
            raise RefinerError("Case limits must be between 2 and 30")
        if args.command == "create":
            from .creation import run
        else:
            from .pipeline import run
        state = run(args)
        print("Report: " + str(Path(state["workspace"]) / "report.md"))
        return 0 if state["status"] in {"passed", "unchanged", "created"} else 2
    except (RefinerError, OSError, ValueError) as exc:
        print(f"skill-refiner: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

