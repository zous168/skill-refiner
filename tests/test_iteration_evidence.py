import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
import zipfile

from refiner.common import RefinerError
from refiner.evidence import archive_diagnostics, failure_bundle, repair_evidence, workspace_paths
from refiner.pipeline import compare_candidate, qualifies, validate_plan_review


class IterationEvidenceTests(unittest.TestCase):
    def test_new_workspace_diagnostics_survive_cleanup_without_copying_installed_inputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root/"SKILL.md").write_text("original installed instructions")
            before = workspace_paths(root)
            (root/"workflow/agent-calls/001-plan").mkdir(parents=True)
            raw = b'{"coverage_gaps":["missing fixture"]}'
            (root/"workflow/agent-calls/001-plan/response.json").write_bytes(raw)
            (root/"workflow/state.json").write_text('{"status":"error"}')
            artifact = archive_diagnostics(root, before)
            self.assertFalse(Path(artifact["path"]).is_absolute(), "skill-up only accepts workspace artifact paths")
            path = root / artifact["path"]
            try:
                with zipfile.ZipFile(path) as archive:
                    self.assertNotIn("SKILL.md", archive.namelist())
                    self.assertEqual(archive.read("workflow/agent-calls/001-plan/response.json"), raw)
                    index = json.loads(archive.read("refiner-diagnostic-index.json"))
                    entry = next(x for x in index["files"] if x["path"].endswith("response.json"))
                    self.assertEqual(entry["sha256"], hashlib.sha256(raw).hexdigest())
            finally:
                path.unlink()

    def test_partial_repair_survives_honest_unresolved_report_without_final_qualification(self):
        baseline = {"cases": [{"id": "codex::a", "status": "FAIL", "passed": False},
                              {"id": "native::a", "status": "FAIL", "passed": False}]}
        improved = copy.deepcopy(baseline)
        improved["cases"][0].update(status="PASS", passed=True)
        accepted, regressed = compare_candidate(set(), baseline, [], improved, [], [], ["native case remains"])
        self.assertTrue(accepted)
        self.assertEqual(regressed, [])
        self.assertFalse(qualifies(improved, [], ["native case remains"]))
        complete = copy.deepcopy(improved)
        complete["cases"][1].update(status="PASS", passed=True)
        self.assertFalse(qualifies(complete, [], ["unverified required behavior"]))
        self.assertTrue(qualifies(complete, []))
        self.assertFalse(compare_candidate(set(), improved, [], complete,
                         [{"severity": "error"}], ["native case remains"], [])[0])
        self.assertFalse(compare_candidate(set(), improved, [], baseline, [], ["native case remains"], [])[0])

    def test_plan_approval_cannot_hide_conflicting_or_missing_fixture_assessments(self):
        plan = {"cases": [{"id": "normal"}, {"id": "boundary"}]}
        review = {"approved": True, "issues": [], "case_contracts": [
            {"case_id": name, "installed_source_unchanged": True, "fixture_contract_consistent": True,
             "response_contract_consistent": True, "trace_checks_observable": True,
             "basis": "Read-only helper consumes workspace input; candidate outputs are separate."}
            for name in ["normal", "boundary"]]}
        validate_plan_review(review, plan)
        broken = copy.deepcopy(review)
        broken["case_contracts"][0]["fixture_contract_consistent"] = False
        with self.assertRaisesRegex(RefinerError, "conflicting"):
            validate_plan_review(broken, plan)
        for field in ["response_contract_consistent", "trace_checks_observable"]:
            invalid = copy.deepcopy(review)
            invalid["case_contracts"][0][field] = False
            with self.assertRaisesRegex(RefinerError, "conflicting"):
                validate_plan_review(invalid, plan)
        broken["approved"] = False
        broken["issues"] = ["Fixture demands changing its installed source"]
        validate_plan_review(broken, plan)
        broken["case_contracts"][1]["case_id"] = "normal"
        with self.assertRaisesRegex(RefinerError, "exactly once"):
            validate_plan_review(broken, plan)

    def test_archived_failure_context_is_scoped_bounded_and_hash_identified(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run = root / "iteration-1/failed-case/with_skill/outputs/agent/run"
            run.mkdir(parents=True)
            raw = json.dumps({"requirements": ["R1"], "coverage_gaps": ["specific missing fixture"],
                              "cases": []}).encode()
            with zipfile.ZipFile(run/"diagnostics.zip", "w") as archive:
                archive.writestr("workflow/agent-calls/003-plan/response.json", raw)
                archive.writestr("workflow/state.json", '{"status":"error"}')
                archive.writestr("workflow/agent-calls/004-plan-review/request.json", json.dumps({
                    "role": "plan-review", "input": {"plan": {"important": "rejected plan"},
                    "files": [{"content": "source repeated" * 10000}]}}))
                archive.writestr("../../state.json", "TRAVERSAL_MUST_NOT_LOAD")
                archive.writestr("evals/acceptance.oracle.json", "ORACLE_MUST_NOT_LOAD")
            held_out = root / "iteration-1/held-out/with_skill"
            held_out.mkdir(parents=True)
            (held_out/"state.json").write_text("HELD_OUT_MUST_NOT_LOAD")
            bundle = failure_bundle(root, "failed-case", 3000)
            encoded = json.dumps(bundle)
            self.assertIn("specific missing fixture", encoded)
            self.assertIn("rejected plan", encoded)
            for forbidden in ["TRAVERSAL_MUST_NOT_LOAD", "ORACLE_MUST_NOT_LOAD", "HELD_OUT_MUST_NOT_LOAD", "source repeated"]:
                self.assertNotIn(forbidden, encoded)
            record = next(r for r in bundle["records"] if r["source"].endswith("003-plan/response.json"))
            self.assertEqual(record["sha256"], hashlib.sha256(raw).hexdigest())
            pair = repair_evidence([{"id": "native::failed-case", "case_id": "failed-case",
                                     "evidence_directory": str(root)}])
            self.assertEqual(pair[0]["evidence_id"], "native::failed-case")
            self.assertFalse(failure_bundle(root, "../held-out")["records"])


if __name__ == "__main__":
    unittest.main()
