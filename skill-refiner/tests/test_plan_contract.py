"""Deterministic checks for plan case contracts and markdown rendering."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

import jsonschema

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

from refiner.checks import validate_plan  # noqa: E402
from refiner.common import PlanGateError, RefinerError, read_json, run_process, write_json  # noqa: E402
from refiner.creation import ANALYSIS, analysis_markdown, validate_analysis  # noqa: E402
from refiner.plan import write_test_scenarios, write_work_plan  # noqa: E402
from refiner.pipeline import (PLAN_REVIEW_CASE_BOOLS, close_incomplete_plan_gate, initial_phase_results, make_plan,
                               require_plan_gate, set_phase_result, validate_plan_review)  # noqa: E402
from refiner.gate import (_verify_phase_history, _verify_plan_control_artifacts,
                          _verify_plan_review_artifacts)  # noqa: E402
from refiner.runtime import codex_output_schema, cursor_command, decode_codex_response  # noqa: E402


class CodexWireSchemaTests(unittest.TestCase):
    def test_unconstrained_and_optional_fields_round_trip(self):
        schema = {
            "type": "object", "required": ["assertion_id", "value"],
            "properties": {"assertion_id": {"type": "string"}, "value": {},
                           "optional": {"type": "string"}},
        }
        wire_schema = codex_output_schema(schema)
        self.assertEqual(wire_schema["required"], ["assertion_id", "value_json", "optional"])
        self.assertEqual(wire_schema["properties"]["optional"]["anyOf"][1]["type"], "null")
        wire = {"assertion_id": "A-01", "value_json": json.dumps(["phrase one", "phrase two"]),
                "optional": None}
        self.assertEqual(decode_codex_response(wire, schema),
                         {"assertion_id": "A-01", "value": ["phrase one", "phrase two"]})

    def test_dynamic_object_round_trip(self):
        schema = {"type": "object", "additionalProperties": {"type": "string"}}
        lowered = codex_output_schema(schema)
        self.assertEqual(lowered["type"], "array")
        wire = [{"key": "status", "value": "passed"}, {"key": "stage", "value": "review"}]
        self.assertEqual(decode_codex_response(wire, schema), {"status": "passed", "stage": "review"})

    def test_cursor_executable_path_is_preserved_for_native_launcher(self):
        self.assertEqual(cursor_command("C:/tools/cursor-agent.exe"), (["C:/tools/cursor-agent.exe"], False))

    def test_process_output_is_decoded_as_utf8_on_windows(self):
        code, stdout, stderr, _ = run_process(
            [sys.executable, "-X", "utf8", "-c", "print('实际字段：已通过')"], timeout=10)
        self.assertEqual(code, 0, stderr)
        self.assertIn("实际字段：已通过", stdout)


def _case(case_id, kind, scenario, expected_outcome, prompt, requirement_id="R1"):
    token = f"token-{case_id}"
    return {
        "id": case_id,
        "requirement_ids": [requirement_id],
        "kind": kind,
        "scenario": scenario,
        "expected_outcome": expected_outcome,
        "prompt": prompt,
        "files": {},
        "helpers": [],
        "asset_audit": {
            "status": "not_required", "required_paths": [], "available_paths": [],
            "source_refs": [f"requirements.md#{requirement_id}"],
            "basis": "The case uses no files, helpers or external assets.",
        },
        "checks": [{"id": "A-RESPONSE", "type": "contains", "value": token}],
        "oracle_reason": f"Response must include {token} for observable success.",
        "good_response": f"Answer with {token} present.",
        "good_responses": [f"Alt wording includes {token}.", f"Another phrasing has {token}."],
        "bad_response": "No matching token in this answer.",
        "positive_controls": [
            {"id": "P-1", "response": f"Answer with {token} present."},
            {"id": "P-2", "response": f"Alt wording includes {token}."},
            {"id": "P-3", "response": f"Another phrasing has {token}."},
        ],
        "negative_controls": [{
            "id": "N-1", "assertion_id": "A-RESPONSE",
            "response": "No matching token in this answer.",
            "rationale": "The response omits the required observable result.",
        }],
    }


def _minimal_plan(**case_overrides):
    requirements = [{"id": "R1", "behavior": "Do the thing", "source": "requirements.md#R1", "required": True}]
    normal = _case("dev-normal", "normal", "Operator starts a valid task.", "Task completes with visible proof.",
                   "Execute the documented valid task path with explicit executor wording.")
    boundary = _case("dev-boundary", "boundary", "Operator supplies incomplete input.", "Clarification without side effects.",
                     "Trigger the boundary path using incomplete operator input only.")
    for key, value in case_overrides.items():
        if key in normal:
            normal[key] = value
        elif key in boundary:
            boundary[key] = value
    return {
        "schema_version": "skill-refiner.plan/v2",
        "requirements": requirements,
        "cases": [normal, boundary],
        "coverage_gaps": [],
        "assumptions": [],
    }


def _contract_entry(case_id, **flags):
    entry = {
        "case_id": case_id,
        "installed_source_unchanged": True,
        "fixture_contract_consistent": True,
        "response_contract_consistent": True,
        "trace_checks_observable": True,
        "scenario_consistent": True,
        "requirement_trace_consistent": True,
        "expected_outcome_consistent": True,
        "machine_checks_consistent": True,
        "matrix_complete": True,
        "asset_audit_consistent": True,
        "positive_controls_validated": True,
        "negative_controls_targeted": True,
        "basis": "Fixture and checks align with the frozen case contract.",
    }
    entry.update(flags)
    return entry


def _approved_review(plan):
    return {
        "approved": True,
        "issues": [],
        "case_contracts": [_contract_entry(case["id"]) for case in plan["cases"]],
    }


class PlanCaseFieldValidationTests(unittest.TestCase):
    def test_missing_required_case_text_rejected(self):
        for field in ("scenario", "expected_outcome"):
            with self.subTest(field=field):
                plan = _minimal_plan()
                del plan["cases"][0][field]
                with tempfile.TemporaryDirectory() as root:
                    with self.assertRaises(jsonschema.ValidationError):
                        validate_plan(plan, root)

    def test_helpers_is_an_explicit_required_array(self):
        plan = _minimal_plan()
        del plan["cases"][0]["helpers"]
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaises(jsonschema.ValidationError):
                validate_plan(plan, root)

    def test_asset_audit_cannot_claim_a_missing_source_asset(self):
        plan = _minimal_plan()
        audit = plan["cases"][0]["asset_audit"]
        audit.update(status="ready", required_paths=["references/missing.md"],
                     available_paths=["references/missing.md"])
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaises(RefinerError) as ctx:
                validate_plan(plan, root)
        self.assertIn("unavailable_claims", str(ctx.exception))
        self.assertEqual(ctx.exception.evidence["status"], "invalid_coverage")
        self.assertEqual(len(ctx.exception.evidence["case_control_results"]), 2)

    def test_every_positive_and_semantic_negative_is_checked_as_a_matrix(self):
        plan = _minimal_plan()
        case = plan["cases"][0]
        case["prompt"] += ' Return JSON only; status must be one of "complete" or "pending".'
        valid = [
            '{"status":"complete","message":"不需要补集数"}',
            '{"status":"complete","message":"无需补集数"}',
            '{"status":"complete","message":"不用补集数"}',
        ]
        case["checks"] = [
            {"id": "A-STATUS", "type": "json_path_equal", "pointer": "/status", "value": "complete"},
            {"id": "A-SEMANTIC", "type": "contains_any",
             "value": ["不需要补集数", "无需补集数", "不用补集数"]},
        ]
        case["positive_controls"] = [
            {"id": f"P-{i}", "response": response} for i, response in enumerate(valid, 1)
        ]
        case["good_response"] = valid[0]
        case["good_responses"] = valid[1:]
        semantic_failure = '{"status":"complete","message":"必须补集数"}'
        wrong_status_only = '{"status":"pending","message":"不需要补集数"}'
        case["negative_controls"] = [
            {"id": "N-STATUS", "assertion_id": "A-STATUS", "response": wrong_status_only,
             "rationale": "The semantic answer is correct but the status is incomplete."},
            {"id": "N-SEMANTIC", "assertion_id": "A-SEMANTIC", "response": semantic_failure,
             "rationale": "The status is correct but the business result is wrong."},
        ]
        case["bad_response"] = semantic_failure

        with tempfile.TemporaryDirectory() as root:
            result = validate_plan(plan, root)
        controls = result["case_control_results"][0]["controls"]
        self.assertEqual(len(controls), 5)
        self.assertTrue(all(row["passed"] for control in controls
                            for row in control["assertions"]
                            if control["polarity"] == "positive"))

        # A wrong status alone must not count as evidence that the semantic
        # business assertion detects an incorrect response.
        wrong_status_semantic_only = '{"status":"pending","message":"无需补集数"}'
        case["negative_controls"][1]["response"] = wrong_status_semantic_only
        case["bad_response"] = wrong_status_semantic_only
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaises(RefinerError) as ctx:
                validate_plan(plan, root)
        diagnostic = str(ctx.exception)
        self.assertIn("Frozen plan controls failed", diagnostic)
        self.assertIn("N-SEMANTIC", diagnostic)
        self.assertIn("A-STATUS", diagnostic)
        self.assertIn("A-SEMANTIC", diagnostic)
        self.assertIn("expected_condition", diagnostic)
        self.assertIn("actual_value", diagnostic)

    def test_positive_control_failure_names_assertion_and_actual_value(self):
        plan = _minimal_plan()
        case = plan["cases"][0]
        case["checks"].append({"id": "A-SECOND", "type": "contains", "value": "complete"})
        for control in case["negative_controls"]:
            control["assertion_id"] = "A-RESPONSE"
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaises(RefinerError) as ctx:
                validate_plan(plan, root)
        message = str(ctx.exception)
        self.assertIn("A-SECOND", message)
        self.assertIn("field_path", message)
        self.assertIn("expected_condition", message)
        self.assertIn("actual_value", message)

    def test_blank_case_text_rejected(self):
        for field in ("scenario", "expected_outcome"):
            with self.subTest(field=field):
                plan = _minimal_plan()
                plan["cases"][0][field] = "   "
                with tempfile.TemporaryDirectory() as root:
                    with self.assertRaises(RefinerError) as ctx:
                        validate_plan(plan, root)
                self.assertIn(f"missing required {field}", str(ctx.exception))

    def test_prompt_duplicated_expected_outcome_rejected(self):
        plan = _minimal_plan()
        plan["cases"][0]["expected_outcome"] = plan["cases"][0]["prompt"]
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaises(RefinerError) as ctx:
                validate_plan(plan, root)
        self.assertIn("must be separate from the executor prompt", str(ctx.exception))


class PlanReviewValidationTests(unittest.TestCase):
    def setUp(self):
        self.plan = _minimal_plan()
        self.plan["cases"].append(_case(
            "dev-error", "error", "Operator triggers an error case.",
            "The error is explicit and safe.", "Trigger the documented error path."))

    def test_incomplete_review_rejected(self):
        review = _approved_review(self.plan)
        review["case_contracts"] = review["case_contracts"][:2]
        with self.assertRaises(RefinerError) as ctx:
            validate_plan_review(review, self.plan)
        self.assertIn("every case exactly once", str(ctx.exception))

    def test_missing_review_assessment_rejected(self):
        review = _approved_review(self.plan)
        del review["case_contracts"][0]["scenario_consistent"]
        with self.assertRaises(jsonschema.ValidationError):
            validate_plan_review(review, self.plan)

    def test_duplicate_review_assessment_rejected(self):
        review = _approved_review(self.plan)
        review["case_contracts"].append(review["case_contracts"][0])
        with self.assertRaises(RefinerError) as ctx:
            validate_plan_review(review, self.plan)
        self.assertIn("duplicate case assessments", str(ctx.exception))

    def test_unknown_case_review_rejected(self):
        review = _approved_review(self.plan)
        review["case_contracts"][0]["case_id"] = "unknown-case"
        with self.assertRaises(RefinerError) as ctx:
            validate_plan_review(review, self.plan)
        self.assertIn("unknown case", str(ctx.exception))

    def test_false_contract_blocks_approval(self):
        review = _approved_review(self.plan)
        review["case_contracts"][0]["machine_checks_consistent"] = False
        with self.assertRaises(RefinerError) as ctx:
            validate_plan_review(review, self.plan)
        self.assertIn("conflicting execution contract", str(ctx.exception))

    def test_all_review_bools_required_for_approval(self):
        review = _approved_review(self.plan)
        for name in PLAN_REVIEW_CASE_BOOLS:
            entry = dict(review["case_contracts"][0])
            entry[name] = False
            bad = dict(review)
            bad["case_contracts"] = [entry, review["case_contracts"][1]]
            with self.assertRaises(RefinerError):
                validate_plan_review(bad, self.plan)


class PlanRevisionAndPhaseGateTests(unittest.TestCase):
    def test_upstream_planning_error_closes_a_running_plan_gate(self):
        state = {"phase_results": initial_phase_results(), "phase_history": []}
        set_phase_result(state, "plan_gate", "running")
        self.assertTrue(close_incomplete_plan_gate(state, "planner connection reset"))
        self.assertEqual(state["phase_results"]["plan_gate"]["status"], "not_passed")
        self.assertEqual(state["phase_history"][-1]["reason"], "upstream_error")
        self.assertEqual(state["phase_history"][-1]["error"], "planner connection reset")
        self.assertFalse(close_incomplete_plan_gate(state, "a later error"))

    def test_accepted_plan_persists_full_controls_and_independent_review(self):
        plan = _minimal_plan()

        class Agent:
            def __init__(self, work):
                self.work = Path(work)
                self.roles = []

            def ask(self, role, instruction, payload, schema):
                self.roles.append(role)
                return plan if role == "plan" else _approved_review(plan)

        with tempfile.TemporaryDirectory() as root:
            agent = Agent(root)
            phases = []
            accepted = make_plan(agent, {}, root, 8, max_attempts=1,
                                 phase_callback=lambda name, status, **detail:
                                 phases.append((name, status, detail.get("suite"))))
            self.assertEqual(accepted, plan)
            controls = read_json(Path(root) / "plan-controls" / "development.json")
            review = read_json(Path(root) / "plan-reviews" / "development.json")
            attempt = read_json(Path(root) / "plan-controls" / "attempts" / "development-01.json")
        self.assertEqual(agent.roles, ["plan", "plan-review"])
        self.assertEqual(controls["plan_sha256"], attempt["plan_sha256"])
        self.assertEqual(controls["case_control_results"], attempt["case_control_results"])
        self.assertEqual(review["review"]["approved"], True)
        self.assertIn(("plan_self_check", "passed", "development"), phases)
        self.assertIn(("independent_review", "passed", "development"), phases)

    def test_plan_revision_cannot_drop_an_unrelated_case(self):
        first = _minimal_plan()
        replacement = _case("dev-added", "boundary", "Operator uses an added case.",
                            "The added case produces evidence.", "Run the added boundary prompt.")
        revised = {**first, "cases": [first["cases"][0], replacement]}

        class Agent:
            def __init__(self):
                self.calls = 0
                self.reviews = 0

            def ask(self, role, instruction, payload, schema):
                if role == "plan":
                    self.calls += 1
                    return first if self.calls == 1 else revised
                self.reviews += 1
                review = _approved_review(first)
                review["approved"] = False
                review["issues"] = ["Revise dev-normal assertion while preserving the full matrix."]
                return review

        with tempfile.TemporaryDirectory() as root:
            with self.assertRaises(PlanGateError) as ctx:
                make_plan(Agent(), {}, root, 8, max_attempts=2)
            dropped_revision_controls = read_json(
                Path(root) / "plan-controls" / "attempts" / "development-02.json")
        self.assertIn("dropped_frozen_matrix_entries", str(ctx.exception))
        self.assertIn("after full controls", str(ctx.exception))
        self.assertIn("dev-boundary", str(ctx.exception))
        self.assertEqual(dropped_revision_controls["status"], "passed")
        self.assertTrue(dropped_revision_controls["revision_failures"])

    def test_skill_evaluation_is_blocked_until_all_plan_gates_pass(self):
        state = {"phase_results": initial_phase_results()}
        with self.assertRaises(PlanGateError):
            require_plan_gate(state)
        for name in ("plan_generation", "plan_self_check", "independent_review"):
            set_phase_result(state, name, "passed", suite="development")
            set_phase_result(state, name, "passed", suite="acceptance")
        set_phase_result(state, "plan_gate", "passed")
        require_plan_gate(state)

    def test_phase_status_enum_rejects_unknown_values(self):
        state = {"phase_results": initial_phase_results()}
        with self.assertRaises(RefinerError):
            set_phase_result(state, "plan_generation", "complete", suite="development")

    def test_agent_process_streams_events_and_waits_for_the_terminal_result(self):
        import sys

        events = []
        code, stdout, stderr, _ = run_process(
            [sys.executable, "-c", "import time; print('agent-start', flush=True); "
             "time.sleep(.05); print('agent-result', flush=True)"],
            timeout=None, on_output=lambda stream, chunk: events.append((stream, chunk)))
        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        self.assertIn("agent-result", stdout)
        self.assertEqual("".join(chunk for stream, chunk in events if stream == "stdout"), stdout)

    def test_final_plan_verification_recomputes_controls_and_binds_review(self):
        plans = {"development": _minimal_plan(), "acceptance": _minimal_plan()}
        for suite in plans:
            plans[suite]["cases"][0]["id"] = f"{suite}-normal"
            plans[suite]["cases"][1]["id"] = f"{suite}-boundary"
        with tempfile.TemporaryDirectory() as root:
            for suite, plan in plans.items():
                class Agent:
                    work = Path(root)

                    def ask(self, role, instruction, payload, schema):
                        return plan if role in {"plan", "acceptance-plan"} else _approved_review(plan)

                make_plan(Agent(), {}, root, 8,
                          role="acceptance-plan" if suite == "acceptance" else "plan",
                          max_attempts=1, suite=suite)
            _verify_plan_control_artifacts(Path(root), plans)
            _verify_plan_review_artifacts(Path(root), plans)

            summary = read_json(Path(root) / "plan-controls" / "development.json")
            summary["case_control_results"][0]["controls"][0]["assertions"][0]["actual_value"] = "tampered"
            write_json(Path(root) / "plan-controls" / "development.json", summary)
            with self.assertRaises(RefinerError):
                _verify_plan_control_artifacts(Path(root), plans)

    def test_final_gate_rejects_missing_or_out_of_order_plan_evidence(self):
        state = {"phase_results": initial_phase_results(), "phase_history": [
            {"sequence": 1, "phase": "plan_gate", "status": "passed"},
            {"sequence": 2, "phase": "skill_evaluation", "status": "running"},
        ]}
        for name in ("plan_generation", "plan_self_check", "independent_review"):
            state["phase_results"][name]["status"] = "passed"
            state["phase_results"][name]["suites"] = {"development": "passed", "acceptance": "passed"}
        state["phase_results"]["plan_gate"]["status"] = "passed"
        state["phase_results"]["skill_evaluation"] = {
            "status": "passed", "suites": {"baseline": "passed", "regression": "passed", "acceptance": "passed"}}
        state["phase_results"]["final_verification"]["status"] = "running"
        _verify_phase_history(state)
        state["phase_history"][0]["sequence"] = 3
        state["phase_history"][1]["sequence"] = 2
        with self.assertRaises(RefinerError):
            _verify_phase_history(state)


class PlanMarkdownRenderingTests(unittest.TestCase):
    def test_both_suites_render_with_distinct_requirement_sources(self):
        dev_plan = _minimal_plan()
        dev_plan["requirements"] = [
            {"id": "R1", "behavior": "Development behavior", "source": "dev-spec.md#R1", "required": True},
        ]
        acceptance = {
            "requirements": [
                {"id": "R1", "behavior": "Acceptance behavior", "source": "acceptance-spec.md#A1", "required": True},
            ],
            "cases": [
                _case("acc-normal", "normal", "Independent operator runs acceptance.", "Acceptance proof is visible.",
                      "Run the independent acceptance prompt with distinct executor wording."),
                _case("acc-error", "error", "Independent operator triggers failure.", "Failure is explicit and safe.",
                      "Run the independent error prompt with distinct executor wording."),
            ],
            "coverage_gaps": [],
            "assumptions": [],
        }
        with tempfile.TemporaryDirectory() as root:
            work = Path(root)
            write_json(work / "test-plan.json", dev_plan)
            write_json(work / "acceptance-plan.json", acceptance)
            write_work_plan(work, plan=dev_plan, acceptance=acceptance)
            write_test_scenarios(work, plan=dev_plan, acceptance=acceptance)
            work_plan = (work / "work-plan.md").read_text(encoding="utf-8")
            scenarios = (work / "test-scenarios.md").read_text(encoding="utf-8")
        self.assertIn("dev-spec.md#R1", work_plan)
        self.assertIn("acceptance-spec.md#A1", work_plan)
        self.assertIn("Scenario: Operator starts a valid task.", work_plan)
        self.assertIn("Expected outcome: Acceptance proof is visible.", work_plan)
        self.assertIn("contains; expected=token-acc-normal", work_plan)
        self.assertIn("Scenario: Operator starts a valid task.", scenarios)
        self.assertIn("Expected outcome: Acceptance proof is visible.", scenarios)
        self.assertIn("acceptance-spec.md#A1", scenarios)
        self.assertIn("contains; expected=token-acc-normal", scenarios)
        self.assertIn("Oracle rationale:", work_plan)
        self.assertEqual(work_plan.count("- Conclusion required to advance:"), 8)
        self.assertIn("Step 1 — Inspect the request and map the existing Skill", work_plan)
        self.assertIn("Step 4 — Pass the plan gate before target evaluation", work_plan)
        self.assertIn("Step 8 — Verify and deliver", work_plan)
        self.assertIn("showing this plan is a visibility checkpoint, not an approval wait", work_plan)
        self.assertIn("exhausted configured budget", work_plan)


class CreationAnalysisConclusionTests(unittest.TestCase):
    def _analysis(self):
        return {
            "name": "demo-skill",
            "goal": "Complete the requested business workflow.",
            "requirements": [
                {"id": "R1", "behavior": "Return a useful result.", "source": "spec-1: requested result", "required": True},
            ],
            "workflow": [
                {"id": "W1", "action": "Process the request.", "inputs": "Request text.",
                 "outputs": "A result.", "completion_check": "The result contains the requested fields.",
                 "failure_behavior": "Report the missing field and stop.", "requirement_ids": ["R1"]},
            ],
            "scenarios": [
                {"kind": "normal", "applicable": True, "situation": "Valid request.",
                 "expected_behavior": "Return the requested result.", "basis": "R1",
                 "requirement_ids": ["R1"]},
                {"kind": "boundary", "applicable": False, "situation": "No extra boundary case.",
                 "expected_behavior": "Not applicable to this scope.", "basis": "No boundary behavior requested.",
                 "requirement_ids": []},
                {"kind": "error", "applicable": False, "situation": "No extra error case.",
                 "expected_behavior": "Not applicable to this scope.", "basis": "No error behavior requested.",
                 "requirement_ids": []},
            ],
            "assumptions": [], "external_dependencies": [], "blocking_questions": [],
        }

    def test_business_workflow_step_requires_observable_completion_check(self):
        analysis = self._analysis()
        validate_analysis(analysis)
        self.assertIn("Completion check: The result contains the requested fields.", analysis_markdown(analysis))
        del analysis["workflow"][0]["completion_check"]
        with self.assertRaises(jsonschema.ValidationError):
            validate_analysis(analysis)


if __name__ == "__main__":
    unittest.main()
