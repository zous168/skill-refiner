"""Machine-checkable finalization gate for completed Skill Refiner runs."""
from __future__ import annotations

from pathlib import Path

from .common import RefinerError, digest, manifest, read_json
from .artifact_validation import validate_skill_schema_package
from .stages import verify_stage_manifest


def _verify_phase_history(state):
    phases = state.get("phase_results")
    expected_phase_statuses = ["not_started", "pending", "running", "passed", "failed", "error", "skipped", "reused"]
    expected_gate_statuses = ["pending", "running", "passed", "not_passed"]
    if (not isinstance(phases, dict)
            or phases.get("schema_version") != "skill-refiner.phase-results/v1"
            or phases.get("status_enums") != {"phase": expected_phase_statuses,
                                               "plan_gate": expected_gate_statuses}):
        raise RefinerError("Phase result state is missing or does not match the versioned status contract")
    for name in ("plan_generation", "plan_self_check", "independent_review"):
        entry = phases.get(name, {})
        if entry.get("status") not in {"passed", "reused"}:
            raise RefinerError(f"Plan phase {name} is not complete: {entry.get('status')!r}")
        suites = entry.get("suites", {})
        if any(suites.get(suite) not in {"passed", "reused"}
               for suite in ("development", "acceptance")):
            raise RefinerError(f"Plan phase {name} lacks passed development and acceptance evidence")
    if phases.get("plan_gate", {}).get("status") != "passed":
        raise RefinerError(f"Plan gate is not passed: {phases.get('plan_gate', {}).get('status')!r}")
    evaluation = phases.get("skill_evaluation", {})
    if evaluation.get("status") != "passed" or any(
            evaluation.get("suites", {}).get(suite) != "passed"
            for suite in ("baseline", "regression", "acceptance")):
        raise RefinerError("Target Skill evaluation lacks passed baseline, regression and acceptance evidence")
    if phases.get("final_verification", {}).get("status") not in {"running", "passed"}:
        raise RefinerError("Final Refiner verification has not reached its machine gate")

    history = state.get("phase_history")
    if not isinstance(history, list):
        raise RefinerError("Phase history is missing")
    gate_passes = [row.get("sequence", index) for index, row in enumerate(history)
                   if isinstance(row, dict) and row.get("phase") == "plan_gate"
                   and row.get("status") == "passed"]
    evaluations = [row.get("sequence", index) for index, row in enumerate(history)
                   if isinstance(row, dict) and row.get("phase") == "skill_evaluation"
                   and row.get("status") == "running"]
    if not gate_passes or not evaluations or min(gate_passes) > min(evaluations):
        raise RefinerError("Target Skill evaluation started without an earlier passed plan gate")


def _verify_plan_control_artifacts(root, plans):
    from .checks import validate_plan
    from .common import PlanValidationError

    for suite, plan in plans.items():
        summary_path = _required_file(root, f"plan-controls/{suite}.json")
        summary = read_json(summary_path)
        if (summary.get("schema_version") != "skill-refiner.plan-controls/v1"
                or summary.get("suite") != suite or summary.get("status") != "passed"
                or summary.get("plan_sha256") != digest(plan)
                or summary.get("plan_snapshot") != plan):
            raise RefinerError(f"{suite} plan control summary does not bind to the frozen plan")

        attempts_dir = root / "plan-controls" / "attempts"
        attempts = []
        if attempts_dir.is_dir():
            for path in sorted(attempts_dir.glob(f"{suite}-*.json")):
                attempt = read_json(path)
                if (attempt.get("schema_version") != "skill-refiner.plan-controls/v1"
                        or attempt.get("suite") != suite
                        or not isinstance(attempt.get("attempt"), int)
                        or attempt.get("plan_sha256") != digest(attempt.get("plan_snapshot"))):
                    raise RefinerError(f"Malformed {suite} plan-control attempt evidence: {path.name}")
                try:
                    recomputed = validate_plan(attempt["plan_snapshot"], root, max_cases=30)
                except PlanValidationError as exc:
                    expected_status = exc.evidence.get("status")
                    expected_cases = exc.evidence.get("case_control_results")
                    expected_failures = exc.evidence.get("control_failures")
                    expected_coverage = exc.evidence.get("coverage_failures")
                else:
                    expected_status = "passed"
                    expected_cases = recomputed.get("case_control_results")
                    expected_failures = []
                    expected_coverage = []
                if (attempt.get("status") != expected_status
                        or digest(attempt.get("case_control_results")) != digest(expected_cases)
                        or digest(attempt.get("control_failures")) != digest(expected_failures)
                        or digest(attempt.get("coverage_failures")) != digest(expected_coverage)):
                    raise RefinerError(f"{suite} plan-control evidence differs from the complete recomputed controls: {path.name}")
                attempts.append(attempt)
        latest_matching = [row for row in attempts
                           if row.get("plan_sha256") == digest(plan) and row.get("status") == "passed"]
        if not latest_matching:
            raise RefinerError(f"{suite} plan has no passed full-control attempt evidence")
        recomputed = validate_plan(plan, root, max_cases=30)
        if digest(summary.get("case_control_results")) != digest(recomputed.get("case_control_results")):
            raise RefinerError(f"{suite} current control summary differs from the full recomputed controls")


def _verify_plan_review_artifacts(root, plans):
    from .pipeline import validate_plan_review

    for suite, plan in plans.items():
        summary = read_json(_required_file(root, f"plan-reviews/{suite}.json"))
        if (summary.get("schema_version") != "skill-refiner.plan-review/v1"
                or summary.get("suite") != suite or summary.get("status") != "passed"
                or summary.get("plan_sha256") != digest(plan)
                or summary.get("plan_snapshot") != plan
                or not isinstance(summary.get("review"), dict)
                or summary["review"].get("approved") is not True):
            raise RefinerError(f"{suite} independent review does not bind to the frozen plan")
        validate_plan_review(summary["review"], plan)

        attempts_dir = root / "plan-reviews" / "attempts"
        attempts = []
        if attempts_dir.is_dir():
            for path in sorted(attempts_dir.glob(f"{suite}-*.json")):
                row = read_json(path)
                if (row.get("schema_version") != "skill-refiner.plan-review/v1"
                        or row.get("suite") != suite
                        or not isinstance(row.get("attempt"), int)
                        or row.get("plan_sha256") != digest(row.get("plan_snapshot"))
                        or not isinstance(row.get("review"), dict)):
                    raise RefinerError(f"Malformed {suite} independent-review attempt evidence: {path.name}")
                validate_plan_review(row["review"], row["plan_snapshot"])
                expected_status = "passed" if row["review"].get("approved") else "rejected"
                if row.get("status") != expected_status:
                    raise RefinerError(f"{suite} independent-review attempt status differs from its response: {path.name}")
                attempts.append(row)
        if not any(row.get("plan_sha256") == digest(plan)
                   and row.get("status") == "passed" for row in attempts):
            raise RefinerError(f"{suite} plan has no persisted approved independent review")


def _required_file(root: Path, relative: str) -> Path:
    path = root / relative
    if not path.is_file():
        raise RefinerError(f"Completion evidence missing: {path}")
    return path


def _phase(result, *, phase: str, required_cases: set[str], must_pass: bool,
           suite_sha256: str | None = None) -> list[str]:
    if not isinstance(result, dict) or not isinstance(result.get("cases"), list):
        raise RefinerError(f"{phase} evidence has no executed case records")
    if must_pass and result.get("passed") is not True:
        raise RefinerError(f"{phase} did not pass")

    engines = result.get("engines")
    if engines is not None:
        if not isinstance(engines, list) or not engines:
            raise RefinerError(f"{phase} matrix evidence has no executors")
        executor_ids = []
        for engine in engines:
            if not isinstance(engine, dict) or not isinstance(engine.get("executor"), str):
                raise RefinerError(f"{phase} has malformed executor evidence")
            executor_ids.append(engine["executor"])
            if suite_sha256 is not None and engine.get("suite_sha256") != suite_sha256:
                raise RefinerError(f"{phase} executor {engine['executor']} used a different frozen suite")
            if must_pass and engine.get("passed") is not True:
                raise RefinerError(f"{phase} failed on executor {engine['executor']}")
            engine_cases = engine.get("cases")
            if not isinstance(engine_cases, list):
                raise RefinerError(f"{phase} has no cases for executor {engine['executor']}")
            rows = {}
            for row in engine_cases:
                if not isinstance(row, dict) or not isinstance(row.get("id"), str):
                    raise RefinerError(f"{phase} has malformed case evidence on {engine['executor']}")
                if row["id"] in rows:
                    raise RefinerError(f"{phase} duplicates case {row['id']} on {engine['executor']}")
                rows[row["id"]] = row
            if not required_cases <= rows.keys():
                raise RefinerError(f"{phase} omitted cases on executor {engine['executor']}")
            for case_id, row in rows.items():
                if row.get("status") not in {"PASS", "FAIL", "ERROR"}:
                    raise RefinerError(f"{phase} contains unexecuted case {case_id} on {engine['executor']}")
                if phase == "baseline" and row.get("status") == "ERROR":
                    raise RefinerError(f"{phase} contains an execution error on {engine['executor']}")
                if must_pass and (row.get("status") != "PASS" or row.get("passed") is not True):
                    raise RefinerError(f"{phase} has a non-passing case {case_id} on {engine['executor']}")
        if len(set(executor_ids)) != len(executor_ids):
            raise RefinerError(f"{phase} contains duplicate executor records")
        return executor_ids

    rows = {row.get("id"): row for row in result["cases"] if isinstance(row, dict)}
    if not required_cases <= rows.keys():
        raise RefinerError(f"{phase} omitted required case records")
    for case_id, row in rows.items():
        if row.get("status") not in {"PASS", "FAIL", "ERROR"}:
            raise RefinerError(f"{phase} contains unexecuted case {case_id}")
        if phase == "baseline" and row.get("status") == "ERROR":
            raise RefinerError(f"{phase} contains an execution error")
        if must_pass and (row.get("status") != "PASS" or row.get("passed") is not True):
            raise RefinerError(f"{phase} has a non-passing case {case_id}")
    return []


def _verify_pipeline(root: Path, *, expected_status: set[str]) -> dict:
    state = read_json(_required_file(root, "state.json"))
    if state.get("status") not in expected_status:
        raise RefinerError(f"Run status is {state.get('status')!r}; a fully accepted status is required")
    try:
        verify_stage_manifest(root, mode="refine", run_status=state["status"],
                              allow_active_verification=True)
    except (ValueError, OSError, KeyError, TypeError) as exc:
        raise RefinerError(f"Stage contract verification failed: {exc}") from exc
    _required_file(root, "report.md")
    plan = read_json(_required_file(root, "test-plan.json"))
    acceptance = read_json(_required_file(root, "acceptance-plan.json"))
    inputs = read_json(_required_file(root, "inputs.json"))
    lock = read_json(_required_file(root, "oracle.lock.json"))
    if lock.get("sha256") != digest({"plan": plan, "acceptance": acceptance, "inputs": inputs}):
        raise RefinerError("Frozen requirements or tests do not match their lock")
    if not isinstance(plan.get("cases"), list) or not plan["cases"]:
        raise RefinerError("Development plan contains no executable cases")
    if not isinstance(acceptance.get("cases"), list) or not acceptance["cases"]:
        raise RefinerError("Acceptance plan contains no executable cases")
    if plan.get("schema_version") in {"skill-refiner.plan/v4", "skill-refiner.plan/v5"}:
        if (acceptance.get("schema_version") != plan.get("schema_version")
                or acceptance.get("stage_contracts") != plan.get("stage_contracts")):
            raise RefinerError("Independent acceptance changed the frozen Skill stage/schema contract")
    if (plan.get("schema_version") == "skill-refiner.plan/v5"
            and acceptance.get("execution_plan") != plan.get("execution_plan")):
        raise RefinerError("Independent acceptance changed the frozen execution plan")
    if acceptance.get("coverage_gaps") or plan.get("coverage_gaps"):
        raise RefinerError("A completed run cannot contain unresolved coverage gaps")
    _verify_phase_history(state)
    _verify_plan_control_artifacts(root, {"development": plan, "acceptance": acceptance})
    _verify_plan_review_artifacts(root, {"development": plan, "acceptance": acceptance})

    source = Path(state.get("source", "")).resolve()
    original = root / "original" / source.name
    if not source.is_dir() or not original.is_dir():
        raise RefinerError("Original Skill or immutable snapshot is missing")
    source_manifest = manifest(source)
    if source_manifest != manifest(original) or source_manifest != lock.get("source_manifest"):
        raise RefinerError("Original Skill changed or does not match the frozen baseline")

    final = Path(state.get("final_skill", "")).resolve()
    if not (final / "SKILL.md").is_file() or not final.is_dir():
        raise RefinerError("Accepted candidate Skill is missing")
    if plan.get("schema_version") in {"skill-refiner.plan/v4", "skill-refiner.plan/v5"}:
        try:
            validate_skill_schema_package(final, plan["stage_contracts"])
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise RefinerError(f"Delivered Skill schema/validator package failed verification: {exc}") from exc
    evidence = read_json(_required_file(root, "final/evidence.json"))
    candidate_manifest = manifest(final)
    if evidence.get("candidate_manifest") != candidate_manifest:
        raise RefinerError("Candidate Skill no longer matches accepted evidence")
    if evidence.get("oracle_hash") != lock.get("sha256"):
        raise RefinerError("Accepted evidence refers to a different frozen test suite")
    acceptance_target = read_json(_required_file(root, "acceptance/target.json"))
    acceptance_candidate_ref = Path(acceptance_target.get("candidate", ""))
    acceptance_candidate = (root / acceptance_candidate_ref).resolve()
    if (acceptance_target.get("candidate_manifest") != candidate_manifest
            or acceptance_target.get("candidate_sha256") != digest(candidate_manifest)
            or acceptance_target.get("suite_sha256") != digest(acceptance["cases"])
            or acceptance_candidate_ref.is_absolute()
            or not acceptance_candidate.is_relative_to(root)
            or not acceptance_candidate.is_dir()
            or manifest(acceptance_candidate) != candidate_manifest):
        raise RefinerError("Independent acceptance was not run against the delivered candidate and frozen suite")

    baseline_cases = {case["id"] for case in plan["cases"] if isinstance(case, dict) and isinstance(case.get("id"), str)}
    regression_cases = baseline_cases
    acceptance_cases = {case["id"] for case in acceptance["cases"] if isinstance(case, dict) and isinstance(case.get("id"), str)}
    if not baseline_cases or not acceptance_cases:
        raise RefinerError("Frozen plans contain no valid case identifiers")
    baseline_executors = _phase(evidence.get("baseline"), phase="baseline", required_cases=baseline_cases,
                                must_pass=False, suite_sha256=digest(plan["cases"]))
    executors = _phase(evidence.get("regression"), phase="regression", required_cases=regression_cases,
                       must_pass=True, suite_sha256=digest(plan["cases"]))
    acceptance_executors = _phase(evidence.get("acceptance"), phase="acceptance", required_cases=acceptance_cases,
                                  must_pass=True, suite_sha256=digest(acceptance["cases"]))
    if executors != acceptance_executors or baseline_executors != executors:
        raise RefinerError("Baseline, regression and independent acceptance used different executors")
    _required_file(root, "final/changes.patch")

    backend_config = root / "backend-config.json"
    if backend_config.is_file():
        configured = read_json(backend_config).get("executors", [])
        expected_executors = [entry.get("id") for entry in configured if isinstance(entry, dict)]
        if expected_executors and executors != expected_executors:
            raise RefinerError("Not every configured executor passed regression and acceptance")
    else:
        backends = state.get("backends")
        configured = backends.get("executors", []) if isinstance(backends, dict) else []
        expected_executors = [entry.get("id") for entry in configured if isinstance(entry, dict)]
        if expected_executors and executors != expected_executors:
            raise RefinerError("Not every configured executor passed regression and acceptance")

    expected_final = (root / "final" / source.name).resolve()
    if final != expected_final:
        raise RefinerError("Accepted candidate is outside this run's final directory")

    return {"status": "verified", "run_status": state["status"], "run_dir": str(root),
            "final_skill": str(final), "executors": executors,
            "development_cases": sorted(baseline_cases), "acceptance_cases": sorted(acceptance_cases),
            "evidence": str((root / "final/evidence.json").resolve())}


def verify_run(path) -> dict:
    """Verify `run` or `create` evidence; reject partial, edited or failed runs."""
    root = Path(path).resolve()
    state = read_json(_required_file(root, "state.json"))
    if state.get("mode") == "create":
        if state.get("status") != "created" or state.get("refinement_status") not in {"passed", "unchanged"}:
            raise RefinerError("Creation did not complete every refinement gate")
        try:
            verify_stage_manifest(root, mode="create", run_status=state["status"],
                                  allow_active_verification=True)
        except (ValueError, OSError, KeyError, TypeError) as exc:
            raise RefinerError(f"Creation stage contract verification failed: {exc}") from exc
        refinement_root = root / "refinement"
        verified = _verify_pipeline(refinement_root, expected_status={"passed", "unchanged"})
        create_final = Path(state.get("final_skill", "")).resolve()
        expected_create_final = (root / "final" / Path(verified["final_skill"]).name).resolve()
        if create_final != expected_create_final:
            raise RefinerError("Created Skill is outside this creation run's final directory")
        if not create_final.is_dir() or manifest(create_final) != manifest(Path(verified["final_skill"])):
            raise RefinerError("Created Skill differs from the fully accepted refinement candidate")
        create_evidence_path = _required_file(root, "final/evidence.json")
        create_evidence = read_json(create_evidence_path)
        refinement_evidence = _required_file(refinement_root, "final/evidence.json")
        if create_evidence.get("refinement_evidence_sha256") != digest(read_json(refinement_evidence)):
            raise RefinerError("Creation evidence does not bind the accepted refinement record")
        _required_file(root, "final/creation.patch")
        _required_file(root, "final/changes.patch")
        _required_file(root, "report.md")
        verified.update(run_status="created", run_dir=str(root), final_skill=str(create_final),
                        evidence=str(create_evidence_path.resolve()))
        return verified
    return _verify_pipeline(root, expected_status={"passed", "unchanged"})
