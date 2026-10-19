import json
from pathlib import Path
import tempfile
import unittest

from refiner.common import RefinerError, digest, manifest, snapshot, write_json
from refiner.gate import verify_run


ROOT = Path(__file__).resolve().parents[1]


def phase(rows, passed):
    return {"passed": passed, "cases": [{"id": case_id, "status": status, "passed": status == "PASS"}
                                          for case_id, status in rows]}


def matrix_phase(rows, passed, executor_ids, suite_hash):
    return {"passed": passed, "cases": [], "engines": [
        {"executor": executor_id, "passed": passed, "suite_sha256": suite_hash,
         "cases": [{"id": case_id, "status": status, "passed": status == "PASS"}
                   for case_id, status in rows]}
        for executor_id in executor_ids
    ]}


def make_pipeline(root, *, parent_status="passed"):
    source = root / "source" / "sample-skill"
    snapshot(ROOT / "examples/decimal-total", source)
    run = root / "run"
    run.mkdir()
    original = run / "original" / source.name
    snapshot(source, original)
    final = run / "final" / source.name
    snapshot(source, final)
    plan = {"cases": [{"id": "normal"}, {"id": "error"}], "coverage_gaps": []}
    acceptance = {"cases": [{"id": "fresh-boundary"}], "coverage_gaps": []}
    inputs = {"requirements": ["frozen"]}
    for name, value in (("test-plan.json", plan), ("acceptance-plan.json", acceptance), ("inputs.json", inputs)):
        write_json(run / name, value)
    oracle = digest({"plan": plan, "acceptance": acceptance, "inputs": inputs})
    write_json(run / "oracle.lock.json", {"sha256": oracle, "source_manifest": manifest(source)})
    evidence = {"oracle_hash": oracle, "candidate_manifest": manifest(final),
                "baseline": phase([("normal", "FAIL"), ("error", "PASS")], False),
                "regression": phase([("normal", "PASS"), ("error", "PASS")], True),
                "acceptance": phase([("fresh-boundary", "PASS")], True)}
    write_json(run / "final/evidence.json", evidence)
    (run / "final/changes.patch").write_text("candidate patch\n")
    (run / "report.md").write_text("Complete run report\n")
    write_json(run / "state.json", {"status": parent_status, "source": str(source), "final_skill": str(final)})
    return run


class CompletionGateTests(unittest.TestCase):
    def test_accepts_only_complete_run_with_failed_baseline_and_passing_fresh_acceptance(self):
        with tempfile.TemporaryDirectory() as temporary:
            run = make_pipeline(Path(temporary))
            result = verify_run(run)
            self.assertEqual(result["status"], "verified")
            self.assertEqual(result["run_status"], "passed")
            self.assertEqual(result["development_cases"], ["error", "normal"])
            self.assertEqual(result["acceptance_cases"], ["fresh-boundary"])

    def test_refuses_claim_when_frozen_plan_has_changed(self):
        with tempfile.TemporaryDirectory() as temporary:
            run = make_pipeline(Path(temporary))
            plan_path = run / "test-plan.json"
            plan = json.loads(plan_path.read_text())
            plan["cases"].pop()
            write_json(plan_path, plan)
            with self.assertRaisesRegex(RefinerError, "do not match their lock"):
                verify_run(run)

    def test_refuses_any_nonpassing_acceptance_case(self):
        with tempfile.TemporaryDirectory() as temporary:
            run = make_pipeline(Path(temporary))
            evidence_path = run / "final/evidence.json"
            evidence = json.loads(evidence_path.read_text())
            evidence["acceptance"] = phase([("fresh-boundary", "FAIL")], False)
            write_json(evidence_path, evidence)
            with self.assertRaisesRegex(RefinerError, "acceptance did not pass"):
                verify_run(run)

    def test_matrix_gate_requires_every_configured_executor_on_the_frozen_suite(self):
        with tempfile.TemporaryDirectory() as temporary:
            run = make_pipeline(Path(temporary))
            plan = json.loads((run / "test-plan.json").read_text())
            acceptance = json.loads((run / "acceptance-plan.json").read_text())
            evidence_path = run / "final/evidence.json"
            evidence = json.loads(evidence_path.read_text())
            evidence["baseline"] = matrix_phase([("normal", "FAIL"), ("error", "PASS")], False,
                                                  ["codex", "cursor"], digest(plan["cases"]))
            evidence["regression"] = matrix_phase([("normal", "PASS"), ("error", "PASS")], True,
                                                    ["codex", "cursor"], digest(plan["cases"]))
            evidence["acceptance"] = matrix_phase([("fresh-boundary", "PASS")], True,
                                                   ["codex", "cursor"], digest(acceptance["cases"]))
            write_json(evidence_path, evidence)
            write_json(run / "backend-config.json", {"executors": [{"id": "codex"}, {"id": "cursor"}]})

            verified = verify_run(run)
            self.assertEqual(verified["executors"], ["codex", "cursor"])

            evidence["acceptance"]["engines"][1]["suite_sha256"] = "different-suite"
            write_json(evidence_path, evidence)
            with self.assertRaisesRegex(RefinerError, "used a different frozen suite"):
                verify_run(run)

    def test_matrix_gate_rejects_omitted_configured_executor(self):
        with tempfile.TemporaryDirectory() as temporary:
            run = make_pipeline(Path(temporary))
            plan = json.loads((run / "test-plan.json").read_text())
            acceptance = json.loads((run / "acceptance-plan.json").read_text())
            evidence_path = run / "final/evidence.json"
            evidence = json.loads(evidence_path.read_text())
            evidence["baseline"] = matrix_phase([("normal", "FAIL"), ("error", "PASS")], False,
                                                  ["codex"], digest(plan["cases"]))
            evidence["regression"] = matrix_phase([("normal", "PASS"), ("error", "PASS")], True,
                                                    ["codex"], digest(plan["cases"]))
            evidence["acceptance"] = matrix_phase([("fresh-boundary", "PASS")], True,
                                                   ["codex"], digest(acceptance["cases"]))
            write_json(evidence_path, evidence)
            write_json(run / "backend-config.json", {"executors": [{"id": "codex"}, {"id": "cursor"}]})
            with self.assertRaisesRegex(RefinerError, "Not every configured executor"):
                verify_run(run)

    def test_created_run_must_bind_to_accepted_child_refinement(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "create"
            root.mkdir()
            refinement = make_pipeline(root)
            refinement.rename(root / "refinement")
            refinement = root / "refinement"
            child_state_path = refinement / "state.json"
            child_state = json.loads(child_state_path.read_text())
            child_state["final_skill"] = str((refinement / "final/sample-skill").resolve())
            write_json(child_state_path, child_state)
            child_evidence_path = refinement / "final/evidence.json"
            child_evidence = json.loads(child_evidence_path.read_text())
            created_skill = root / "final" / "sample-skill"
            snapshot(Path(child_evidence_path).parent / "sample-skill", created_skill)
            write_json(root / "final/evidence.json", {
                "refinement_evidence_sha256": digest(child_evidence),
                "refinement_evidence": str(child_evidence_path.resolve()),
            })
            (root / "final/creation.patch").write_text("created patch\n")
            (root / "final/changes.patch").write_text("repair patch\n")
            (root / "report.md").write_text("Created report\n")
            write_json(root / "state.json", {"mode": "create", "status": "created",
                       "refinement_status": "passed", "final_skill": str(created_skill)})
            result = verify_run(root)
            self.assertEqual(result["run_status"], "created")
            self.assertEqual(Path(result["final_skill"]), created_skill.resolve())

    def test_created_run_cannot_point_final_skill_outside_creation_directory(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "create"
            root.mkdir()
            refinement = make_pipeline(root)
            refinement.rename(root / "refinement")
            refinement = root / "refinement"
            child_state_path = refinement / "state.json"
            child_state = json.loads(child_state_path.read_text())
            child_state["final_skill"] = str((refinement / "final/sample-skill").resolve())
            write_json(child_state_path, child_state)
            child_evidence_path = refinement / "final/evidence.json"
            child_evidence = json.loads(child_evidence_path.read_text())
            created_skill = root / "final" / "sample-skill"
            snapshot(Path(child_evidence_path).parent / "sample-skill", created_skill)
            write_json(root / "final/evidence.json", {
                "refinement_evidence_sha256": digest(child_evidence),
                "refinement_evidence": str(child_evidence_path.resolve()),
            })
            (root / "final/creation.patch").write_text("created patch\n")
            (root / "final/changes.patch").write_text("repair patch\n")
            (root / "report.md").write_text("Created report\n")
            write_json(root / "state.json", {"mode": "create", "status": "created",
                       "refinement_status": "passed", "final_skill": str(refinement / "final/sample-skill")})
            with self.assertRaisesRegex(RefinerError, "outside this creation run"):
                verify_run(root)


if __name__ == "__main__":
    unittest.main()
