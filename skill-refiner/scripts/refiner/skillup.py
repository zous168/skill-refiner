from __future__ import annotations

import json
from pathlib import Path
import re
import shutil
import sys
import time
import os
import shlex
import tempfile
from contextlib import contextmanager

from .bootstrap import PROJECT_ROOT
from .common import RefinerError, read_json, run_process, write_json


@contextmanager
def local_judge_lock(deadline):
    """v0.13.0 names local judge dirs with a coarse clock; avoid collisions."""
    import fcntl
    lock = Path(tempfile.gettempdir()) / f"skill-refiner-judge-{os.getuid()}.lock"
    descriptor = os.open(lock, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        while True:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if deadline is not None and time.monotonic() >= deadline:
                    raise RefinerError("Wall-clock budget exhausted waiting for the local skill-up judge lock")
                time.sleep(0.05)
        yield
    finally:
        os.close(descriptor)


def locate(explicit=None):
    executable = explicit or shutil.which("skill-up") or str(PROJECT_ROOT / ".tools" / "skill-up")
    if not Path(executable).is_file():
        raise RefinerError("skill-up missing. Run: skill-refiner bootstrap")
    return str(Path(executable).resolve())


class SkillUp:
    def __init__(self, executable, engine=None, engine_config=None, timeout=240, parallelism=2):
        if engine_config is None and engine is None:
            raise RefinerError("No evaluation engine selected; configure an executor or pass --engine explicitly")
        self.executable = locate(executable)
        self.engine = engine_config or {"name": engine}
        self.timeout = timeout
        self.parallelism = parallelism
        self.evaluations = 0
        self.max_evaluations = 50
        self.deadline = None
        self.progress_callback = None
        self.env = os.environ.copy()
        self._shim_dir = None
        if engine == "codex" and not engine_config:
            code, stdout, _, _ = run_process(["codex", "--version"], timeout=15)
            match = re.search(r"\d+\.\d+\.\S+", stdout)
            if code or not match:
                raise RefinerError("Cannot determine installed Codex CLI version")
            self.engine["version"] = match[0]
            # v0.13's rollout parser predates custom_tool_call records in recent
            # Codex. Ephemeral runs make upstream use its complete JSONL stdout
            # parser instead. Keep the real CLI/model/auth and all upstream judges.
            real_codex = shutil.which("codex")
            self._shim_dir = tempfile.TemporaryDirectory(prefix="refiner-codex-")
            shim = Path(self._shim_dir.name) / "codex"
            shim.write_text("#!/bin/sh\nif [ \"$1\" = exec ]; then\n shift\n exec " +
                            shlex.quote(real_codex) + " exec --ephemeral \"$@\"\nfi\nexec " +
                            shlex.quote(real_codex) + " \"$@\"\n")
            shim.chmod(0o755)
            self.env["PATH"] = str(shim.parent) + os.pathsep + self.env.get("PATH", "")

    def evaluate(self, target, cases, run_dir, *, selected=None):
        """Compile frozen assertions outside the agent's installed Skill; skill-up owns execution."""
        target, run_dir = Path(target), Path(run_dir)
        run_dir.mkdir(parents=True, exist_ok=True)
        cases = [c for c in cases if selected is None or c["id"] in selected]
        if not cases:
            raise RefinerError("Refusing to run an empty test selection")
        generated = target / "evals" / ".refiner-generated"
        if generated.exists():
            shutil.rmtree(generated)
        generated.mkdir(parents=True)
        case_paths = []
        for case in cases:
            frozen = run_dir / f"{case['id']}.oracle.json"
            write_json(frozen, case)
            grader = run_dir / f"{case['id']}.judge.sh"
            module_root = str(Path(__file__).resolve().parent.parent)
            # Use shlex.quote rather than interpolating model text into executable shell.
            import shlex
            pycode = "from refiner.checks import grader_main; import sys; sys.exit(grader_main(sys.argv[1]))"
            grader.write_text("#!/bin/sh\nexport PYTHONPATH=" + shlex.quote(module_root) +
                              "\nexec " + shlex.quote(sys.executable) + " -c " + shlex.quote(pycode) +
                              " " + shlex.quote(str(frozen)) + "\n", encoding="utf-8")
            grader.chmod(0o755)
            native = {"id": case["id"], "title": case["id"], "input": {"prompt": case["prompt"]},
                      "context": {"files": {**case["files"], **(
                          {"refiner-python-probe.py": Path(__file__).with_name("python_probe.py").read_text()}
                          if "python_probe" in case.get("helpers", []) else {})}},
                      "constraints": {"timeout_seconds": self.timeout},
                      "judge": {"type": "script", "script_path": str(grader), "timeout_seconds": 30}}
            path = generated / f"{case['id']}.yaml"
            write_json(path, native)  # JSON is valid YAML; no template escaping ambiguity.
            case_paths.append(str(path.relative_to(target)))
        config = {"schema_version": "v1alpha1", "environment": {"type": "none"},
                  "skills": [{"source": "local_path", "path": "."}],
                  "engine": self.engine,
                  "cases": {"files": case_paths, "parallelism": 1,
                            "defaults": {"timeout_seconds": self.timeout}},
                  "benchmark": {"enabled": False}, "report": {"formats": ["json"]}}
        config_path = target / "evals" / "refiner-eval.yaml"
        if config_path.exists():
            raise RefinerError("Target already contains evals/refiner-eval.yaml; choose a different internal namespace")
        write_json(config_path, config)
        try:
            code, _, stderr, _ = run_process([self.executable, "validate", str(config_path)],
                                             timeout=30, log=run_dir / "validate.log")
            if code:
                raise RefinerError(f"skill-up rejected generated evals; see {run_dir / 'validate.log'}")
            result = self._execute(config_path, run_dir, len(cases))
            results = result["cases"]
            expected = {c["id"] for c in cases}
            if {r["id"] for r in results} != expected or len(results) != len(expected):
                raise RefinerError("skill-up returned missing, duplicated or unexpected cases")
            write_json(run_dir / "summary.json", result)
            return result
        finally:
            config_path.unlink(missing_ok=True)
            shutil.rmtree(generated, ignore_errors=True)

    def _execute(self, config_path, run_dir, case_count):
        if self.evaluations + case_count > self.max_evaluations:
            raise RefinerError("Target-evaluation budget exhausted")
        self.evaluations += case_count
        timeout = self.timeout * case_count + 120
        if self.deadline is not None:
            timeout = min(timeout, self.deadline - time.monotonic())
            if timeout <= 0:
                raise RefinerError("Wall-clock budget exhausted before evaluation")
        with local_judge_lock(self.deadline):
            if self.deadline is not None:
                timeout = min(timeout, self.deadline - time.monotonic())
                if timeout <= 0:
                    raise RefinerError("Wall-clock budget exhausted after waiting for judge lock")
            engine_label = (self.engine.get("name", self.engine.get("model", "configured"))
                            if isinstance(self.engine, dict) else "configured")
            print(json.dumps({"stage": "evaluation", "status": "running", "engine": engine_label,
                              "case_count": case_count, "case_evaluations_reserved": case_count,
                              "max_evaluations": self.max_evaluations,
                              "timeout_seconds": round(timeout)}, ensure_ascii=False), flush=True)
            def heartbeat(elapsed, remaining):
                if self.progress_callback:
                    self.progress_callback(elapsed, remaining, case_count, engine_label, self.evaluations)
                print(json.dumps({"stage": "evaluation", "status": "still_running", "engine": engine_label,
                                  "elapsed_seconds": elapsed, "remaining_timeout_seconds": remaining,
                                  "case_count": case_count, "case_evaluations_reserved": case_count,
                                  "max_evaluations": self.max_evaluations}, ensure_ascii=False), flush=True)
            code, _, _, duration = run_process(
                [self.executable, "run", str(config_path), "--output-dir", str(run_dir / "upstream")],
                timeout=timeout, log=run_dir / "skill-up.log", env=self.env,
                on_progress=heartbeat, progress_interval=30)
        reports = sorted((run_dir / "upstream").glob("iteration-*/result.json"))
        if not reports:
            raise RefinerError(f"skill-up produced no result.json (exit {code}); see {run_dir / 'skill-up.log'}")
        raw = read_json(reports[-1])
        write_json(run_dir / "upstream-result.json", raw)
        rows = raw.get("case_results", [])
        if not rows:
            raise RefinerError("Unknown skill-up result schema or zero cases")
        results = []
        for row in rows:
            status = row.get("status", "ERROR").upper()
            if status not in {"PASS", "FAIL", "ERROR"}:
                status = "ERROR"
            # Detect misrouted upstream script judges independently of verdict.
            for assertion in (row.get("grading") or {}).get("assertion_results", []):
                try:
                    evidence = json.loads(assertion.get("evidence", ""))
                except (ValueError, TypeError):
                    continue
                if isinstance(evidence, dict) and "case_id" in evidence and evidence["case_id"] != row.get("case_id"):
                    status = "ERROR"
                    row["error"] = "Judge case identity mismatch; result cannot be trusted"
            results.append({"id": row.get("case_id"), "status": status,
                            "evidence_directory": str((run_dir / "upstream").resolve()),
                            "evidence": row, "passed": status == "PASS"})
        if code and all(r["passed"] for r in results):
            raise RefinerError("skill-up exited nonzero despite all cases passing")
        summary = {"passed": all(r["passed"] for r in results), "cases": results,
                "seconds": duration, "engine": self.engine, "upstream_exit_code": code,
                "requested_parallelism": self.parallelism, "effective_parallelism": 1,
                "execution_note": "Pinned skill-up local script judges serialized to prevent temporary-directory collisions",
                "upstream_report": str(reports[-1])}
        counts = {status: sum(row["status"] == status for row in results) for status in ("PASS", "FAIL", "ERROR")}
        print(json.dumps({"stage": "evaluation", "status": "completed", "seconds": duration,
                          "case_count": len(results), **counts}, ensure_ascii=False), flush=True)
        return summary

    def evaluate_existing(self, target, run_dir):
        """Preserve and execute native eval.yaml; never claim existing tests ran if unsupported."""
        import yaml
        target, run_dir = Path(target), Path(run_dir)
        config = target / "evals" / "eval.yaml"
        if not config.exists():
            if (target / "evals" / "evals.json").exists():
                raise RefinerError("Existing Anthropic evals.json must first be imported with skill-up import")
            return None
        native = yaml.safe_load(config.read_text())
        if not isinstance(native, dict) or native.get("environment", {}).get("type", "none") != "none":
            raise RefinerError("v0.1 existing eval suites must use environment.type=none")
        native["engine"] = self.engine
        native["benchmark"] = {"enabled": False}
        native["report"] = {"formats": ["json"]}
        native["cases"]["parallelism"] = 1
        generated = target / "evals" / "refiner-existing.yaml"
        if generated.exists():
            raise RefinerError("Reserved evals/refiner-existing.yaml already exists")
        run_dir.mkdir(parents=True, exist_ok=True)
        write_json(generated, native)
        try:
            code, _, _, _ = run_process([self.executable, "validate", str(generated)], timeout=30,
                                        log=run_dir / "validate.log")
            if code:
                raise RefinerError(f"Existing native suite validation failed: {run_dir / 'validate.log'}")
            result = self._execute(generated, run_dir, len(native["cases"]["files"]))
            for row in result["cases"]:
                row["id"] = "existing:" + row["id"]
            write_json(run_dir / "summary.json", result)
            return result
        finally:
            generated.unlink(missing_ok=True)


