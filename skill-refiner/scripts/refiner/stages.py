"""Versioned, machine-readable stage contracts and artifact manifests."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import mimetypes
from pathlib import Path

from .common import write_json


def _a(path, media_type=None, glob=False):
    return {"path": path, "media_type": media_type, "glob": glob}


def _calls(role):
    return [_a(f"agent-calls/*-{role}/{name}", media_type, True)
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
        {"id": "repair", "inputs": [_a("baseline/summary.json"), _a("test-plan.json")],
         "outputs": [_a("iterations/round-*/changes.json", glob=True), _a("candidates/**", glob=True),
                     *_calls("repair")],
         "validation_gate": {"id": "evidence-bound-patch", "implementation": "REPAIR schema; apply_changes"},
         "failure_states": ["no_progress", "error"]},
        {"id": "regression", "inputs": [_a("iterations/round-*/changes.json", glob=True), _a("test-plan.json")],
         "outputs": [_a("iterations/round-*/regression/summary.json", glob=True),
                     _a("iterations/round-*/standards.json", glob=True), *_calls("standards-review")],
         "validation_gate": {"id": "same-suite-no-regressions", "implementation": "compare_candidate; qualifies"},
         "failure_states": ["no_progress", "not_converged", "error"]},
        {"id": "acceptance", "inputs": [_a("acceptance-plan.json"), _a("final/**", glob=True)],
         "outputs": [_a("acceptance/summary.json")],
         "validation_gate": {"id": "fresh-independent-acceptance", "implementation": "evaluate; all cases and engines pass"},
         "failure_states": ["failed_acceptance", "execution_error", "error"]},
        {"id": "delivery", "inputs": [_a("final/evidence.json"), _a("oracle.lock.json")],
         "outputs": [_a("final/changes.patch", "text/x-diff"), _a("report.md", "text/markdown"),
                     _a("final/**", glob=True)],
         "validation_gate": {"id": "evidence-bound-candidate", "implementation": "snapshot; digest"},
         "failure_states": ["error"]},
        {"id": "verification", "inputs": [_a("report.md", "text/markdown"), _a("final/evidence.json")],
         "outputs": [_a("verification.json")],
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
                     _a("final/changes.patch", "text/x-diff"), _a("report.md", "text/markdown"),
                     _a("test-scenarios.md", "text/markdown")],
         "validation_gate": {"id": "created-candidate-binds-refinement", "implementation": "snapshot; refinement evidence digest"},
         "failure_states": ["error"]},
        {"id": "verification", "inputs": [_a("refinement/verification.json"), _a("final/evidence.json")],
         "outputs": [_a("verification.json")],
         "validation_gate": {"id": "machine-creation-completion-gate", "implementation": "verify_run"},
         "failure_states": ["verification_failed", "error"]},
    ],
}


def _now():
    return datetime.now(timezone.utc).isoformat()


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
        results, missing = [], []
        for declaration in declarations:
            pattern = declaration["path"]
            paths = sorted(self.root.glob(pattern)) if declaration.get("glob") else [self.root / pattern]
            existing = [path for path in paths if path.is_file()]
            if not existing:
                missing.append(pattern)
            for path in existing:
                data = path.read_bytes()
                media_type = declaration.get("media_type")
                if media_type is None:
                    media_type = {".md": "text/markdown", ".py": "text/x-python",
                                  ".sh": "text/x-shellscript", ".txt": "text/plain"}.get(path.suffix)
                if media_type is None:
                    media_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
                results.append({"path": path.relative_to(self.root).as_posix(),
                                "media_type": media_type, "bytes": len(data),
                                "sha256": hashlib.sha256(data).hexdigest()})
        return results, missing

    def _refresh(self, stage):
        stage["artifacts"], stage["missing_outputs"] = self._artifacts(stage["outputs"])
        stage["input_artifacts"], stage["missing_inputs"] = self._artifacts(stage["inputs"])

    def complete(self, stage_id):
        stage = next(row for row in self.data["stages"] if row["id"] == stage_id)
        stage["status"] = "completed"
        stage["completed_at"] = _now()
        self._refresh(stage)
        if self.data.get("active_stage") == stage_id:
            self.data["active_stage"] = None
        self._save()

    def activate(self, stage_id):
        for row in self.data["stages"]:
            if row["id"] == self.data.get("active_stage") and row["status"] == "running":
                row["status"] = "completed"
                row["completed_at"] = _now()
                self._refresh(row)
        stage = next(row for row in self.data["stages"] if row["id"] == stage_id)
        if stage["status"] != "completed":
            stage["status"] = "running"
            stage["started_at"] = stage["started_at"] or _now()
            self.data["active_stage"] = stage_id
        self._refresh(stage)
        self._save()

    def finish(self, status):
        self.data["status"] = status
        active = self.data.get("active_stage")
        for stage in self.data["stages"]:
            if stage["id"] == active and stage["status"] == "running":
                stage["status"] = "completed" if status in {"passed", "unchanged", "created"} else "blocked"
                stage["completed_at"] = _now()
            elif stage["status"] == "pending":
                stage["status"] = "skipped"
            self._refresh(stage)
        self.data["active_stage"] = None
        self._save()


REFINE_EVENTS = {"planning": "planning", "coverage_gap": "planning", "baseline": "baseline",
                 "repairing": "repair", "focused": "regression", "regression": "regression",
                 "acceptance": "acceptance", "passed": "delivery", "unchanged": "delivery"}
CREATE_EVENTS = {"analyzing": "analysis", "resuming": "analysis", "needs_clarification": "analysis",
                 "reusing_draft": "draft", "creating": "draft", "refining": "refinement",
                 "created": "delivery"}
