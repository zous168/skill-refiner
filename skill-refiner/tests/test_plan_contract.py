"""Deterministic checks for plan case contracts and markdown rendering."""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

import jsonschema

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

from refiner.checks import validate_plan  # noqa: E402
from refiner.common import RefinerError, write_json  # noqa: E402
from refiner.plan import write_test_scenarios, write_work_plan  # noqa: E402
from refiner.pipeline import PLAN_REVIEW_CASE_BOOLS, validate_plan_review  # noqa: E402


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
        "checks": [{"type": "contains", "value": token}],
        "oracle_reason": f"Response must include {token} for observable success.",
        "good_response": f"Answer with {token} present.",
        "good_responses": [f"Alt wording {token}.", f"Another {token} phrasing."],
        "bad_response": "No matching token in this answer.",
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


if __name__ == "__main__":
    unittest.main()
