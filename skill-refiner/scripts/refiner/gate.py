"""Machine-checkable finalization gate for completed Skill Refiner runs."""
from __future__ import annotations

from pathlib import Path

from .common import RefinerError, digest, manifest, read_json
from .stages import verify_stage_manifest


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
    if acceptance.get("coverage_gaps") or plan.get("coverage_gaps"):
        raise RefinerError("A completed run cannot contain unresolved coverage gaps")

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
