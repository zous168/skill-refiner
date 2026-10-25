import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from refiner.common import write_json
from refiner.stages import StageManifest
from refiner.common import inventory


class StageManifestTests(unittest.TestCase):
    def test_records_typed_artifact_hashes_and_terminal_stage_states(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "run-1"
            root.mkdir()
            (root / "inputs.json").write_text('{"goal":"test"}\n')
            (root / "original").mkdir()
            (root / "original/SKILL.md").write_text("# Skill\n")
            stages = StageManifest(root, "refine")
            stages.complete("inputs")
            (root / "test-plan.json").write_text('{"cases":[]}\n')
            (root / "acceptance-plan.json").write_text('{"cases":[]}\n')
            (root / "oracle.lock.json").write_text('{"sha256":"abc"}\n')
            call = root / "agent-calls/001-plan"
            call.mkdir(parents=True)
            (call / "request.json").write_text('{"schema_version":"skill-refiner.agent-request/v1"}\n')
            (call / "request-manifest.json").write_text('{"status":"completed"}\n')
            (call / "transmission.txt").write_text("request bytes")
            (call / "response.json").write_text('{"cases":[]}\n')
            stages.activate("planning")
            stages.activate("baseline")
            stages.finish("execution_error")

            data = json.loads((root / "stages.json").read_text())
            self.assertEqual(data["schema_version"], "skill-refiner.stages/v1")
            self.assertEqual(data["mode"], "refine")
            self.assertEqual(data["status"], "execution_error")
            self.assertEqual(data["stages"][0]["status"], "completed")
            original_file = next(x for x in data["stages"][0]["artifacts"] if x["path"] == "original/SKILL.md")
            self.assertEqual(original_file["media_type"], "text/markdown")
            self.assertEqual(data["stages"][1]["status"], "completed")
            self.assertEqual(data["stages"][2]["status"], "blocked")
            self.assertEqual(data["stages"][3]["status"], "skipped")
            item = next(x for x in data["stages"][1]["artifacts"] if x["path"] == "test-plan.json")
            self.assertEqual(item["media_type"], "application/json")
            self.assertEqual(item["sha256"], hashlib.sha256((root / "test-plan.json").read_bytes()).hexdigest())
            self.assertEqual(data["active_stage"], None)
            planning = data["stages"][1]
            self.assertEqual(planning["validation_gate"]["id"], "reviewed-frozen-plans")
            self.assertIn("validate_plan_review", planning["validation_gate"]["implementation"])
            self.assertIn("error", planning["failure_states"])
            self.assertIn("agent-calls/001-plan/transmission.txt",
                          {x["path"] for x in planning["artifacts"]})

    def test_creation_contract_has_business_analysis_and_reuses_refinement(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "create-1"
            root.mkdir()
            write_json(root / "inputs.json", {"business_description": "example"})
            (root / "inputs").mkdir()
            (root / "inputs/business-request.md").write_text("example\n")
            stages = StageManifest(root, "create")
            stages.complete("inputs")
            stages.activate("analysis")
            stages.finish("needs_clarification")
            data = json.loads((root / "stages.json").read_text())
            self.assertEqual([s["id"] for s in data["stages"]],
                             ["inputs", "analysis", "draft", "refinement", "delivery", "verification"])
            self.assertEqual(data["stages"][1]["status"], "blocked")
            self.assertEqual(data["stages"][2]["status"], "skipped")

    def test_source_inventory_hashes_included_and_omitted_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            included = root / "SKILL.md"
            omitted = root / "references/large.md"
            omitted.parent.mkdir()
            included.write_text("# café\n")
            omitted.write_text("x" * 40)
            rows = inventory(root, max_bytes=10)
            by_path = {row["path"]: row for row in rows}
            for path, expected in [(included, "# café\n".encode()), (omitted, b"x" * 40)]:
                self.assertEqual(by_path[path.relative_to(root).as_posix()]["sha256"],
                                 hashlib.sha256(expected).hexdigest())
                self.assertEqual(by_path[path.relative_to(root).as_posix()]["bytes"], len(expected))

    def test_required_outputs_block_stage_completion(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "run-1"
            root.mkdir()
            stages = StageManifest(root, "refine")
            with self.assertRaisesRegex(ValueError, "missing required outputs"):
                stages.complete("inputs")
            data = json.loads((root / "stages.json").read_text())
            self.assertEqual(data["stages"][0]["status"], "pending")

    def test_repeated_event_does_not_complete_a_stage_before_its_gate(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "run-1"
            root.mkdir()
            (root / "inputs.json").write_text("{}\n")
            (root / "original").mkdir()
            (root / "original/SKILL.md").write_text("# Skill\n")
            stages = StageManifest(root, "refine")
            stages.complete("inputs")
            stages.activate("planning")
            stages.activate("planning")
            data = json.loads((root / "stages.json").read_text())
            self.assertEqual(data["stages"][1]["status"], "running")

    def test_acceptance_failure_can_reopen_repair_regression_and_acceptance_stages(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "run-acceptance-loop"
            root.mkdir()
            (root / "inputs.json").write_text("{}\n")
            (root / "original").mkdir()
            (root / "original/SKILL.md").write_text("# Skill\n")
            (root / "test-plan.json").write_text('{"cases":[]}\n')
            (root / "acceptance-plan.json").write_text('{"cases":[]}\n')
            (root / "oracle.lock.json").write_text('{"sha256":"test"}\n')
            stages = StageManifest(root, "refine")
            stages.complete("inputs")
            (root / "baseline").mkdir()
            (root / "baseline/summary.json").write_text('{"cases":[]}\n')
            (root / "baseline/standards.json").write_text('{"findings":[]}\n')
            stages.activate("planning")
            stages.activate("baseline")
            stages.complete("baseline")
            (root / "acceptance/attempts/initial/evaluation").mkdir(parents=True)
            (root / "acceptance/attempts/initial/evaluation/summary.json").write_text('{"passed":false}\n')
            (root / "iterations/round-1").mkdir(parents=True)
            (root / "iterations/round-1/changes.json").write_text('{"changes":[]}\n')
            (root / "candidates/1/skill").mkdir(parents=True)
            (root / "candidates/1/skill/SKILL.md").write_text("# Candidate\n")
            (root / "iterations/round-1/regression").mkdir()
            (root / "iterations/round-1/regression/summary.json").write_text('{"cases":[]}\n')
            (root / "iterations/round-1/standards.json").write_text('{"findings":[]}\n')
            stages.activate("repair")
            stages.activate("regression")
            (root / "acceptance").mkdir(exist_ok=True)
            (root / "acceptance/target.json").write_text('{}\n')
            (root / "acceptance/summary.json").write_text('{"passed":false}\n')
            stages.activate("acceptance")
            stages.complete("acceptance")
            (root / "acceptance/attempts/round-1/evaluation").mkdir(parents=True)
            (root / "acceptance/attempts/round-1/evaluation/summary.json").write_text('{"passed":false}\n')
            stages.activate("repair")
            data = json.loads((root / "stages.json").read_text())
            by_id = {stage["id"]: stage for stage in data["stages"]}
            self.assertEqual(data["active_stage"], "repair")
            self.assertEqual(by_id["repair"]["status"], "running")
            self.assertEqual(by_id["acceptance"]["status"], "completed")
            repair_inputs = {item["path"] for item in by_id["repair"]["input_artifacts"]}
            self.assertIn("acceptance/attempts/initial/evaluation/summary.json", repair_inputs)
            self.assertIn("acceptance/attempts/round-1/evaluation/summary.json", repair_inputs)


if __name__ == "__main__":
    unittest.main()
