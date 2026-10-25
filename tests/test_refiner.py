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
from refiner.pipeline import (CAPABILITY_ABSENCE_AUDIT, CONVERSATIONAL_JSON_AUDIT, HELPER_EXECUTION_AUDIT, INPUT_BOUNDARY_AUDIT,
                              NATURAL_LANGUAGE_ASSERTION_AUDIT, PLANNING,
                              apply_changes, compare_candidate)
from refiner.standards import lint

ROOT = Path(__file__).resolve().parents[1]


def get_plan():
    result = subprocess.run([sys.executable, str(ROOT/"tests/fixtures/protocol_agent.py")],
                            input=json.dumps({"role": "plan", "input": {}}), text=True, capture_output=True, check=True)
    return json.loads(result.stdout)


class ContractTests(unittest.TestCase):
    def test_planning_and_review_audit_helper_control_flow_and_path_anchoring(self):
        for prompt in (PLANNING, HELPER_EXECUTION_AUDIT):
            self.assertIn("inspect its executable source line by line", prompt)
            self.assertIn("Path.iterdir() or glob()", prompt)
            self.assertIn("relative paths", prompt)
            self.assertIn("absolute saved_paths", prompt)
            self.assertIn("all assertions can be true together", prompt)

    def test_planning_and_review_ground_json_field_types_and_semantic_equivalents(self):
        for prompt in (PLANNING, CONVERSATIONAL_JSON_AUDIT):
            self.assertIn("json_path_regex requires its pointer to resolve", prompt)
            self.assertIn("array such as a natural list", prompt)
            self.assertIn("clarification questions", prompt)
            self.assertIn("video from a text description", prompt)
            self.assertIn("literal phrase “text-to-video”", prompt)
        self.assertIn("Do not invent a JSON response schema", CONVERSATIONAL_JSON_AUDIT)
        self.assertIn("whole-response semantic checks", CONVERSATIONAL_JSON_AUDIT)
        self.assertIn("at least two distinct good-response paraphrase controls", CONVERSATIONAL_JSON_AUDIT)

    def test_planning_and_review_cover_capability_absence_synonyms(self):
        for wording in ("未提供", "未列出", "不提供", "未返回", "不支持"):
            self.assertIn(wording, CAPABILITY_ABSENCE_AUDIT)
            self.assertIn(wording, CONVERSATIONAL_JSON_AUDIT)
        self.assertIn("sentence or clause", NATURAL_LANGUAGE_ASSERTION_AUDIT)
        self.assertIn("sentence or clause", CONVERSATIONAL_JSON_AUDIT)
        self.assertIn("specific URL", NATURAL_LANGUAGE_ASSERTION_AUDIT)
        self.assertIn("unrelated resource", CONVERSATIONAL_JSON_AUDIT)

    def test_planning_and_review_cover_documented_input_transport_boundaries(self):
        for prompt in (PLANNING, INPUT_BOUNDARY_AUDIT):
            self.assertIn("documented input channels", prompt)
            self.assertIn("malformed encoded bytes", prompt)
            self.assertIn("before external side effects", prompt)
        self.assertIn("harness-forced strict decoder masks that defect", INPUT_BOUNDARY_AUDIT)

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

    def test_bad_response_failure_names_each_oracle_that_still_matches(self):
        plan = get_plan()
        case = plan["cases"][0]
        case["checks"] = [
            {"type": "regex", "value": "orange"},
            {"type": "regex", "value": "kite"},
        ]
        case["good_response"] = "orange kite is available"
        case["good_responses"] = ["The orange kite can be used.", "There is an orange kite."]
        case["bad_response"] = "orange kite is unavailable"
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(RefinerError, "response checks that still match") as raised:
                validate_plan(plan, tmp)
        self.assertIn('"index": 1', str(raised.exception))
        self.assertIn('"type": "regex"', str(raised.exception))

    def test_natural_language_oracles_validate_multiple_paraphrase_controls(self):
        plan = get_plan()
        case = plan["cases"][0]
        case["checks"] = [{"type": "regex", "value": r"(?i)(?:link|URL).{0,40}(?:not (?:be )?used|not (?:be )?submitted|omitted|ignored)|(?:not (?:be )?used|not (?:be )?submitted|omitted|ignored).{0,40}(?:link|URL)"}]
        case["good_response"] = "The link will not be used as an image reference."
        case["good_responses"] = [
            "The URL will not be submitted as image input.",
            "The image link is omitted from this text-only request.",
        ]
        case["bad_response"] = "I will use the URL as the image reference."
        with tempfile.TemporaryDirectory() as tmp:
            validate_plan(plan, tmp)
            case["good_responses"][1] = "The image reference will be included."
            with self.assertRaisesRegex(RefinerError, "Good-response control 3"):
                validate_plan(plan, tmp)

    def test_binary_boundary_plan_rejects_environment_forced_strict_decoding(self):
        plan = get_plan()
        plan["requirements"][0]["behavior"] += "; reject malformed UTF-8 stdin before side effects"
        case = plan["cases"][0]
        case["prompt"] += " Test malformed UTF-8 input through stdin."
        case["files"]["probe.py"] = "env.update(PYTHONIOENCODING='utf-8:strict', PYTHONUTF8='1')"
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(RefinerError, "overrides the runtime decoding environment"):
                validate_plan(plan, tmp)

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

    def test_json_path_regex_matches_decoded_field_with_escaped_quotes(self):
        check = {"type": "json_path_regex", "pointer": "/explanation",
                 "value": r"(?is)(?:no.{0,30}(?:i2v|image-to-video)|(?:i2v|image-to-video).{0,30}(?:no|unconfirmed))"}
        good = json.dumps({"explanation": 'Only t2v="文生视频" is listed; there is no i2v capability.'},
                          ensure_ascii=False)
        bad = json.dumps({"explanation": 'Both t2v="文生视频" and i2v are confirmed.'}, ensure_ascii=False)
        self.assertTrue(check_one(check, good, ROOT)[0])
        self.assertFalse(check_one(check, bad, ROOT)[0])
        plan = get_plan()
        case = plan["cases"][0]
        case["good_response"], case["bad_response"], case["checks"] = good, bad, [check]
        with tempfile.TemporaryDirectory() as tmp:
            validate_plan(plan, tmp)

    def test_unanchored_status_enum_is_rejected_but_prompted_enum_is_allowed(self):
        plan = get_plan()
        case = plan["cases"][0]
        case["checks"] = [{"type": "json_path_equal", "pointer": "/status", "value": "clarification"}]
        case["good_response"] = '{"status":"clarification"}'
        case["bad_response"] = '{"status":"completed"}'
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "original" / "example"
            source.mkdir(parents=True)
            (source / "SKILL.md").write_text(
                "Ask follow-up questions when a request is incomplete.", encoding="utf-8")
            with self.assertRaisesRegex(RefinerError, "Ungrounded exact status/state"):
                validate_plan(plan, tmp)
            case["prompt"] = 'Return JSON and set status exactly to "clarification".'
            validate_plan(plan, tmp)

    def test_command_claim_is_not_execution_evidence(self):
        case = {"id": "command", "checks": [{"type": "tool_command", "value": "python.*total.py"}]}
        self.assertFalse(grade(case, "I executed python scripts/total.py", ROOT)["passed"])

    def test_tool_command_absent_checks_observed_commands_and_discriminating_control(self):
        check = {"type": "tool_command_absent", "value": r"scripts/csp_media\.py\s+run",
                 "control_command": "python3 scripts/csp_media.py run --request request.json"}
        self.assertTrue(check_one(check, "Please provide video dimensions first.", ROOT, [])[0])
        trace = [{"role": "tool_call", "tool_call": {"id": "submit", "name": "command_execution",
                 "arguments": {"command": "python3 scripts/csp_media.py run --request request.json"}}}]
        self.assertFalse(check_one(check, "Please provide video dimensions first.", ROOT, trace)[0])
        plan = get_plan()
        case = plan["cases"][0]
        case["checks"].append(check)
        with tempfile.TemporaryDirectory() as tmp:
            validate_plan(plan, tmp)

    def test_tool_call_absent_checks_observed_tool_names_and_discriminating_control(self):
        check = {"type": "tool_call_absent", "value": r".+",
                 "control_tool_name": "browser.open"}
        self.assertTrue(check_one(check, "Please provide dimensions first.", ROOT, [])[0])
        trace = [{"role": "tool_call", "tool_call": {"id": "open", "name": "browser.open",
                 "arguments": {"url": "https://example.test"}}}]
        self.assertFalse(check_one(check, "Please provide dimensions first.", ROOT, trace)[0])
        # The assertion is about recorded tool names, even if a command argument also exists.
        command_trace = [{"role": "tool_call", "tool_call": {"id": "cmd", "name": "exec_command",
                         "arguments": {"command": "echo harmless"}}}]
        self.assertFalse(check_one(check, "Please provide dimensions first.", ROOT, command_trace)[0])
        plan = get_plan()
        case = plan["cases"][0]
        case["checks"].append(check)
        with tempfile.TemporaryDirectory() as tmp:
            validate_plan(plan, tmp)

    def test_dynamic_response_path_must_match_observed_artifact_path(self):
        check = {"type": "json_path_matches_file", "pointer": "/saved_paths/0",
                 "path": "observed.json", "value": {"file_pointer": "/saved_paths/0"}}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "observed.json").write_text(json.dumps({"saved_paths": ["/tmp/actual.png"]}))
            self.assertTrue(check_one(check, '{"saved_paths":["/tmp/actual.png"]}', root)[0])
            self.assertFalse(check_one(check, '{"saved_paths":["/tmp/fabricated.png"]}', root)[0])

    def test_dynamic_path_checker_has_discriminating_plan_controls(self):
        plan = get_plan()
        case = plan["cases"][0]
        case["good_response"] = '{"saved_path":"/tmp/actual.png","task_id":"encoded%2Fid"}'
        case["bad_response"] = '{"saved_path":"/tmp/fabricated.png","task_id":"other%2Fid"}'
        case["checks"] = [
            {"type": "json_path_matches_file", "pointer": "/saved_path",
             "path": "observed.json", "value": {"file_pointer": "/saved_path"}},
            {"type": "json_path_matches_file", "pointer": "/task_id",
             "path": "observed.json", "value": {"file_pointer": "/task_id"}},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            validate_plan(plan, tmp)
            case["bad_response"] = case["good_response"]
            with self.assertRaisesRegex(RefinerError, "Bad-response control passes"):
                validate_plan(plan, tmp)

    def test_dynamic_path_checker_accepts_planner_pointer_alias(self):
        plan = get_plan()
        case = plan["cases"][0]
        case["good_response"] = '{"saved_path":"/tmp/actual.png"}'
        case["bad_response"] = '{"saved_path":"/tmp/fabricated.png"}'
        case["checks"] = [{"type": "json_path_matches_file", "pointer": "/saved_path",
                            "path": "observed.json", "value": {"pointer": "/saved_path"}}]
        with tempfile.TemporaryDirectory() as tmp:
            validate_plan(plan, tmp)

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

    def test_nonzero_tool_output_requires_the_documented_exit_code(self):
        output = '{"status":"error","error":{"code":"missing_key"}}'
        command = "python3 /tmp/skill/scripts/csp_media.py models"
        check = {"type": "tool_output_matches", "value": r"python3.*scripts/csp_media\.py models",
                 "control_command": "python3 scripts/csp_media.py models", "expected_exit_code": 3}
        def trace(exit_code, content=output):
            return [
                {"role": "tool_call", "tool_call": {"id": "cmd", "name": "command_execution",
                 "arguments": {"command": command}}},
                {"role": "tool_result", "tool_result": {"call_id": "cmd", "status": "error",
                 "exit_code": exit_code, "content": content}},
            ]
        self.assertTrue(check_one(check, output, ROOT, trace(3))[0])
        self.assertFalse(check_one(check, output, ROOT, trace(4))[0])
        self.assertFalse(check_one(check, '{"status":"completed"}', ROOT, trace(3))[0])
        self.assertFalse(check_one({k: v for k, v in check.items() if k != "expected_exit_code"},
                                   output, ROOT, trace(3))[0])
        plan = get_plan()
        case = plan["cases"][0]
        case["good_response"], case["bad_response"] = output, '{"status":"completed"}'
        case["checks"] = [check, {"type": "json_equal", "value": json.loads(output)}]
        with tempfile.TemporaryDirectory() as tmp:
            validate_plan(plan, tmp)

    def test_plan_rejects_redundant_zero_exit_code_for_tool_output(self):
        plan = get_plan()
        case = plan["cases"][0]
        case["checks"].append({
            "type": "tool_output_matches",
            "value": r"python3.*scripts/merge\.py",
            "control_command": "python3 scripts/merge.py ledger.json",
            "expected_exit_code": 0,
        })
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(RefinerError, "Redundant expected_exit_code=0"):
                validate_plan(plan, tmp)

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
    def run_pipeline(self, acceptance_failure=False, acceptance_fix=False):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            engine = {"name": "refiner-test-script", "custom": {"transport": "local", "response_format": "session_result",
                      "local": {"command": sys.executable, "args": [str(ROOT/"tests/fixtures/script_engine.py"),
                                 "${input_file}", "${output_file}"], "input_file": "${input_file}", "output_file": "${output_file}"}}}
            (tmp/"engine.json").write_text(json.dumps(engine))
            target = tmp/"source"/"decimal-total"
            snapshot(ROOT/"examples/decimal-total", target)
            if acceptance_fix:
                script = target/"scripts/total.py"
                content = script.read_text()
                content = content.replace('if not values:\n        print(json.dumps({"error": "empty list"}))\n        return',
                                          'if not values:\n        print(json.dumps({"total": "0.00"}))\n        return')
                content = content.replace("sum(int(float(value)) for value in values)",
                                          "sum(float(value) for value in values)")
                script.write_text(content)
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
            if acceptance_fix:
                env["REFINER_TEST_ACCEPT_FIX"] = "1"
            result = subprocess.run(command, cwd=ROOT, env=env, text=True, capture_output=True, timeout=150)
            report = (tmp/"run/report.md").read_text() if (tmp/"run/report.md").exists() else ""
            self.assertEqual(result.returncode, 2 if acceptance_failure else 0, result.stdout + result.stderr +
                             report)
            state = json.loads((tmp/"run/state.json").read_text())
            self.assertEqual(state["status"], "failed_acceptance" if acceptance_failure else "passed",
                             result.stdout + report)
            if not acceptance_failure:
                self.assertEqual(state["verification_status"], "verified")
                self.assertEqual(json.loads((tmp/"run/verification.json").read_text())["status"], "verified")
                stages = json.loads((tmp/"run/stages.json").read_text())
                self.assertTrue(all(stage["status"] == "completed" for stage in stages["stages"]))
            if acceptance_fix:
                self.assertEqual(len(state["rounds"]), 1)
                self.assertTrue(state["rounds"][0]["accepted"])
                self.assertEqual(state["rounds"][0]["trigger"], "acceptance")
                self.assertTrue(state["rounds"][0]["qualified_for_acceptance"])
            else:
                self.assertEqual(len(state["rounds"]), 2)
                self.assertTrue(all(r["accepted"] for r in state["rounds"]))
                self.assertTrue(state["rounds"][0]["unresolved"])
                self.assertFalse(state["rounds"][0]["qualified_for_acceptance"])
                self.assertTrue(state["rounds"][1]["qualified_for_acceptance"])
            self.assertEqual(manifest(target), before)
            if acceptance_failure:
                self.assertFalse((tmp/"run/final").exists())
                repair_requests = list((tmp/"run/agent-calls").glob("*-repair/request.json"))
                self.assertEqual(len(repair_requests), 3)
                last = json.loads(repair_requests[-1].read_text())
                self.assertIn("acceptance::accept-large", last["input"]["allowed_evidence_ids"])
                self.assertEqual(last["input"]["acceptance_failures"][0]["case_id"], "accept-large")
                return
            evidence = json.loads((tmp/"run/final/evidence.json").read_text())
            self.assertEqual(evidence["baseline"]["passed"], acceptance_fix)
            self.assertTrue(evidence["regression"]["passed"])
            self.assertTrue(evidence["acceptance"]["passed"])
            self.assertIn("existing:whole", [r["id"] for r in evidence["regression"]["cases"]])

    @unittest.skipUnless((ROOT/".tools/skill-up").exists(), "Run skill-refiner bootstrap first")
    def test_real_skillup_two_round_repair(self):
        self.run_pipeline()

    @unittest.skipUnless((ROOT/".tools/skill-up").exists(), "Run skill-refiner bootstrap first")
    def test_acceptance_failure_never_publishes_candidate(self):
        self.run_pipeline(acceptance_failure=True)

    @unittest.skipUnless((ROOT/".tools/skill-up").exists(), "Run skill-refiner bootstrap first")
    def test_real_acceptance_failure_repairs_then_regresses_and_reaccepts(self):
        self.run_pipeline(acceptance_fix=True)


if __name__ == "__main__":
    unittest.main()

