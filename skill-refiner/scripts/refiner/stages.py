"""Versioned, machine-readable stage contracts and artifact manifests."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import fnmatch
import mimetypes
from pathlib import Path

from .common import write_json


def _a(path, media_type=None, glob=False, required=True):
    return {"path": path, "media_type": media_type, "glob": glob, "required": required}


def _calls(role):
    # Role-call evidence is indexed and hash-checked when present. It is optional
    # in a new run when the plan or draft is explicitly reused from an earlier run.
    return [_a(f"agent-calls/*-{role}/{name}", media_type, True, required=False)
            for name, media_type in (("request.json", "application/json"),
                                     ("request-manifest.json", "application/json"),
                                     ("transmission.txt", "text/plain"),
                                     ("response.json", "application/json"))]


CONTRACTS = {
    "refine": [
        {"id": "inputs", "inputs": [], "outputs": [_a("inputs.json"), _a("original/**", glob=True)],
         "validation_gate": {"id": "immutable-input-snapshot", "implementation": "snapshot; manifest"},
         "failure_states": ["error"]},
        {"id": "planning", "inputs": [_a("inputs.json"), _a("original/**", glob=True)],
         "outputs": [_a("test-plan.json"), _a("acceptance-plan.json"), _a("oracle.lock.json"),
                     *_calls("plan"), *_calls("acceptance-plan"), *_calls("plan-review"),
                    ],
         "validation_gate": {"id": "reviewed-frozen-plans", "implementation": "validate_plan; validate_plan_review; oracle lock"},
         "failure_states": ["coverage_gap", "error"]},
        {"id": "baseline", "inputs": [_a("test-plan.json"), _a("original/**", glob=True)],
         "outputs": [_a("baseline/summary.json"), _a("baseline/standards.json"), *_calls("standards-review")],
         "validation_gate": {"id": "baseline-observed", "implementation": "evaluate; standards_review; reject execution ERROR"},
         "failure_states": ["execution_error", "error"]},
        {"id": "repair", "inputs": [_a("baseline/summary.json"), _a("test-plan.json"),
                                       _a("acceptance/attempts/*/evaluation/summary.json",
                                          glob=True, required=False)],
         "outputs": [_a("iterations/round-*/changes.json", glob=True), _a("candidates/**", glob=True),
                     *_calls("repair")],
         "validation_gate": {"id": "evidence-bound-patch", "implementation": "REPAIR schema; apply_changes"},
         "failure_states": ["no_progress", "error"]},
        {"id": "regression", "inputs": [_a("iterations/round-*/changes.json", glob=True), _a("test-plan.json")],
         "outputs": [_a("iterations/round-*/regression/summary.json", glob=True),
                     _a("iterations/round-*/standards.json", glob=True), *_calls("standards-review")],
         "validation_gate": {"id": "same-suite-no-regressions", "implementation": "compare_candidate; qualifies"},
         "failure_states": ["no_progress", "not_converged", "error"]},
        {"id": "acceptance", "inputs": [_a("acceptance-plan.json"), _a("acceptance/target.json")],
         "outputs": [_a("acceptance/summary.json"), _a("acceptance/attempts/**", glob=True, required=False)],
         "validation_gate": {"id": "fresh-independent-acceptance", "implementation": "evaluate; failures return to evidence-bound repair and regression"},
         "failure_states": ["failed_acceptance", "execution_error", "error"]},
        {"id": "delivery", "inputs": [_a("final/evidence.json"), _a("oracle.lock.json")],
         "outputs": [_a("final/changes.patch", "text/x-diff"), _a("final/**", glob=True)],
         "validation_gate": {"id": "evidence-bound-candidate", "implementation": "snapshot; digest"},
         "failure_states": ["error"]},
        {"id": "verification", "inputs": [_a("final/evidence.json")],
         "outputs": [_a("verification.json"), _a("report.md", "text/markdown")],
         "validation_gate": {"id": "machine-completion-gate", "implementation": "verify_run"},
         "failure_states": ["verification_failed", "error"]},
    ],
    "create": [
        {"id": "inputs", "inputs": [], "outputs": [_a("inputs.json"), _a("inputs/**", "text/plain", True)],
         "validation_gate": {"id": "frozen-business-inputs", "implementation": "input hash; integrity"},
         "failure_states": ["error"]},
        {"id": "analysis", "inputs": [_a("inputs.json"), _a("inputs/**", "text/plain", True)],
         "outputs": [_a("analysis.json"), _a("analysis-review.json"), _a("business-analysis.md", "text/markdown"),
                     _a("business.lock.json"), *_calls("analyze"), *_calls("analysis-review")],
         "validation_gate": {"id": "grounded-reviewed-business-contract", "implementation": "validate_analysis; validate_analysis_review"},
         "failure_states": ["needs_clarification", "error"]},
        {"id": "draft", "inputs": [_a("analysis.json"), _a("business.lock.json")],
         "outputs": [_a("draft-manifest.json"), _a("drafts/**", glob=True), *_calls("draft")],
         "validation_gate": {"id": "static-valid-first-draft", "implementation": "lint; manifest"},
         "failure_states": ["error"]},
        {"id": "refinement", "inputs": [_a("draft-manifest.json"), _a("drafts/**", glob=True)],
         "outputs": [_a("refinement/report.md", "text/markdown"), _a("refinement/final/evidence.json")],
         "validation_gate": {"id": "child-refinement-verified", "implementation": "refine; verify_run"},
         "failure_states": ["coverage_gap", "not_converged", "failed_acceptance", "error"]},
        {"id": "delivery", "inputs": [_a("refinement/final/evidence.json"), _a("business.lock.json")],
         "outputs": [_a("final/evidence.json"), _a("final/creation.patch", "text/x-diff"),
                     _a("final/changes.patch", "text/x-diff"),
                     _a("test-scenarios.md", "text/markdown")],
         "validation_gate": {"id": "created-candidate-binds-refinement", "implementation": "snapshot; refinement evidence digest"},
         "failure_states": ["error"]},
        {"id": "verification", "inputs": [_a("refinement/verification.json"), _a("final/evidence.json")],
         "outputs": [_a("verification.json"), _a("report.md", "text/markdown")],
         "validation_gate": {"id": "machine-creation-completion-gate", "implementation": "verify_run"},
         "failure_states": ["verification_failed", "error"]},
    ],
}


def _now():
    return datetime.now(timezone.utc).isoformat()


def collect_artifacts(root, declarations):
    root = Path(root).resolve()
    results, missing = [], []
    for declaration in declarations:
        pattern = declaration["path"]
        paths = sorted(root.glob(pattern)) if declaration.get("glob") else [root / pattern]
        existing = [path for path in paths if path.is_file()]
        if not existing and declaration.get("required", True):
            missing.append(pattern)
        for path in existing:
            data = path.read_bytes()
            media_type = declaration.get("media_type")
            if media_type is None:
                media_type = {".md": "text/markdown", ".py": "text/x-python",
                              ".sh": "text/x-shellscript", ".txt": "text/plain"}.get(path.suffix)
            if media_type is None:
                media_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            results.append({"path": path.relative_to(root).as_posix(),
                            "media_type": media_type, "bytes": len(data),
                            "sha256": hashlib.sha256(data).hexdigest()})
    return results, missing


class StageManifest:
    """Track stage state and hash-addressed file handoffs for one run."""

    def __init__(self, root, mode):
        self.root = Path(root).resolve()
        self.path = self.root / "stages.json"
        self.data = {"schema_version": "skill-refiner.stages/v1", "run_id": self.root.name,
                     "mode": mode, "status": "running", "active_stage": None,
                     "created_at": _now(), "updated_at": _now(),
                     "stages": [{**item, "status": "pending", "started_at": None,
                                 "completed_at": None, "artifacts": [], "missing_outputs": []}
                                for item in CONTRACTS[mode]]}
        self._save()

    def _save(self):
        self.data["updated_at"] = _now()
        write_json(self.path, self.data)

    def _artifacts(self, declarations):
        return collect_artifacts(self.root, declarations)

    def _capture_outputs(self, stage):
        stage["artifacts"], stage["missing_outputs"] = self._artifacts(stage["outputs"])

    def _capture_inputs(self, stage):
        stage["input_artifacts"], stage["missing_inputs"] = self._artifacts(stage["inputs"])

    @staticmethod
    def _assert_inputs(stage):
        if stage["missing_inputs"]:
            raise ValueError(f"Stage {stage['id']} is missing required inputs: "
                             + ", ".join(stage["missing_inputs"]))

    @staticmethod
    def _assert_outputs(stage):
        if stage["missing_outputs"]:
            raise ValueError(f"Stage {stage['id']} is missing required outputs: "
                             + ", ".join(stage["missing_outputs"]))

    def complete(self, stage_id):
        stage = next(row for row in self.data["stages"] if row["id"] == stage_id)
        self._capture_outputs(stage)
        self._assert_outputs(stage)
        stage["status"] = "completed"
        stage["completed_at"] = _now()
        self._capture_inputs(stage)
        self._assert_inputs(stage)
        if self.data.get("active_stage") == stage_id:
            self.data["active_stage"] = None
        self._save()

    def activate(self, stage_id):
        if self.data.get("active_stage") == stage_id:
            stage = next(row for row in self.data["stages"] if row["id"] == stage_id)
            if stage["status"] == "running":
                self._capture_outputs(stage)
                self._save()
                return
        for row in self.data["stages"]:
            if row["id"] == self.data.get("active_stage") and row["status"] == "running":
                self._capture_outputs(row)
                self._assert_outputs(row)
                row["status"] = "completed"
                row["completed_at"] = _now()
        stage = next(row for row in self.data["stages"] if row["id"] == stage_id)
        if stage["status"] == "completed":
            # Refinement can return from held-out acceptance to repair and regression.
            # Reopen the named stage so each later pass refreshes its artifact handoff.
            stage["status"] = "running"
            stage["completed_at"] = None
            self.data["active_stage"] = stage_id
            self._capture_inputs(stage)
            self._assert_inputs(stage)
        else:
            self._capture_inputs(stage)
            self._assert_inputs(stage)
            stage["status"] = "running"
            stage["started_at"] = stage["started_at"] or _now()
            self.data["active_stage"] = stage_id
        self._capture_outputs(stage)
        self._save()

    def finish(self, status):
        self.data["status"] = status
        active = self.data.get("active_stage")
        for stage in self.data["stages"]:
            if stage["id"] == active and stage["status"] == "running":
                successful = status in {"passed", "unchanged", "created"}
                self._capture_outputs(stage)
                if successful:
                    self._assert_outputs(stage)
                stage["status"] = "completed" if successful else "blocked"
                stage["completed_at"] = _now()
            elif stage["status"] == "pending":
                stage["status"] = "skipped"
                self._capture_inputs(stage)
                self._capture_outputs(stage)
        self.data["active_stage"] = None
        self._save()


def verify_stage_manifest(root, *, mode, run_status, allow_active_verification=False):
    """Verify stage contract states and that recorded handoff artifacts are intact."""
    root = Path(root).resolve()
    manifest_path = root / "stages.json"
    if not manifest_path.is_file():
        raise ValueError("Completion evidence missing: stages.json")
    from .common import read_json
    data = read_json(manifest_path)
    if data.get("schema_version") != "skill-refiner.stages/v1" or data.get("mode") != mode:
        raise ValueError("Stage manifest schema or mode mismatch")
    rows = data.get("stages", [])
    by_id = {stage.get("id"): stage for stage in rows}
    expected_ids = [stage["id"] for stage in CONTRACTS[mode]]
    if [stage.get("id") for stage in rows] != expected_ids:
        raise ValueError("Stage manifest does not match the versioned stage contract")

    active = data.get("active_stage")
    pre_finish = allow_active_verification and active == "verification" and by_id["verification"].get("status") == "running"
    if pre_finish:
        if data.get("status") != "running":
            raise ValueError("Stage manifest has an invalid in-progress state")
    elif data.get("status") != run_status or active is not None:
        raise ValueError("Stage manifest run status is incomplete or inconsistent")

    required = ["inputs", "planning", "baseline", "acceptance", "delivery"] if mode == "refine" else [
        "inputs", "analysis", "draft", "refinement", "delivery"]
    if mode == "refine" and run_status != "unchanged":
        required.extend(["repair", "regression"])
    for stage_id in required:
        if by_id[stage_id].get("status") != "completed":
            raise ValueError(f"Required stage {stage_id} is not completed")
    verification_status = by_id["verification"].get("status")
    if verification_status != ("running" if pre_finish else "completed"):
        raise ValueError("Verification stage is incomplete")
    if mode == "refine" and run_status == "unchanged":
        allowed = {"completed", "skipped", "pending"} if pre_finish else {"completed", "skipped"}
        if any(by_id[s].get("status") not in allowed for s in ("repair", "regression")):
            raise ValueError("Unchanged run has an invalid repair or regression stage state")

    for stage_id in expected_ids:
        stage = by_id[stage_id]
        if stage.get("status") not in {"completed", "skipped", "blocked", "running"}:
            raise ValueError(f"Stage {stage_id} has an invalid status")
        if stage.get("status") == "completed":
            if stage.get("missing_outputs") or stage.get("missing_inputs"):
                raise ValueError(f"Completed stage {stage_id} recorded missing required artifacts")
            for field in ("artifacts", "input_artifacts"):
                recorded = stage.get(field)
                if not isinstance(recorded, list):
                    raise ValueError(f"Completed stage {stage_id} has no {field} inventory")
                for artifact in recorded:
                    relative = Path(artifact.get("path", ""))
                    path = (root / relative).resolve()
                    if relative.is_absolute() or not path.is_relative_to(root) or not path.is_file():
                        raise ValueError(f"Stage {stage_id} artifact is missing or outside its run directory")
                    data_bytes = path.read_bytes()
                    if (len(data_bytes) != artifact.get("bytes")
                            or hashlib.sha256(data_bytes).hexdigest() != artifact.get("sha256")):
                        raise ValueError(f"Stage {stage_id} {field} artifact hash changed: {relative}")
            for field, declarations in (("artifacts", stage["outputs"]),
                                        ("input_artifacts", stage["inputs"])):
                paths = {item["path"] for item in stage[field]}
                for declaration in declarations:
                    if not declaration.get("required", True):
                        continue
                    pattern = declaration["path"]
                    if not any(fnmatch.fnmatch(path, pattern) for path in paths):
                        kind = "output" if field == "artifacts" else "input"
                        raise ValueError(f"Completed stage {stage_id} omitted required {kind} {pattern}")
        elif stage.get("status") == "running" and not (pre_finish and stage_id == "verification"):
            raise ValueError(f"Stage {stage_id} was left running")
    return data


REFINE_EVENTS = {"planning": "planning", "coverage_gap": "planning", "baseline": "baseline",
                 "repairing": "repair", "focused": "regression", "regression": "regression",
                 "acceptance": "acceptance", "passed": "delivery", "unchanged": "delivery"}
CREATE_EVENTS = {"analyzing": "analysis", "resuming": "analysis", "needs_clarification": "analysis",
                 "reusing_draft": "draft", "creating": "draft", "refining": "refinement",
                 "created": "delivery"}
