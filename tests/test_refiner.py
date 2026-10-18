import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from refiner.checks import grade, validate_plan
from refiner.checks import check_one
from refiner.common import RefinerError, manifest, snapshot
from refiner.pipeline import apply_changes, compare_candidate
from refiner.standards import lint

ROOT = Path(__file__).resolve().parents[1]


def get_plan():
    result = subprocess.run([sys.executable, str(ROOT/"tests/fixtures/protocol_agent.py")],
                            input=json.dumps({"role": "plan", "input": {}}), text=True, capture_output=True, check=True)
    return json.loads(result.stdout)


class ContractTests(unittest.TestCase):
    def test_reject_bad_oracle_and_missing_coverage(self):
        plan = get_plan()
        with tempfile.TemporaryDirectory() as tmp:
            validate_plan(plan, tmp)
            weak = copy.deepcopy(plan)
            weak["cases"][0]["bad_response"] = weak["cases"][0]["good_response"]
            with self.assertRaisesRegex(RefinerError, "Bad-response"):
                validate_plan(weak, tmp)
            weak = copy.deepcopy(plan)
            weak["requirements"].append({"id": "R-uncovered", "behavior": "uncovered", "source": "spec", "required": True})
            with self.assertRaisesRegex(RefinerError, "Uncovered"):
                validate_plan(weak, tmp)

    def test_preserve_original_and_protect_tests_and_escape(self):
        with tempfile.TemporaryDirectory() as tmp:
            candidate = Path(tmp)/"candidate"
            snapshot(ROOT/"examples/decimal-total", candidate)
            before = manifest(candidate)
            for path in ["../outside.py", "evals/case.yaml", "/tmp/outside.py"]:
                with self.assertRaises(RefinerError):
                    apply_changes(candidate, {"changes": [{"path": path, "content": "x", "reason": "reason", "evidence_ids": ["case"]}]}, {"case"})
            self.assertEqual(before, manifest(candidate))
            with self.assertRaises(RefinerError):
                apply_changes(candidate, {"changes": [{"path": "SKILL.md", "content": "x", "reason": "reason", "evidence_ids": ["invented"]}]}, {"case"})

    def test_real_content_grading_not_filename_only(self):
        case = get_plan()["cases"][0]
        case["checks"] = [c for c in case["checks"] if not c["type"].startswith("tool_")]
        self.assertTrue(grade(case, '{"total":"0.33"}', ROOT)["passed"])
        self.assertFalse(grade(case, '{"total":"0.00"}', ROOT)["passed"])
        self.assertFalse(grade(case, "I did it correctly", ROOT)["passed"])

    def test_command_claim_is_not_execution_evidence(self):
        case = {"id": "command", "checks": [{"type": "tool_command", "value": "python.*total.py"}]}
        self.assertFalse(grade(case, "I executed python scripts/total.py", ROOT)["passed"])

    def test_manual_correction_of_wrong_tool_output_is_rejected(self):
        check = {"type": "tool_output_matches", "value": "python.*total.py"}
        trace = [{"role": "tool_call", "tool_call": {"id": "c1", "name": "command_execution", "arguments": {"command": "python3 scripts/total.py"}}},
                 {"role": "tool_result", "tool_result": {"call_id": "c1", "status": "success", "content": '{"total":"0.00"}'}}]
        self.assertFalse(check_one(check, '{"total":"0.33"}', ROOT, trace)[0])
        self.assertTrue(check_one(check, '{"total":"0.00"}', ROOT, trace)[0])
        trace[1]["tool_result"]["call_id"] = "another-call"
        self.assertFalse(check_one(check, '{"total":"0.00"}', ROOT, trace)[0])

    def test_codex_rollout_wrapped_command_arguments(self):
        check = {"type": "tool_output_matches", "value": "python.*total.py"}
        trace = [{"role": "tool_call", "tool_call": {"id": "c", "name": "exec_command",
                   "arguments": {"arguments": json.dumps({"cmd": "python3 scripts/total.py"})}}},
                 {"role": "tool_result", "tool_result": {"call_id": "c", "status": "success",
                  "content": 'Chunk ID: abc\nProcess exited with code 0\nFinal output:\n{"total":"0.33"}'}}]
        self.assertTrue(check_one(check, '{"total":"0.33"}', ROOT, trace)[0])

    def test_multiple_complete_tool_outputs_can_be_forwarded_in_call_order(self):
        check = {"type": "tool_output_matches", "value": r"python3 scripts/merge\.py"}
        trace = [
            {"role": "tool_call", "tool_call": {"id": "touch", "name": "command_execution",
                "arguments": {"command": "python3 scripts/merge.py touching.json"}}},
            {"role": "tool_call", "tool_call": {"id": "empty", "name": "command_execution",
                "arguments": {"command": "python3 scripts/merge.py empty.json"}}},
            {"role": "tool_result", "tool_result": {"call_id": "empty", "status": "success", "content": "[]\n"}},
            {"role": "tool_result", "tool_result": {"call_id": "touch", "status": "success", "content": "[[-3, 7]]\n"}},
        ]
        self.assertTrue(check_one(check, "[[-3, 7]]\n[]", ROOT, trace)[0])
        self.assertFalse(check_one(check, "[]\n[[-3, 7]]", ROOT, trace)[0])
        self.assertFalse(check_one(check, "[[-3, 8]]\n[]", ROOT, trace)[0])
        self.assertFalse(check_one(check, "[[-3, 7]]\n[]\n[]", ROOT, trace)[0])

    def test_combined_tool_outputs_require_all_successful_matching_call_ids(self):
        check = {"type": "tool_output_matches", "value": r"python3 scripts/merge\.py"}
        trace = []
        for call_id, filename, output in [("touch", "touching.json", "[[-3, 7]]\n"), ("empty", "empty.json", "[]\n")]:
            trace += [{"role": "tool_call", "tool_call": {"id": call_id, "name": "command_execution",
                        "arguments": {"command": "python3 scripts/merge.py " + filename}}},
                      {"role": "tool_result", "tool_result": {"call_id": call_id, "status": "success", "content": output}}]
        answer = "[[-3, 7]]\n[]"
        self.assertTrue(check_one(check, answer, ROOT, trace)[0])
        for field, value in [("call_id", "unrelated"), ("status", "error")]:
            invalid = copy.deepcopy(trace)
            invalid[-1]["tool_result"][field] = value
            self.assertFalse(check_one(check, answer, ROOT, invalid)[0])
        self.assertFalse(check_one(check, answer, ROOT, trace[:-1])[0])

    def test_platform_shell_quotes_do_not_invalidate_actual_invocation(self):
        check = {"type": "tool_output_matches", "value": r"\bpython3\s+\S*scripts/total\.py\s+ledger\.json(?:\s|$)"}
        trace = [{"role": "tool_call", "tool_call": {"id": "c", "name": "command_execution",
                 "arguments": {"command": "/bin/zsh -lc 'python3 .codex/skills/decimal-total/scripts/total.py ledger.json'"}}},
                 {"role": "tool_result", "tool_result": {"call_id": "c", "status": "success", "content": '{"total":"-1.10"}'}}]
        self.assertTrue(check_one(check, '{"total":"-1.10"}', ROOT, trace)[0])
        self.assertFalse(check_one(check, '{"total":"0.00"}', ROOT, trace)[0])
        self.assertFalse(check_one(check, '{"total":"0.99"}', ROOT, trace)[0])
        trace[1]["tool_result"]["content"] = '{"amounts":["0.11","0.22"]}\n{"total":"0.33"}\n'
        self.assertTrue(check_one(check, '{"total":"0.33"}', ROOT, trace)[0])

    def test_lint_rejects_empty_description_and_script_syntax(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp)
            (p/"SKILL.md").write_text('---\nname: test\ndescription: ""\n---\n')
            (p/"broken.py").write_text('def broken(:')
            rules = {f["rule"] for f in lint(p)}
            self.assertIn("format.description", rules)
            self.assertIn("script.syntax", rules)

    def test_reject_regression_even_when_total_pass_count_improves(self):
        best = {"cases": [{"id": "fixed", "passed": True, "status": "PASS"},
                           {"id": "b", "passed": False, "status": "FAIL"},
                           {"id": "c", "passed": False, "status": "FAIL"}]}
        current = {"cases": [{"id": "fixed", "passed": False, "status": "FAIL"},
                              {"id": "b", "passed": True, "status": "PASS"},
                              {"id": "c", "passed": True, "status": "PASS"}]}
        accepted, regressions = compare_candidate(set(), best, [], current, [])
        self.assertFalse(accepted)
        self.assertEqual(regressions, ["fixed"])


class SkillUpIntegration(unittest.TestCase):
    @unittest.skipUnless((ROOT/".tools/skill-up").exists(), "Run skill-refiner bootstrap first")
    def run_pipeline(self, acceptance_failure=False):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            engine = {"name": "refiner-test-script", "custom": {"transport": "local", "response_format": "session_result",
                      "local": {"command": sys.executable, "args": [str(ROOT/"tests/fixtures/script_engine.py"),
                                 "${input_file}", "${output_file}"], "input_file": "${input_file}", "output_file": "${output_file}"}}}
            (tmp/"engine.json").write_text(json.dumps(engine))
            target = tmp/"source"/"decimal-total"
            snapshot(ROOT/"examples/decimal-total", target)
            # Include a real existing native suite and prove it remains a passing regression gate.
            (target/"evals/cases").mkdir(parents=True)
            (target/"evals/cases/whole.yaml").write_text(json.dumps({"id": "whole", "input": {"prompt": "Use script on input.json"},
                "context": {"files": {"input.json": '{"amounts":["2","3"]}'}},
                "judge": {"type": "rule_based", "success": [{"output_contains": {"all": ["5.00"]}}]}}))
            (target/"evals/eval.yaml").write_text(json.dumps({"schema_version": "v1alpha1", "environment": {"type": "none"},
                "engine": engine, "skills": [{"source": "local_path", "path": "."}],
                "cases": {"files": ["evals/cases/whole.yaml"]}}))
            before = manifest(target)
            command = [sys.executable, "-m", "refiner.cli", "run", str(target), "--engine-config", str(tmp/"engine.json"),
                       "--agent-command", f"{sys.executable} {ROOT/'tests/fixtures/protocol_agent.py'}",
                       "--output", str(tmp/"run"), "--parallelism", "1", "--timeout", "30"]
            env = os.environ.copy()
            if acceptance_failure:
                env["REFINER_TEST_ACCEPT_FAIL"] = "1"
            result = subprocess.run(command, cwd=ROOT, env=env, text=True, capture_output=True, timeout=150)
            self.assertEqual(result.returncode, 2 if acceptance_failure else 0, result.stdout + result.stderr +
                             ((tmp/"run/report.md").read_text() if (tmp/"run/report.md").exists() else ""))
            state = json.loads((tmp/"run/state.json").read_text())
            self.assertEqual(state["status"], "failed_acceptance" if acceptance_failure else "passed")
            self.assertEqual(len(state["rounds"]), 2)
            self.assertTrue(all(r["accepted"] for r in state["rounds"]))
            self.assertTrue(state["rounds"][0]["unresolved"])
            self.assertFalse(state["rounds"][0]["qualified_for_acceptance"])
            self.assertTrue(state["rounds"][1]["qualified_for_acceptance"])
            self.assertEqual(manifest(target), before)
            if acceptance_failure:
                self.assertFalse((tmp/"run/final").exists())
                return
            evidence = json.loads((tmp/"run/final/evidence.json").read_text())
            self.assertFalse(evidence["baseline"]["passed"])
            self.assertTrue(evidence["regression"]["passed"])
            self.assertTrue(evidence["acceptance"]["passed"])
            self.assertIn("existing:whole", [r["id"] for r in evidence["regression"]["cases"]])

    @unittest.skipUnless((ROOT/".tools/skill-up").exists(), "Run skill-refiner bootstrap first")
    def test_real_skillup_two_round_repair(self):
        self.run_pipeline()

    @unittest.skipUnless((ROOT/".tools/skill-up").exists(), "Run skill-refiner bootstrap first")
    def test_acceptance_failure_never_publishes_candidate(self):
        self.run_pipeline(acceptance_failure=True)


if __name__ == "__main__":
    unittest.main()

