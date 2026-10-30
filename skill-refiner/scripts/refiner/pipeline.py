from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import difflib
import json
import hashlib
import copy
import re
from pathlib import Path
import threading
import time

from .agent import Agent
from .checks import validate_plan
from .common import (Cancelled, PlanControlError, PlanGateError, PlanValidationError, RefinerError, digest, inventory, manifest, read_json, relative_path,
                     snapshot, write_json)
from .schemas import (PLAN, PLAN_REVIEW, PLAN_REVIEW_V2, PLAN_REVIEW_V3,
                      PLAN_REVIEW_V4, REPAIR, REVIEW)
from .evidence import repair_evidence
from .skillup import SkillUp
from .standards import AUTHORING, SELF_REVIEW_STANDARD, discover_project_standards, lint
from .profiles import load_config
from .matrix import Matrix, comparison_markdown
from .stages import StageManifest, REFINE_EVENTS
from .plan import parallel_agent_plan, write_test_scenarios, write_work_plan


PHASE_STATUS_ENUM = ("not_started", "pending", "running", "passed", "failed", "error", "skipped", "reused")
PLAN_GATE_STATUS_ENUM = ("pending", "running", "passed", "not_passed")


def initial_phase_results():
    plan_suites = {"development": "pending", "acceptance": "pending"}
    return {
        "schema_version": "skill-refiner.phase-results/v1",
        "status_enums": {"phase": list(PHASE_STATUS_ENUM), "plan_gate": list(PLAN_GATE_STATUS_ENUM)},
        "plan_generation": {"status": "pending", "suites": dict(plan_suites)},
        "plan_self_check": {"status": "pending", "suites": dict(plan_suites)},
        "independent_review": {"status": "pending", "suites": dict(plan_suites)},
        "plan_gate": {"status": "pending", "evidence": []},
        "skill_evaluation": {"status": "not_started", "suites": {
            "baseline": "not_started", "regression": "not_started", "acceptance": "not_started"}},
        "final_verification": {"status": "not_started"},
    }


def set_phase_result(state, name, status, *, suite=None, **details):
    if name not in state.setdefault("phase_results", initial_phase_results()):
        raise RefinerError(f"Unknown phase result: {name}")
    if name == "plan_gate":
        if status not in PLAN_GATE_STATUS_ENUM:
            raise RefinerError(f"Invalid plan-gate status: {status}")
    elif status not in PHASE_STATUS_ENUM:
        raise RefinerError(f"Invalid phase status: {status}")
    entry = state["phase_results"][name]
    if suite is not None:
        if suite not in entry.get("suites", {}):
            raise RefinerError(f"Unknown {name} suite: {suite}")
        if status not in PHASE_STATUS_ENUM:
            raise RefinerError(f"Invalid {name} suite status: {status}")
        entry["suites"][suite] = status
        if name in {"plan_generation", "plan_self_check", "independent_review"}:
            statuses = list(entry["suites"].values())
            if any(value == "error" for value in statuses):
                entry["status"] = "error"
            elif any(value == "failed" for value in statuses):
                entry["status"] = "failed"
            elif any(value == "running" for value in statuses):
                entry["status"] = "running"
            elif all(value in {"passed", "reused"} for value in statuses):
                entry["status"] = "reused" if all(value == "reused" for value in statuses) else "passed"
            else:
                entry["status"] = "pending"
    else:
        entry["status"] = status
    if details:
        entry.setdefault("history", []).append({"status": status, **details})
        entry["history"] = entry["history"][-100:]
    state.setdefault("phase_history", []).append({
        "sequence": len(state["phase_history"]) + 1,
        "phase": name, "status": status, "suite": suite,
        "at": datetime.now(timezone.utc).isoformat(), **details,
    })
    state["phase_history"] = state["phase_history"][-1000:]
    return entry


def close_incomplete_plan_gate(state, reason, *, cause="upstream_error"):
    """Give a started plan gate a terminal result when planning cannot finish."""
    entry = state.get("phase_results", {}).get("plan_gate", {})
    if entry.get("status") not in {"pending", "running"}:
        return False
    set_phase_result(state, "plan_gate", "not_passed", reason=cause, error=str(reason))
    return True


def close_incomplete_planning_phases(state, reason, *, cause="cancelled"):
    """Close any active plan phase when a controller call is cancelled."""
    phases = state.get("phase_results", {})
    changed = False
    for name in ("plan_generation", "plan_self_check", "independent_review"):
        entry = phases.get(name, {})
        running_suites = [suite for suite, status in entry.get("suites", {}).items()
                          if status == "running"]
        if running_suites:
            for suite in running_suites:
                set_phase_result(state, name, "error", suite=suite,
                                 cancellation=str(reason), cause=cause)
                changed = True
        elif entry.get("status") == "running":
            set_phase_result(state, name, "error", cancellation=str(reason), cause=cause)
            changed = True
    return changed


def require_plan_gate(state):
    phases = state.get("phase_results", {})
    for name in ("plan_generation", "plan_self_check", "independent_review"):
        entry = phases.get(name, {})
        allowed = {"passed", "reused"} if name == "plan_generation" else {"passed"}
        if entry.get("status") not in allowed or any(
                entry.get("suites", {}).get(suite) not in allowed
                for suite in ("development", "acceptance")):
            raise PlanGateError(f"Target Skill evaluation blocked because {name} is incomplete")
    status = phases.get("plan_gate", {}).get("status")
    if status != "passed":
        raise PlanGateError(f"Target Skill evaluation blocked because plan gate status is {status!r}")


HELPER_EXECUTION_AUDIT = """For every Python helper supplied in case files, inspect its executable source line by line and
trace the full control flow through every assertion to the final stdout and evidence writes. Check
that all assertions can be true together for the documented client behavior and fake-gateway
responses. Track the case working directory and path anchoring explicitly: Path.iterdir() or glob()
on a relative base yields relative paths, which will not equal an absolute saved_paths value unless
both sides are normalized. Verify artifact counts, directory contents, ordering and cleanup against
the exact operations performed. A correct target must not fail because its test helper compares
incompatible path forms or has contradictory expected outputs. Put the concrete helper assertion
and path/control-flow reasoning in the independent case review basis; do not accept only the
planner's summary of what the helper claims to verify."""


CONVERSATIONAL_JSON_AUDIT = """Do not invent a JSON response schema or a string-only collection field solely to make grading
convenient when the Skill contract asks for ordinary conversation. Prefer checking natural-language
answers directly. If a test needs JSON, state every checked field type in the prompt. json_path_regex requires its pointer to resolve
to a string, so do not apply it to an array such as a natural list of
clarification questions. For an unconstrained collection, accept reasonable string or array forms
with whole-response semantic checks; use exact array checks only when the business contract specifies
all elements. A string good_response does not establish a string-only output contract. Also accept
ordinary equivalent phrasing: a request to generate a video from a text description expresses
text-to-video even when it does not say the token `t2v` or the literal phrase “text-to-video”. Use
structured operation fields when exact operation labels are needed, and do not make natural-language
checks depend on one product-specific term. For alias and capability explanations, assert the
relationship between each exact alias and its returned operation; nearby words assigned to a
different alias must not pass. Make bad_response reverse the relevant alias-operation relationship,
and confirm the oracles reject it before freezing the case. For natural-language response checks,
include at least two distinct good-response paraphrase controls, including ordinary wording likely
used by another capable agent. Validate every good control against every response oracle and ensure
the bad control still contradicts a required behavior."""


CAPABILITY_ABSENCE_AUDIT = """For exact alias-scoped capability absence, accept semantically equivalent wording such as
'未提供', '未列出', '不提供', '未返回', '不支持', or '没有'. Verify each positive wording against the
alias-bounded assertion so one alias's sentence cannot satisfy another alias's capability check."""

NATURAL_LANGUAGE_ASSERTION_AUDIT = """For natural-language assertions, keep the related subject, action and negation within the same
sentence or clause; do not let a phrase about a different action later in the response complete a broad
regex. When a user supplies a specific URL, file or other resource, bind a no-use/no-submit assertion to
that supplied resource; a statement about a different or unrelated resource must not satisfy it."""
CONVERSATIONAL_JSON_AUDIT += "\n" + CAPABILITY_ABSENCE_AUDIT + "\n" + NATURAL_LANGUAGE_ASSERTION_AUDIT + "\n"


INPUT_BOUNDARY_AUDIT = """For executable targets, inspect documented input channels and their decoding and validation paths. When
the same request is accepted from a file and stdin, cover malformed JSON and malformed encoded bytes
on the relevant channels with binary fixtures; assert the documented exit result and that rejection
happens before external side effects. Do not infer byte-decoding behavior from JSON-value fixtures.
Do not make a malformed-byte test pass by setting PYTHONIOENCODING to a strict decoder, setting
PYTHONUTF8, changing locale/encoding variables, or configuring a test-only decoder policy in the
subprocess environment. Remove inherited encoding overrides when needed, then test the target's
documented behavior under ordinary runtime defaults. The target must return the documented input
error itself and must not submit media; a harness-forced strict decoder masks that defect."""


PLANNING = """Inspect every relevant supplied Skill file, executable implementation, references and
external requirements. Derive the promised behavior, not the implementation's mistakes. Design
executable user tasks with concrete fixtures and independently justified expected outputs. Never
put the expected answer into the user's prompt. Capability tests should explicitly ask to use the
installed Skill and its script when this is part of the contract; do not let the executor replace
the target implementation with mental computation. Output checks test the final answer or workspace
files, not SKILL.md text. Decompose each requirement into stable, independently observable acceptance
criteria with its own evidence. Treat executable cases as a detailed scenario matrix: give each a
unique scenario_id and identify preconditions, concrete stimulus, ordered actions, expected observable
outcomes, forbidden outcomes, relevant risk dimensions and source basis. Vary scenarios where workflow
state, actors/permissions, input boundaries, dependencies, cross-requirement effects, side effects,
retries/idempotency or recovery change the correct result. Do not count paraphrases or cosmetic input
changes as distinct coverage. Map each case to the exact acceptance criteria it exercises; cover every
required criterion and each explicitly applicable scenario exactly once in the suite. Assess normal,
boundary and error applicability, and state a scope-based reason for exclusions. If the configured
case/evaluation budget cannot cover the justified matrix, report the uncovered items as a plan gap;
never compress distinct risks into a vague case just to meet a cap.
Use tool_command with a regular expression matching the actual interpreter/script invocation when
executing the bundled script is a required behavior. It inspects real tool-call transcript commands,
not the user's prompt. Installed paths vary across engines; do not invent an absolute/relative path
spelling requirement. Tool checks recognize equivalent existing workspace file paths. Only set
literal_command=true when the source contract explicitly requires a literal command/path spelling.
Use tool_command_absent when a required clarification or permission gate forbids executing a particular
submission command. Give control_command a concrete forbidden-command example; the checker proves no
matching tool-call command appears and validates itself against that positive control.
Use tool_call_absent when a task forbids tool actions, including non-command browser, network or upload
tools. Its value matches recorded tool names and control_tool_name supplies a matching positive
control. For a clarification-only task that must not call any tools, require that in the prompt and
use a nonempty-name pattern such as .+; tool_command_absent alone cannot prove that no non-command
tool was called. This checks observed tool calls, not network egress from an allowed command.
When the response must preserve that script's output, use tool_output_matches
in addition to a content assertion. Its value is a command regex and control_command is a concrete
matching example invocation for positive/negative trace controls. This checks that the final JSON/text
actually equals the observed tool output, rejecting manual correction of a defective script. For an
expected nonzero command result, set expected_exit_code to the documented status; the checker then
requires that exact observed exit code as well as matching complete output. Do not set
expected_exit_code=0: a successful tool-result status already proves success. Some skill-up adapter
transcripts expose only success/failed and omit the numeric exit code; if a test must verify an exact
nonzero subprocess code through such an adapter, use a fixture helper that captures the installed
script's actual argv, exit code and complete stdout into structured evidence, then exits successfully.
Assert that helper's complete observed result and the captured nonzero code.
For paraphrasable user-facing explanations, do not require one literal adjective unless the contract
does; use regex alternatives for materially equivalent explanations or assert structured choices and
reasons instead of grading a single keyword.
The planner's good_response is a synthetic checker control, not evidence that the Skill uses its
labels. Ground every exact json_path_equal value for a status/state field in the documented Skill
contract, or state that exact enum in the case prompt; otherwise omit that exact status check. When
you introduce a JSON output shape for a conversational task, specify any required enum values in
the prompt itself. Prefer checking the requested decision and question content without inventing
an unconstrained status vocabulary.
For clarification checks about concepts such as dimensions, include common equivalent wording from
the target language and domain (for example width, height, pixels and resolution), not only the
formal term. A correct question about width and height must pass a dimensions check.
For an unspecified media quantity, include natural phrases such as 生成段数, 生成几段,
需要几份 or the equivalent number-of-outputs wording; do not require only a fixed noun like 数量.
For capability explanations, accept equivalent absence wording such as “no i2v capability,”
“provides no image-to-video support,” “i2v is not listed,” or “only t2v is confirmed.” Pair
user-facing explanation checks with structured evidence for requested and discovered operations;
phrases such as “was not returned by discovery” or “does not appear in the catalog” also establish
absence. Do not require a single negation phrase when the response clearly names the absent capability.
For permission checks, include equivalent natural language such as “有权使用,” “允许使用,” and
“已获得授权,” not only “授权” or “许可.” Build each prose assertion from the behavior required,
not from one preferred wording; review the good_response and bad_response controls against the full
semantic range rather than treating either sample as the only acceptable phrasing.
For generated-media or similar requests, carry every user-specified subject and distinguishing
attribute (such as color) into fixture validation and observed-submission assertions; do not accept
an output that preserves the subject while dropping an explicit attribute.
When explaining a change from image-to-video to text-to-video, a statement that the supplied URL or
link will not be used as a reference, or that no image URL/reference field will be submitted or
attached, clearly explains the lost image input. Treat that as equivalent to naming the image or
photo; do not require the same source noun when the supplied URL uniquely identifies the input.
Use json_path_regex to check paraphrasable text inside a specific JSON string field; its pattern
matches the decoded field value, so embedded escaped quotes cannot truncate the assertion. Use regex
for plain-text answers or when the assertion intentionally spans the whole serialized response.
For duration questions, include natural wording such as 多长时间 and 需要多久 alongside 时长,
多少秒 and other equivalent expressions. Do not require the user-facing response to repeat the
formal field name.
Use text_equal when exact non-whitespace response text is required. Insignificant
JSON formatting differences are not missing coverage unless explicitly required by business rules.
At least one case must have kind=normal and at least one kind=boundary, error or negative.
Use only supported declarative checks. Provide good_response and clearly wrong bad_response controls
for response checks. JSON checks should prefer json_path_equal if the output has optional fields.
Use json_path_matches_file when a user-facing JSON field must exactly match a value captured in a
workspace evidence file, such as a reported saved path. Its shape is
{"type":"json_path_matches_file","pointer":"/response/path","path":"observed.json",
"value":{"file_pointer":"/saved_path"}}; value.pointer is an accepted alias for file_pointer.
For file checks use path (workspace relative). For JSON pointers use RFC6901 pointer. Record all
required behaviors and map them to tests. Do not invent out-of-scope features. Report missing
evidence or required binary/omitted files as coverage gaps. Only uncovered REQUIRED behaviors belong
in coverage_gaps; intentional scope exclusions belong in assumptions. Keep fixtures small. Each oracle_reason
must explain how the expected result follows from source requirements, including arithmetic where
applicable. Sources are file paths plus section/line identifiers. The expected JSON value belongs
in checks.value; expected strings must not be nested as JSON strings unnecessarily.
Execution contract: the installed evaluated Skill and its bundled implementation are read-only.
Using a Skill to create or repair a separate task-workspace candidate is allowed; modifying the
installed copy is an execution error. Inspect every fixture's instructions as well as its code.
A fixture that instructs an inner evaluated agent to repair its installed implementation conflicts
with this contract and cannot be used to demonstrate an ordinary baseline failure. Construct a
coherent fixture before freezing tests. A refinement Skill should produce repaired candidate copies,
while the child Skill being evaluated should specify its business behavior without telling its
executor to self-repair. Do not remove real target capabilities to resolve a fixture contradiction."""
PLANNING += """
For a batch of independently requested JSON outputs, json_sequence_equal compares an ordered
array of expected JSON values against the actual raw JSON stream, ignoring object key order and
insignificant whitespace. Do not require the response to wrap outputs in an array. Combine this
with tool_output_matches for unchanged stdout forwarding. Avoid text_equal for semantic JSON
contracts unless exact serialization is explicitly required. Error fixtures must isolate the
intended predicate: an invalid-byte fixture should otherwise be schema-valid if decoded
permissively; a NaN fixture must not also violate an independent field/type rule.
Make output and trace contracts executable as written. json_equal/json_path_equal and
tool_output_matches require a complete JSON response or unchanged stdout; a prompt must not
permit extra prose, explanations, links or summaries that those checks reject. Say to return
only the requested JSON/stdout. JSON path checks may parse an embedded JSON object and therefore
do not prove the whole answer contains only JSON; add an anchored whole-response format check when
the contract requires JSON with no surrounding prose. tool_command sees direct recorded tool-call commands, not
subprocess argv constructed inside a Python helper or heredoc. If a task legitimately delegates
CLI execution to a verifier helper, check that helper's observable invocation and its independent
captured-result assertions instead of adding an unobservable inner-command regex. A fixture
verifier must inspect actual subprocess argv, exit status and output rather than return an
unsubstantiated attestation. Avoid prescribing an arbitrary invocation style solely to satisfy
a regex when equivalent execution is part of the allowed target behavior. If a task prompt permits
multiple invocation paths, checks must accept each permitted path or the prompt must require the
specific path the check observes. Do not combine a permissive helper prompt with a direct-command-only
trace regex.

When a tool_command check is needed to prove a helper or script was run, require its standalone
invocation explicitly in the case prompt and make setup inputs available as case files or separate
setup steps. Do not require a direct invocation while allowing the executor to wrap it inside an
inline Python program or heredoc. For example, if the check matches `python3 scenario_probe.py ...`,
the case prompt must require that command form and provide request files so the executor does not
need an inline program to construct them. Keep nested subprocess checks in the helper's own captured
evidence, where they are observable.
For json_path_matches_file, a dynamic example value in good_response is a control only: the plan
validator builds temporary matching evidence from it to prove the bad_response is rejected. Do not
require a fixed evidence fixture for values that are unknown until execution. Instead verify that
the supplied helper or executable fixture actually writes the checked evidence file from observed
execution results and that the case prompt requires returning the matching response value.
When another file_json_path_equal check reads the same evidence file, include every checked pointer
in the known-good file fixture with values that agree with good_response. The runtime helper may
delete or overwrite that control fixture before recording fresh evidence; tell the case prompt to
run the helper and return its observed output, not to use the fixture as runtime evidence.

For tool_output_matches, good_response must be the COMPLETE observed stdout, including every
field the actual source or fixture emits. A partial JSON object can satisfy separate field checks
but is not a valid unchanged-output control. Derive the full response from the supplied implementation,
fixture or recorded real run evidence. If the complete output cannot be justified, do not assert
unchanged forwarding; use checks for the concrete observable fields instead."""
PLANNING += "\n" + HELPER_EXECUTION_AUDIT + "\n"
PLANNING += "\n" + CONVERSATIONAL_JSON_AUDIT + "\n"
PLANNING += "\n" + INPUT_BOUNDARY_AUDIT + "\n"
PLANNING += """
Each case must include human-readable scenario and expected_outcome fields separate from prompt.
scenario states the user situation and inputs at a high level; expected_outcome states what observable
success looks like before machine checks; prompt is only the executor task wording. Do not duplicate the
full prompt into scenario or expected_outcome."""
PLANNING += """

Schema-first JSON contract: read the supplied response_schema before planning. Return exactly one JSON
object matching it, without Markdown fences or prose. Use schema_version=skill-refiner.plan/v5. Every case
must include every required field; always emit helpers as an array, using [] when no helper is needed.
Use only the schema's kind and check-type enums. Give every check a unique stable id. Include an asset_audit
with every fixture/helper path marked required and available, all linked requirement source_refs, a valid
status (ready or not_required), and a concrete basis. Include positive_controls with stable IDs; all good
wordings must be distinct. Include negative_controls for every response assertion; each declares the exact
assertion_id it contradicts and a rationale. A negative control is valid only when that target assertion
fails and every other response assertion passes. Keep good_response equal to the first positive control.
When validation or review returns feedback, preserve every prior requirement ID and case ID; edit the
targeted case in place and keep all unrelated cases and assets. Never return only the examples named by
the latest feedback."""
PLANNING += """

For a user-facing claim that has several valid phrasings, encode the equivalent phrases as a structured
contains_any check (or use a reviewed regex alternation when structure is not applicable). For prohibited
misclassification claims, enumerate materially different wording in not_contains_any or a justified regex
alternation. Include different positive paraphrases and negative responses in the controls. Do not use a
single narrow phrase such as only '不需要补集数' when '无需补集数' has the same meaning, and do not rely on
one not_contains phrase to detect every wording of an operator's incorrect judgment. Controls must show that
the assertion distinguishes business meaning: vary the target behavior while keeping unrelated fields correct."""
PLANNING += """

Every v5 plan must include stage_contracts for the Skill's complete persisted/handoff workflow. Declare
stable schema IDs, exactly one active version per logical ID, package-relative schemas/vN/<name>.schema.json paths
where N matches the schema version,
validator IDs and executable validator paths, and whether each schema is needed at target runtime. A version is
for an incompatible persisted-format change, not an internal revision; do not list historical or compatibility
versions in the target Skill contract. Only package schemas and validators marked as needed at target runtime.
Map every stage's requirement IDs, inputs, outputs, validation gate,
failure states, observable completion check and resume policy. Every input/output artifact references a
declared schema. Every runtime validator and schema file must be packaged. Every stage validates after generation, before handoff, on resume and at final verification.
Do not claim a schema exists when only planned: name the file to be delivered and have the final Skill include
every schema it needs at runtime. Define artifacts in the Skill package, not in Refiner run records. The
controller's own stage artifacts use the same packaged schema/validator policy with no self-exemption."""
PLANNING += """

Every v5 plan also includes a schema-valid execution_plan. State the objective, in-scope/out-of-scope work,
constraints and bounded concurrency. Break the work into ordered, stable-ID steps. Every step names its goal,
concrete actions, inputs, output paths with schema ID/version and observable evidence, requirement IDs, case IDs,
dependencies, execution mode, and one responsible Agent role with isolated write scope, merge owner and join gate.
Give every step observable exit criteria and a bounded failure policy with terminal state and same-run recovery policy.
Use contiguous order numbers; all requirement and case IDs must be covered. Only put independent tasks in the same
parallel wave; explain every serial step. If the host or task provides no safe independent work, set concurrency to 1
and explain the serial schedule. The development and held-out acceptance plans must preserve this exact execution_plan.
The human-readable work plan must render these steps from the structured object, not invent a separate schedule."""
PLANNING += "\n\n" + SELF_REVIEW_STANDARD
LANGUAGE_POLICY_PLANNING = (
    "Language acceptance is part of the frozen plan. For a new Skill, default its user-facing instructions and "
    "human-readable package text to Simplified Chinese unless the user specifies another language. For refinement, "
    "preserve the existing Skill language unless translation is requested. Include at least one case that checks "
    "SKILL.md and representative references or user-visible messages in the required language; do not translate "
    "machine identifiers, protocol literals, source quotations, or business outputs whose language is specified by "
    "the workflow."
)
PLANNING += "\n\n" + LANGUAGE_POLICY_PLANNING

PLAN_REVIEW_CASE_BOOLS = (
    "installed_source_unchanged", "fixture_contract_consistent", "response_contract_consistent",
    "trace_checks_observable", "scenario_consistent", "requirement_trace_consistent",
    "expected_outcome_consistent", "machine_checks_consistent", "matrix_complete",
    "asset_audit_consistent", "positive_controls_validated", "negative_controls_targeted",
    "acceptance_criteria_traceable", "scenario_detail_complete", "risk_coverage_complete",
)

SELF_REVIEW_PLAN_REVIEW = (
    "Independently verify that every consequential target workflow has a frozen, evidence-linked self-review "
    "checklist whose checks link to requirements, acceptance criteria and scenarios. Controls must cover a passing "
    "self-check and a missing-evidence/failure path. Verify the record contract and that one targeted correction is "
    "the maximum: a still-failing check becomes blocked. Self-review cannot replace independent acceptance, add "
    "checks during execution, or trigger unbounded test rounds."
)

LANGUAGE_PLAN_REVIEW = (
    "Independently verify the frozen language policy and its test coverage: new Skills default human-readable "
    "instructions to Simplified Chinese, existing Skills retain their language unless translation is requested, "
    "and the language checks leave machine contracts and required business-output languages intact."
)
PLAN_REVIEW_CASE_BOOLS_V2 = PLAN_REVIEW_CASE_BOOLS[:-3]


def validate_plan_review(review, plan):
    import jsonschema
    version = plan.get("schema_version")
    legacy = version == "skill-refiner.plan/v2"
    review_schema = {"skill-refiner.plan/v2": PLAN_REVIEW_V2,
                     "skill-refiner.plan/v3": PLAN_REVIEW_V3,
                     "skill-refiner.plan/v4": PLAN_REVIEW_V4}.get(version, PLAN_REVIEW)
    jsonschema.validate(review, review_schema)
    if version == "skill-refiner.plan/v4" and not all(
            review[key] for key in ("stage_contracts_assessed", "stage_contracts_complete",
                                    "schema_paths_safe", "validator_lifecycle_covered")):
        raise RefinerError("Independent plan review rejected incomplete Skill stage schema contracts")
    if version == "skill-refiner.plan/v5":
        required_plan_flags = ("stage_contracts_assessed", "stage_contracts_complete", "schema_paths_safe",
                               "validator_lifecycle_covered", "execution_plan_assessed", "execution_plan_complete",
                               "dependency_graph_valid", "concurrency_safe", "failure_recovery_bounded")
        if not all(review[key] for key in required_plan_flags):
            raise RefinerError("Independent plan review rejected incomplete stage schema contracts or execution plan elements")
        step_reviews = review["step_assessments"]
        expected_steps = {step["id"] for step in plan["execution_plan"]["steps"]}
        assessed_steps = [row["step_id"] for row in step_reviews]
        if len(assessed_steps) != len(set(assessed_steps)) or set(assessed_steps) != expected_steps:
            raise RefinerError("Independent plan review must assess every execution step exactly once")
        flags = ("goal_actions_clear", "inputs_outputs_evidenced", "requirement_case_traceable",
                 "dependencies_valid", "agent_scope_safe", "exit_and_recovery_clear")
        if review["approved"] and any(not all(row[key] for key in flags) for row in step_reviews):
            raise RefinerError("Approved plan review contradicts an execution step assessment")
    contracts = review["case_contracts"]
    expected = {c["id"] for c in plan["cases"]}
    assessed = [c["case_id"] for c in contracts]
    if len(assessed) != len(set(assessed)):
        raise RefinerError("Plan review contains duplicate case assessments")
    for entry in contracts:
        if entry["case_id"] not in expected:
            raise RefinerError(f"Plan review references unknown case: {entry['case_id']}")
    if len(contracts) != len(expected) or set(assessed) != expected:
        raise RefinerError("Plan review must assess the execution contract of every case exactly once")
    required_flags = PLAN_REVIEW_CASE_BOOLS_V2 if legacy else PLAN_REVIEW_CASE_BOOLS
    if review["approved"] and any(not all(entry[k] for k in required_flags) for entry in contracts):
        raise RefinerError("Approved plan review contains a conflicting execution contract")


def make_plan(agent, context, work, max_cases, role="plan", max_attempts=2,
              *, phase_callback=None, suite="development", on_self_check_passed=None):
    feedback = []
    previous_plan = None
    def phase(name, status, **detail):
        if phase_callback:
            phase_callback(name, status, suite=suite, **detail)
    for attempt in range(max_attempts):
        phase("plan_generation", "running", attempt=attempt + 1)
        try:
            plan = agent.ask(role, PLANNING + f" Generate 2 to {max_cases} cases, never more. " +
                         ("Copy the supplied contract requirement list exactly, preserving IDs and all properties. "
                          if "contract" in context else "") +
                         ("Include at least one executable case of EACH required_case_kinds category: "
                          + ", ".join(context["required_case_kinds"]) + ". "
                          if context.get("required_case_kinds") else "") +
                         ("Include one distinct executable case for each applicable required_scenario_ids entry; "
                          "copy the source scenario details and keep the scenario_id stable. "
                          if context.get("required_scenario_ids") else "") +
                         ("Copy expected_execution_plan exactly; development and independent acceptance use one frozen work plan. "
                          if context.get("expected_execution_plan") is not None else "") +
                         ("Use different inputs from common examples; these are independent acceptance cases. "
                          "Copy the supplied contract requirement list exactly, preserving IDs and all properties."
                          if role == "acceptance-plan" else ""),
                         {**context, "validation_feedback": feedback, **(
                             {'previous_plan': previous_plan} if previous_plan else {})}, PLAN)
        except RefinerError as exc:
            invalid_contract = "invalid contract" in str(exc).lower()
            phase("plan_generation", "failed" if invalid_contract else "error",
                  attempt=attempt + 1, reason=str(exc))
            if invalid_contract:
                raise PlanGateError(f"Plan generation did not satisfy the JSON schema: {exc}") from exc
            raise
        current_plan = plan
        phase("plan_generation", "passed", attempt=attempt + 1,
              case_ids=[case.get("id") for case in plan.get("cases", []) if isinstance(case, dict)])
        phase("plan_self_check", "running", attempt=attempt + 1)
        revision_failures = []
        try:
            if previous_plan:
                previous_requirements = {row["id"] for row in previous_plan.get("requirements", [])
                                         if isinstance(row, dict) and isinstance(row.get("id"), str)}
                current_requirements = {row["id"] for row in plan.get("requirements", [])
                                        if isinstance(row, dict) and isinstance(row.get("id"), str)}
                previous_cases = {row["id"] for row in previous_plan.get("cases", [])
                                  if isinstance(row, dict) and isinstance(row.get("id"), str)}
                current_cases = {row["id"] for row in plan.get("cases", [])
                                 if isinstance(row, dict) and isinstance(row.get("id"), str)}
                missing_requirements = sorted(previous_requirements - current_requirements)
                missing_cases = sorted(previous_cases - current_cases)
                if missing_requirements or missing_cases:
                    revision_failures.append({
                        "kind": "dropped_frozen_matrix_entries",
                        "missing_requirement_ids": missing_requirements,
                        "missing_case_ids": missing_cases,
                        "expected_condition": "preserve every prior requirement and case ID",
                        "actual_value": {"requirement_ids": sorted(current_requirements),
                                         "case_ids": sorted(current_cases)}})
                if plan.get("requirements") != previous_plan.get("requirements"):
                    revision_failures.append({
                        "kind": "changed_frozen_requirement_contract",
                        "field_path": "requirements", "expected_condition": "byte-for-byte stable requirement rows",
                        "actual_value": plan.get("requirements")})
                feedback_text = "\n".join(feedback)
                targeted_case_ids = {
                    case_id for case_id in previous_cases
                    if re.search(rf"(?<![A-Za-z0-9_-]){re.escape(case_id)}(?![A-Za-z0-9_-])",
                                 feedback_text, flags=re.IGNORECASE)
                }
                previous_by_id = {row["id"]: row for row in previous_plan.get("cases", [])
                                  if isinstance(row, dict) and isinstance(row.get("id"), str)}
                current_by_id = {row["id"]: row for row in plan.get("cases", [])
                                 if isinstance(row, dict) and isinstance(row.get("id"), str)}
                changed_unrelated = sorted(
                    case_id for case_id, old_case in previous_by_id.items()
                    if case_id in current_by_id and case_id not in targeted_case_ids
                    and current_by_id[case_id] != old_case
                )
                if changed_unrelated:
                    revision_failures.append({
                        "kind": "modified_cases_outside_feedback_scope",
                        "field_path": "cases[]", "targeted_case_ids": sorted(targeted_case_ids),
                        "expected_condition": "all unmentioned prior cases remain unchanged",
                        "actual_value": changed_unrelated})
            if "contract" in context and plan["requirements"] != context["contract"]:
                revision_failures.append({"kind": "changed_frozen_capability_contract",
                                          "field_path": "requirements",
                                          "expected_condition": context["contract"],
                                          "actual_value": plan["requirements"]})
            if ("stage_contracts" in context
                    and plan.get("stage_contracts") != context["stage_contracts"]):
                revision_failures.append({"kind": "changed_frozen_stage_artifact_contract",
                                          "field_path": "stage_contracts",
                                          "expected_condition": "exactly preserve the frozen Skill stage/schema map",
                                          "actual_value": plan.get("stage_contracts")})
            if (previous_plan and plan.get("execution_plan") != previous_plan.get("execution_plan")):
                revision_failures.append({"kind": "changed_frozen_execution_plan",
                                          "field_path": "execution_plan",
                                          "expected_condition": "preserve the complete approved step/dependency/agent/recovery plan",
                                          "actual_value": plan.get("execution_plan")})
            missing_kinds = set(context.get("required_case_kinds", [])) - {c["kind"] for c in plan["cases"]}
            if missing_kinds:
                revision_failures.append({"kind": "missing_required_scenario_categories",
                                          "field_path": "cases[].kind",
                                          "expected_condition": sorted(context.get("required_case_kinds", [])),
                                          "actual_value": sorted({c["kind"] for c in plan["cases"]})})
            try:
                business_analysis = context.get("business_analysis") or {}
                expected_scenarios = business_analysis.get("scenarios") or None
                required_scenario_ids = context.get("required_scenario_ids")
                if required_scenario_ids is None and expected_scenarios:
                    required_scenario_ids = [row["id"] for row in expected_scenarios if row["applicable"]]
                control_results = validate_plan(
                    plan, work, max_cases,
                    required_case_kinds=context.get("required_case_kinds", []),
                    required_scenario_ids=required_scenario_ids or [],
                    expected_scenarios=expected_scenarios,
                    expected_requirements=context.get("contract"),
                    expected_execution_plan=context.get("expected_execution_plan"))
            except PlanValidationError as exc:
                failed_evidence = {**exc.evidence, "suite": suite, "attempt": attempt + 1,
                                   "plan_sha256": digest(plan), "plan_snapshot": plan,
                                   "revision_failures": revision_failures}
                write_json(Path(work) / "plan-controls" / "attempts" /
                           f"{suite}-{attempt + 1:02}.json", failed_evidence)
                write_json(Path(work) / "plan-controls" / f"{suite}.json", failed_evidence)
                raise
            control_evidence = {"schema_version": "skill-refiner.plan-controls/v1", "suite": suite,
                                "attempt": attempt + 1, "status": "passed", "plan_sha256": digest(plan),
                                "plan_snapshot": plan,
                                "case_control_results": control_results["case_control_results"],
                                "control_failures": [], "coverage_failures": [],
                                "revision_failures": revision_failures}
            write_json(Path(work) / "plan-controls" / "attempts" /
                       f"{suite}-{attempt + 1:02}.json", control_evidence)
            write_json(Path(work) / "plan-controls" / f"{suite}.json", control_evidence)
            if revision_failures:
                raise RefinerError("Plan revision self-check failed after full controls: "
                                   + json.dumps(revision_failures, ensure_ascii=False))
        except Exception as exc:
            phase("plan_self_check", "failed", attempt=attempt + 1, reason=str(exc))
            # One bounded correction for malformed coverage/oracles; never silently accept a bad suite.
            if attempt + 1 >= max_attempts:
                raise PlanGateError(f"Invalid autonomous test plan: {exc}") from exc
            feedback.append(str(exc))
            previous_plan = current_plan
            continue
        phase("plan_self_check", "passed", attempt=attempt + 1,
              controls=len(control_results.get("case_control_results", [])))
        phase("independent_review", "running", attempt=attempt + 1)
        if on_self_check_passed:
            on_self_check_passed(plan)
        try:
            review = agent.ask("plan-review", "Independently review the test plan against requirements and "
                           "the source Skill. Reject superficial assertions, circular implementation-derived "
                           "oracles, missing critical coverage, answer leakage in prompts, impossible fixtures, "
                           "and claims that require unprovided binary/context files. No stylistic nitpicks. "
                           "control_command is a synthetic example used only to validate grader discrimination; "
                           "it is never executed. An illustrative installed path in that field is valid when it "
                           "matches the command regex. Do not confuse it with the actual user task or executor path. "
                           "Reject invented absolute/relative path spelling requirements when both identify the installed script or fixture. "
                           "A known-good control must reflect correct behavior. For EACH case inspect the fixture "
                           "Skill instructions, supporting implementation and expected executor actions. Installed "
                           "source is immutable; task-workspace candidate copies are writable. Reject a fixture that "
                           "tells its evaluated agent to edit its own installed implementation, including indirect "
                           "repair-on-failure directions that turn the intended baseline FAIL into ERROR. Cite the "
                           "specific fixture instruction and executable behavior in case_contracts.basis, and mark "
                           "conflicting cases false. Never approve solely because the surrounding prompt prohibits "
                           "edits while the fixture itself demands them. Separately assess response_contract_consistent: "
                           "JSON and unchanged-stdout checks reject extra prose, so reject prompts allowing optional "
                           "explanations/links/summaries alongside the answer. For every tool_output_matches check, "
                           "compare good_response with the complete observed output justified by the supplied source, "
                           "fixture or observed run evidence; reject every expected_exit_code=0 value because a success "
                           "status is sufficient and adapters may omit numeric codes. When a command legitimately exits nonzero, ensure the selected "
                           "adapter exposes its exact status or require a helper that records actual subprocess argv, "
                           "exit code and stdout. Reject partial JSON controls even if individual field "
                           "checks pass. For dynamic values such as saved paths, prefer json_path_matches_file "
                           "to compare the response with captured evidence instead of checking only its shape. "
                           "For paraphrasable user-facing explanations, reject regex or contains oracles that demand "
                           "one exact adjective when equivalent wording fulfills the contract; require a justified "
                           "alternation or structure the expected choices instead. For prose in JSON string fields, "
                           "prefer json_path_regex, which matches decoded values and handles escaped quotes; reject "
                           "field regexes that stop at an escaped quote. For duration questions, accept common "
                           "wording such as 多长时间, 多长, 每段分别多长 or 需要多久 as well as 时长/秒数. Treat good_response as a "
                           "synthetic control, never as evidence of the target's output "
                           "vocabulary. Every exact status/state value must be stated in the case prompt or grounded "
                           "in the documented Skill contract; implementation literals alone do not define the "
                           "evaluated agent's response vocabulary. Otherwise reject the invented enum/check. If a test adds "
                           "a conversational JSON response schema, its prompt must state the allowed status enum. "
                           "For discovery explanations, count 'was not returned by discovery' and 'does not appear "
                           "in the catalog' as valid absence wording. For consent checks, include '有权使用', "
                           "'允许使用', and '已获得授权' as equivalent permission wording. Evaluate text checks "
                           "against the complete meaning required, rather than requiring the synthetic good_response "
                           "wording. When an alternative media mode needs user consent, ensure the question tells "
                           "the user what material or capability will be dropped and retains the requested subject "
                           "when the available source gives it. "
                           "For missing media quantity, accept wording such as '生成段数', '生成几段', '每段分别多长' "
                           "or '每段的时长', not only "
                           "the generic noun '数量'. When the supplied reference URL is explicitly identified as "
                           "the image input, accept 'this URL/link will not be used as a reference' or 'no image "
                           "URL/reference field will be submitted or attached' as a clear explanation that the "
                           "image is not used. Include equivalent Chinese such as '不提交图片 URL', '不附带参考图', "
                           "'不将该链接作为图片输入', '不把该链接提交为参考图', or '不支持将图片作为生成输入'; "
                           "do not require the word 'image' or 'photo' when the omitted input is explicit. "
                           "For natural-language response checks, include multiple distinct good-response paraphrase "
                           "controls, including wording likely used by another capable agent; validate every control "
                           "against every response oracle and ensure the bad control still contradicts a required behavior. "
                           "When a case requires JSON only, "
                           "verify the checks reject prose before or after the JSON object; json_path_equal alone can "
                           "pass on embedded JSON. "
                           "Assess trace_checks_observable: tool_command and tool_command_absent see direct command strings, "
                           "while tool_call_absent matches recorded tool names, including non-command tools. For a "
                           "clarification-only task that explicitly forbids every tool call, require that in the prompt "
                           "and use tool_call_absent with a nonempty-name pattern and control_tool_name; this proves "
                           "no observed tool action occurred. It does not prove OS-level network isolation for an "
                           "allowed command. A CLI invoked through a helper's Python "
                           "subprocess argv or heredoc is not observable as a literal shell CLI command. If that is "
                           "allowed by the task, an inner-command regex makes a valid execution fail; instead check "
                           "the visible helper and its actual captured-process assertions. Cite concrete prompt and "
                           "checker pairs in the basis. Ensure each trace check accepts every invocation path the "
                           "prompt allows. If tool_command is used for a helper, verify the case prompt requires its "
                           "standalone invocation and supplies required setup files; a heredoc or inline wrapper can "
                           "hide that invocation from the transcript. For dimensions and similar clarification "
                           "concepts, accept common user wording such as width/height and resolution, not just the "
                           "formal label. For generated media requests, verify the fixture checks all salient "
                           "user-specified attributes in the actual submitted prompt, including color when given. "
                           "For capability explanations, accept equivalent absence wording such as no i2v capability "
                           "or provides no image-to-video support when structured checks independently establish "
                           "the discovered and requested operations. "
                           "For json_path_matches_file, good_response may contain dynamic example values: the plan "
                           "validator synthesizes matching control evidence, while the runtime fixture must write "
                           "the real observed evidence. Do not demand a static expected-path fixture for dynamic "
                           "values. If file_json_path_equal checks inspect other fields in the same file, ensure the "
                           "known-good files fixture supplies every checked pointer consistently with good_response; "
                           "the runtime helper must overwrite/remove that control fixture and capture fresh evidence. "
                           "Approve only a useful executable suite. For EACH case set scenario_consistent, "
                           "requirement_trace_consistent, expected_outcome_consistent and machine_checks_consistent "
                           "to true only when the frozen scenario and expected_outcome align with linked requirement "
                           "IDs and their sources and every check's expected value actually verifies that outcome. "
                           "Also inspect asset_audit_consistent, matrix_complete, positive_controls_validated and "
                           "negative_controls_targeted against the supplied machine control results. Every positive "
                           "control must pass every response assertion. Every negative control must fail exactly its "
                           "declared assertion_id while all other response assertions pass. Reject a control that "
                           "fails only because a status/state field differs while its target business-meaning "
                           "assertion still passes. Confirm every previous case ID remains in a revision. "
                           "Reject approval when any case is unassessed, duplicated, unknown, or marked false. "
                           "For each case also assess acceptance_criteria_traceable, scenario_detail_complete, "
                           "and risk_coverage_complete. Map scenario_id to the matching detailed scenario; map the "
                           "exact atomic acceptance criteria to linked requirements; and require observable checks "
                           "for each expected result. Inspect preconditions, stimulus, ordered actions, expected "
                           "observables, forbidden outcomes and risk dimensions. Require separate cases when actor, "
                           "workflow state, permissions, dependencies, side effects or recovery paths change the result. "
                           "Reject paraphrased duplicates or omitted risks used to fit a case cap. If creation "
                           "business_analysis is supplied, preserve every applicable scenario ID and its frozen detail "
                           "in both development and acceptance suites; missing coverage blocks approval. "
                           + HELPER_EXECUTION_AUDIT + " "
                           + CONVERSATIONAL_JSON_AUDIT + " " + INPUT_BOUNDARY_AUDIT,
                           {"context": context, "plan": plan, "control_results": control_results,
                            "previous_case_ids": sorted({row["id"] for row in previous_plan.get("cases", [])})
                           if previous_plan else []}, PLAN_REVIEW)
        except RefinerError as exc:
            invalid_contract = "invalid contract" in str(exc).lower()
            phase("independent_review", "failed" if invalid_contract else "error",
                  attempt=attempt + 1, reason=str(exc))
            if invalid_contract:
                raise PlanGateError(f"Independent review did not satisfy its JSON schema: {exc}") from exc
            raise
        _persist_plan_review(work, suite, attempt + 1, plan, review)
        try:
            validate_plan_review(review, plan)
        except RefinerError as exc:
            phase("independent_review", "failed", attempt=attempt + 1, reason=str(exc))
            if attempt + 1 >= max_attempts:
                raise PlanGateError(f"Independent test review did not pass its schema/coverage gate: {exc}") from exc
            feedback.append(str(exc))
            continue
        if review["approved"]:
            phase("independent_review", "passed", attempt=attempt + 1,
                  assessed_cases=len(review["case_contracts"]))
            _persist_plan_review(work, suite, attempt + 1, plan, review)
            return plan
        phase("independent_review", "failed", attempt=attempt + 1, issues=review["issues"])
        if attempt + 1 >= max_attempts:
            raise PlanGateError("Independent test review rejected the plan: " + "; ".join(review["issues"]))
        feedback.extend(review["issues"])
        previous_plan = current_plan
    raise RefinerError("Unable to construct a valid plan")


def make_plan_suites(agent, context, work, max_cases, acceptance_cases, *, max_attempts=2,
                     phase_callback=None, agent_parallelism=2, wave_callback=None):
    """Plan development and acceptance; overlap only after requirements are frozen."""
    def make_acceptance(plan):
        return make_plan(agent, {**context, "contract": plan["requirements"],
                                 "stage_contracts": plan["stage_contracts"],
                                 "expected_execution_plan": plan["execution_plan"]}, work,
                         acceptance_cases, "acceptance-plan", max_attempts,
                         phase_callback=phase_callback, suite="acceptance")

    if agent_parallelism not in {1, 2}:
        raise RefinerError("The safe planning wave supports agent_parallelism 1 or 2")
    if agent_parallelism == 1:
        if wave_callback:
            wave_callback("serial", **parallel_agent_plan(1))
        development_status, acceptance_status = "not_started", "not_started"
        try:
            if wave_callback:
                wave_callback("task_started", task_id="AG-DEV-REVIEW", task_status="running")
            plan = make_plan(agent, context, work, max_cases, max_attempts=max_attempts,
                             phase_callback=phase_callback, suite="development")
            development_status = "passed"
            if wave_callback:
                wave_callback("task_finished", task_id="AG-DEV-REVIEW", task_status="passed")
                wave_callback("task_started", task_id="AG-ACCEPTANCE-PLAN", task_status="running")
            acceptance = make_acceptance(plan)
            acceptance_status = "passed"
            if wave_callback:
                wave_callback("task_finished", task_id="AG-ACCEPTANCE-PLAN", task_status="passed")
        except Exception as exc:
            if development_status == "passed":
                acceptance_status = "failed"
            else:
                development_status = "failed"
            if wave_callback:
                wave_callback("failed", task_results=[
                    {"id": "AG-DEV-REVIEW", "status": development_status,
                     **({"error": str(exc)} if development_status == "failed" else {})},
                    {"id": "AG-ACCEPTANCE-PLAN", "status": acceptance_status,
                     **({"error": str(exc)} if acceptance_status == "failed" else {})},
                ], error=str(exc))
            raise
        if wave_callback:
            wave_callback("passed", task_results=[
                {"id": "AG-DEV-REVIEW", "status": "passed"},
                {"id": "AG-ACCEPTANCE-PLAN", "status": "passed"},
            ], development_status="passed", acceptance_status="passed")
        if plan["execution_plan"] != acceptance["execution_plan"]:
            raise PlanGateError("Development and independent acceptance plans do not share the same frozen execution_plan")
        return plan, acceptance
    acceptance_future = None
    with ThreadPoolExecutor(max_workers=2, thread_name_prefix="skill-refiner-plan") as pool:
        def start_acceptance(plan):
            nonlocal acceptance_future
            if acceptance_future is not None:
                return
            if wave_callback:
                wave_callback("running", **parallel_agent_plan(2))
            acceptance_future = pool.submit(make_acceptance, plan)

        try:
            plan = make_plan(agent, context, work, max_cases, max_attempts=max_attempts,
                             phase_callback=phase_callback, suite="development",
                             on_self_check_passed=start_acceptance)
            if wave_callback and acceptance_future is not None:
                wave_callback("task_finished", task_id="AG-DEV-REVIEW", task_status="passed")
        except Exception as exc:
            peer_error = None
            if acceptance_future is not None:
                try:
                    acceptance_future.result()
                except Exception as other:
                    peer_error = other
            if wave_callback and acceptance_future is not None:
                wave_callback("failed", error=str(peer_error or exc),
                              development_error=str(exc), acceptance_error=str(peer_error) if peer_error else None,
                              task_results=[
                                  {"id": "AG-DEV-REVIEW", "status": "failed", "error": str(exc)},
                                  {"id": "AG-ACCEPTANCE-PLAN",
                                   "status": "failed" if peer_error else "passed",
                                   **({"error": str(peer_error)} if peer_error else {})},
                              ])
            if peer_error and not isinstance(exc, Cancelled):
                raise PlanGateError(f"Development planning failed: {exc}; concurrent acceptance planning also failed: {peer_error}") from exc
            raise
        if acceptance_future is None:
            acceptance = make_acceptance(plan)
        else:
            try:
                acceptance = acceptance_future.result()
                if wave_callback:
                    wave_callback("task_finished", task_id="AG-ACCEPTANCE-PLAN", task_status="passed")
            except Exception as exc:
                if wave_callback:
                    wave_callback("failed", error=str(exc), development_status="passed", acceptance_status="failed",
                                  task_results=[{"id": "AG-DEV-REVIEW", "status": "passed"},
                                                {"id": "AG-ACCEPTANCE-PLAN", "status": "failed", "error": str(exc)}])
                raise
    if wave_callback and agent_parallelism == 2:
        wave_callback("passed", development_status="passed", acceptance_status="passed",
                      task_results=[{"id": "AG-DEV-REVIEW", "status": "passed"},
                                    {"id": "AG-ACCEPTANCE-PLAN", "status": "passed"}])
    if plan["execution_plan"] != acceptance["execution_plan"]:
        raise PlanGateError("Development and independent acceptance plans do not share the same frozen execution_plan")
    return plan, acceptance


def _review_frozen_plan(agent, plan, context, control_results, *, phase_callback, suite):
    def phase(status, **detail):
        if phase_callback:
            phase_callback("independent_review", status, suite=suite, **detail)

    phase("running", case_ids=[case["id"] for case in plan["cases"]])
    try:
        review_schema = {"skill-refiner.plan/v2": PLAN_REVIEW_V2,
                         "skill-refiner.plan/v3": PLAN_REVIEW_V3,
                         "skill-refiner.plan/v4": PLAN_REVIEW_V4}.get(
                             plan.get("schema_version"), PLAN_REVIEW)
        review = agent.ask("plan-review",
                           "Independently review this already-frozen Skill test plan against the source "
                           "requirements, scenarios, expected outcomes, assets, response assertions and "
                           "the complete positive/negative/auxiliary control results. Verify every case "
                           "exactly once. Every response assertion must express the linked business meaning; "
                           "every positive paraphrase must pass all response assertions; each negative control "
                           "must fail its declared assertion and pass every other assertion. A status-field "
                           "failure cannot substitute for detecting the intended business error. Check that all "
                           "available assets are supported by the original inventory or declared fixtures. "
                           "Do not narrow checks to a single wording when equivalent phrases meet the contract. "
                           "Return exactly the supplied PLAN_REVIEW JSON schema. Cite case and assertion IDs "
                           "in each issue. For v3 cases assess atomic-criterion traceability, concrete scenario "
                           "detail and workflow-specific risk coverage. Verify preconditions, stimulus, ordered "
                           "actions, observable expected results and forbidden outcomes; reject broad, duplicated "
                            "or paraphrased scenarios. For v4/v5, independently assess every stage/artifact schema link, "
                            "safe schema path, target-runtime packaging need and all four validator lifecycle points; "
                            "set each stage_contracts review flag from evidence. For v5, also inspect the full execution_plan: "
                            "assess every ordered step exactly once, verify requirement/case traceability, input/output evidence, "
                            "dependency DAG, nonoverlapping parallel write scopes, explicit serial reasons, bounded failure "
                            "attempts and same-run recovery. Set all execution_plan review flags and step_assessments from "
                            "the plan evidence; approval is allowed only when every required flag is true and the complete "
                           "plan is useful. " + SELF_REVIEW_PLAN_REVIEW + " " + LANGUAGE_PLAN_REVIEW,
                           {"context": context, "plan": plan, "control_results": control_results,
                            "previous_case_ids": [case["id"] for case in plan["cases"]]}, review_schema)
        _persist_plan_review(agent.work, suite, 1, plan, review)
        validate_plan_review(review, plan)
    except RefinerError as exc:
        phase("failed", reason=str(exc))
        raise PlanGateError(f"Independent review did not pass for reused {suite} plan: {exc}") from exc
    if not review["approved"]:
        phase("failed", issues=review["issues"])
        raise PlanGateError(f"Independent review rejected reused {suite} plan: " + "; ".join(review["issues"]))
    phase("passed", assessed_cases=len(review["case_contracts"]), reused=True)
    return review


def _persist_plan_review(work, suite, attempt, plan, review):
    record = {"schema_version": "skill-refiner.plan-review/v1", "suite": suite,
              "attempt": attempt, "status": "passed" if review.get("approved") else "rejected",
              "plan_sha256": digest(plan), "plan_snapshot": plan, "review": review}
    write_json(Path(work) / "plan-reviews" / "attempts" / f"{suite}-{attempt:02}.json", record)
    if review.get("approved"):
        write_json(Path(work) / "plan-reviews" / f"{suite}.json", record)


def validate_changes(candidate, response, evidence_ids):
    import yaml
    # Protect native suite graders even when they live outside evals/.
    protected = set()
    eval_dir = Path(candidate) / "evals"
    for config in list(eval_dir.rglob("*.yaml")) + list(eval_dir.rglob("*.yml")):
        def collect(value):
            if isinstance(value, dict):
                for key, item in value.items():
                    if key in {"script_path", "golden_file"} and isinstance(item, str):
                        path = Path(item)
                        if not path.is_absolute():
                            protected.add(path.as_posix())
                    collect(item)
            elif isinstance(value, list):
                for item in value:
                    collect(item)
        collect(yaml.safe_load(config.read_text(encoding="utf-8")))
    changes = response["changes"]
    if len(changes) > 20 or sum(len(c["content"].encode()) for c in changes) > 500_000:
        raise RefinerError("Repair exceeds bounded patch size")
    seen, validated = set(), []
    # Validate every path before writing any file.
    for change in changes:
        rel = change["path"]
        path = relative_path(candidate, rel)
        if (rel in seen or rel in protected or Path(rel).parts[0] in {"evals", "tests", ".git", ".refiner", ".codex", ".agents", ".cursor", ".claude"}
                or Path(rel).name.startswith("test_") or ".test." in Path(rel).name
                or Path(rel).stem.endswith("_test")):
            raise RefinerError(f"Repair attempted to modify protected/duplicate path: {rel}")
        if not set(change["evidence_ids"]) <= evidence_ids:
            raise RefinerError(f"Repair lacks valid failure/standards evidence: {rel}")
        if path.suffix not in {".md", ".py", ".sh", ".json", ".yaml", ".yml", ".txt", ".js", ".ts", ".toml"}:
            raise RefinerError(f"Unsupported repair file type: {rel}")
        seen.add(rel)
        validated.append((path, change))
    return validated


def expected_candidate_manifest(base, response, evidence_ids):
    base = Path(base)
    expected = manifest(base)
    for path, change in validate_changes(base, response, evidence_ids):
        expected[str(path.relative_to(base))] = hashlib.sha256(change["content"].encode("utf-8")).hexdigest()
    return expected


def apply_changes(candidate, response, evidence_ids):
    for path, change in validate_changes(candidate, response, evidence_ids):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(change["content"], encoding="utf-8")


def resumable_candidate(base, response, evidence_ids, destination):
    """Reuse a complete candidate, finish a clean snapshot, or preserve partial work and retry safely."""
    base, destination = Path(base), Path(destination)
    expected = expected_candidate_manifest(base, response, evidence_ids)
    if destination.exists():
        current = manifest(destination)
        if current == expected:
            return destination
        if current == manifest(base):
            apply_changes(destination, response, evidence_ids)
            if manifest(destination) == expected:
                return destination
        attempts = destination.parent / "resume-attempts"
        attempts.mkdir(exist_ok=True)
        numbers = [int(path.name.split("-", 1)[1]) for path in attempts.glob("attempt-*")
                   if path.is_dir() and path.name.split("-", 1)[1].isdigit()]
        destination = attempts / f"attempt-{max(numbers, default=0)+1:03}" / destination.name
    snapshot(base, destination)
    apply_changes(destination, response, evidence_ids)
    if manifest(destination) != expected:
        raise RefinerError("Resumed repair candidate does not match its frozen patch")
    return destination


def resumable_snapshot(source, destination, *, keep_destination=False):
    source, destination = Path(source), Path(destination)
    expected = manifest(source)
    if destination.exists():
        if manifest(destination) == expected:
            return destination
        attempts = destination.parent / "resume-attempts"
        attempts.mkdir(exist_ok=True)
        numbers = [int(path.name.split("-", 1)[1]) for path in attempts.glob("attempt-*")
                   if path.is_dir() and path.name.split("-", 1)[1].isdigit()]
        attempt = attempts / f"attempt-{max(numbers, default=0)+1:03}" / destination.name
        if keep_destination:
            run_root = destination.parent.parent.resolve()
            if (destination.is_symlink() or not destination.resolve().is_relative_to(run_root)
                    or not attempt.resolve().is_relative_to(run_root)):
                raise RefinerError("Cannot safely preserve an incomplete snapshot outside its run directory")
            attempt.parent.mkdir(parents=True, exist_ok=True)
            destination.rename(attempt)
        else:
            destination = attempt
    snapshot(source, destination)
    if manifest(destination) != expected:
        raise RefinerError("Resumed snapshot differs from its source")
    return destination


def standards_review(agent, target, context, work, expected_name):
    findings = lint(target, expected_name)
    review = agent.ask("standards-review", "Review this candidate against the supplied authoring principles "
                       "and external standards. Check preservation of identity, scope, discoverable references "
                       "and correctness of script/documentation relationships. Do not require arbitrary headings, "
                       "examples, length padding or new features. Only reject concrete material violations, "
                       "quoting the file/section and applicable rule in each issue. Do not grade execution "
                       "by reading prose; behavior is evaluated separately. Missing necessary evidence is a gap.",
                       {"files": inventory(target, max_bytes=300_000), "standards": context["standards"],
                        "requirements": context["requirements"], "static_findings": findings}, REVIEW)
    for i, issue in enumerate(review["issues"] if not review["approved"] else []):
        findings.append({"id": f"N-{i+1:03}", "rule": "authoring.review", "path": "SKILL.md",
                         "message": issue, "severity": "error"})
    write_json(Path(work) / "standards.json", {"findings": findings, "review": review})
    return findings


def score(result, findings):
    return (sum(r["status"] == "ERROR" for r in result["cases"]),
            sum(f["severity"] == "error" for f in findings),
            sum(not r["passed"] for r in result["cases"]))


def qualifies(result, findings, unresolved=()):
    return not unresolved and score(result, findings) == (0, 0, 0)


def source_references(rows, default_prefix):
    """Carry provenance for large source artifacts without repeating their contents."""
    result = []
    for index, row in enumerate(rows):
        if isinstance(row, dict):
            source, content = row.get("source", f"{default_prefix}:{index+1}"), row.get("content", "")
        else:
            source, content = f"{default_prefix}:{index+1}", str(row)
        data = content.encode("utf-8")
        result.append({"source": source, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()})
    return result


def linked_source_paths(root, findings):
    """Prioritize the entrypoint, cited files and local references linked by the Skill."""
    root = Path(root)
    paths = {"SKILL.md"}
    paths.update(str(item.get("path")) for item in findings if item.get("path"))
    scan = [root / "SKILL.md"]
    scan.extend((root / "references").glob("*.md") if (root / "references").is_dir() else [])
    for source in scan:
        try:
            content = source.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            continue
        for ref in re.findall(r"\]\(([^)]+)\)", content):
            value = ref.split("#", 1)[0]
            if not value or "://" in value or value.startswith(("mailto:", "/")):
                continue
            candidate = (source.parent / value).resolve()
            if candidate.is_relative_to(root.resolve()) and candidate.is_file():
                paths.add(candidate.relative_to(root.resolve()).as_posix())
    return paths


def compare_candidate(baseline_passed, best_result, best_findings, current, current_findings,
                      best_unresolved=(), current_unresolved=()):
    protected = baseline_passed | {r["id"] for r in best_result["cases"] if r["passed"]}
    current_passed = {r["id"] for r in current["cases"] if r["passed"]}
    regressed = sorted(protected - current_passed)
    # Self-reported work remaining is not a newly observed standards violation.
    # Retain proven partial improvement, but gate final qualification separately.
    before = (*score(best_result, best_findings), len(best_unresolved))
    after = (*score(current, current_findings), len(current_unresolved))
    return not regressed and after < before, regressed


def combine_results(development, acceptance):
    """Compare a candidate across both frozen suites without conflating case IDs."""
    if acceptance is None:
        return development
    extra = []
    for row in acceptance.get("cases", []):
        case_id = row.get("case_id", row["id"])
        extra.append({**row, "id": f"acceptance::{case_id}", "case_id": case_id,
                      "suite": "acceptance"})
    return {**development, "passed": development.get("passed", False) and acceptance.get("passed", False),
            "cases": [*development.get("cases", []), *extra],
            "acceptance_report": acceptance.get("upstream_report")}


def _persisted_evaluation_cost(work):
    total = 0
    for path in Path(work).rglob("resume-cache.json"):
        try:
            total += max(0, int(read_json(path).get("evaluations_used", 0)))
        except (OSError, ValueError, TypeError):
            continue
    return total


def run(args):
    config = load_config(getattr(args, "config", None))
    if config and (args.agent_command or args.engine_config or args.engine is not None):
        raise RefinerError("Use --config alone for backend selection; legacy engine/command flags cannot be mixed")
    if not config and not args.agent_command:
        raise RefinerError("Select a controller backend with --config or --agent-command")
    if not config and not args.engine_config and args.engine is None:
        raise RefinerError("Select an evaluation backend with --config, --engine-config or --engine")
    source = Path(args.target).resolve()
    if not (source / "SKILL.md").is_file():
        raise RefinerError(f"No SKILL.md in {source}")
    resume_from = Path(args.resume_from).resolve() if getattr(args, "resume_from", None) else None
    if resume_from and getattr(args, "reuse_plan", None):
        raise RefinerError("--reuse-plan starts a new run; do not combine it with --resume-from")
    standard_paths = [] if resume_from else discover_project_standards(source, getattr(args, "standard", ()))
    if not resume_from:
        for reference in args.spec + standard_paths:
            if not Path(reference).is_file():
                raise RefinerError(f"Reference file does not exist: {reference}")
    if resume_from:
        if args.output and Path(args.output).resolve() != resume_from:
            raise RefinerError("--output cannot point somewhere else when resuming; --resume-from owns the run directory")
        work = resume_from
        if not work.is_dir() or work.is_relative_to(source):
            raise RefinerError("--resume-from must name an existing run directory outside the target Skill")
    else:
        work = Path(args.output).resolve() if args.output else source.parent / (
            source.name + "-refiner-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ"))
        if work.exists() or work.is_relative_to(source):
            raise RefinerError("Output must be a new directory outside the target Skill")
        work.mkdir(parents=True)
    controller_root = Path(__file__).resolve().parent.parent
    controller_sha256 = digest({path.relative_to(controller_root).as_posix():
        hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(controller_root.rglob("*.py"))
        if "__pycache__" not in path.parts})
    started = time.monotonic()
    previous_state, resume_contract = None, None
    elapsed_before = 0.0
    if resume_from:
        contract_path = work / "resume-contract.json"
        if not contract_path.is_file():
            raise RefinerError("This run predates resumable checkpoints or has no resume contract; start a new run")
        resume_contract = read_json(contract_path)
        if resume_contract.get("schema_version") != "skill-refiner.resume/v1":
            raise RefinerError("Unsupported run resume contract")
        if resume_contract.get("controller_sha256") != controller_sha256:
            raise RefinerError("Refiner implementation changed since this run; start a new run")
        previous_state = read_json(work / "state.json")
        if previous_state.get("source") != str(source) or resume_contract.get("source") != str(source):
            raise RefinerError("Resume target path differs from the original run")
        original_manifest = resume_contract.get("source_manifest")
        original = work / "original" / source.name
        if (manifest(source) != original_manifest or not original.is_dir()
                or manifest(original) != original_manifest):
            raise RefinerError("Resume source or immutable baseline changed; refusing to reuse checkpoints")
        context = read_json(work / "inputs.json")
        if digest(context) != resume_contract.get("inputs_sha256"):
            raise RefinerError("Resume inputs changed; start a new run with the updated requirements")
        if (work / "oracle.lock.json").is_file():
            saved_lock = read_json(work / "oracle.lock.json")
            saved_plan = read_json(work / "test-plan.json")
            saved_acceptance = read_json(work / "acceptance-plan.json")
            if (saved_lock.get("source_manifest") != original_manifest
                    or saved_lock.get("sha256") != digest({"plan": saved_plan,
                        "acceptance": saved_acceptance, "inputs": context})):
                raise RefinerError("Frozen plans or oracle lock changed; refusing to resume")
        elapsed_before = float(previous_state.get("elapsed_seconds", 0.0))
        if previous_state.get("status") in {"passed", "unchanged"} and previous_state.get("verification_status") == "verified":
            from .gate import verify_run
            verify_run(work)
            return previous_state
    else:
        original_manifest = manifest(source)
        original = work / "original" / source.name
        snapshot(source, original)
        context = {"files": inventory(
            original, max_bytes=275_000,
            priority_paths=("scripts/refiner/pipeline.py", "scripts/refiner/checks.py",
                            "scripts/refiner/plan.py", "scripts/refiner/schemas.py",
                            "scripts/refiner/stages.py", "SKILL.md")),
            "requirements": [], "standards": [AUTHORING]}
        context.update(getattr(args, "creation_context", {}))
        for path in args.spec:
            context["requirements"].append({"source": str(Path(path).resolve()), "content": Path(path).read_text(encoding="utf-8")})
        for path in standard_paths:
            context["standards"].append({"source": str(Path(path).resolve()), "content": Path(path).read_text(encoding="utf-8")})
        write_json(work / "inputs.json", context)
    state = {"status": "planning", "source": str(source), "workspace": str(work),
             "rounds": [], "limitations": ["Uses local environment.type=none; no container isolation",
             "Agent role separation uses fresh contexts; it is not an OS-level read barrier",
             "Declarative response/file checks only; no automatic domain-specific executable graders",
             "Small acceptance sets provide observed evidence, not statistical reliability proof"],
             "phase_results": initial_phase_results()}
    if resume_from:
        state = previous_state
        state.setdefault("rounds", [])
        history_dir = work / "resume-history"
        history_dir.mkdir(exist_ok=True)
        resume_number = len(list(history_dir.glob("state-*.json"))) + 1
        write_json(history_dir / f"state-{resume_number:03}.json", previous_state)
        stage_data = read_json(work / "stages.json")
        state.setdefault("resume_history", []).append({"resume_number": resume_number,
            "previous_status": previous_state.get("status"), "previous_active_stage": stage_data.get("active_stage"),
            "elapsed_seconds": elapsed_before})
        state.update(status="resuming", elapsed_seconds=elapsed_before)
        for key in ("error", "gaps", "verification_status", "verification_error", "final_skill"):
            state.pop(key, None)
    else:
        write_json(work / "state.json", state)
    state.setdefault("phase_results", initial_phase_results())
    state["agent_parallelism"] = getattr(args, "agent_parallelism", 2)
    agent = Agent(work, args.agent_command, args.timeout, config=config)
    agent.max_calls = args.max_agent_calls
    engine_config = read_json(args.engine_config) if args.engine_config else None
    execution = {"backend_config": config, "agent_command": args.agent_command, "engine": args.engine,
                 "engine_config": engine_config, "skill_up": args.skill_up, "timeout": args.timeout,
                 "parallelism": args.parallelism,
                 "agent_parallelism": getattr(args, "agent_parallelism", 2)}
    options = {"max_rounds": args.max_rounds, "planning_attempts": getattr(args, "planning_attempts", 2),
               "max_cases": args.max_cases, "acceptance_cases": args.acceptance_cases}
    limits = {"max_seconds": args.max_seconds, "max_agent_calls": args.max_agent_calls,
              "max_evaluations": args.max_evaluations, "max_rounds": args.max_rounds,
              "planning_attempts": getattr(args, "planning_attempts", 2),
              "max_cases": args.max_cases, "acceptance_cases": args.acceptance_cases}
    state["limits"] = limits
    state.setdefault("evaluation_history", [])
    if resume_from:
        if digest(execution) != resume_contract.get("execution_sha256"):
            raise RefinerError("Resume backend or execution settings differ from the original run")
        prior_options = resume_contract.get("options", {})
        if (options["max_cases"] != prior_options.get("max_cases")
                or options["acceptance_cases"] != prior_options.get("acceptance_cases")
                or options["planning_attempts"] < prior_options.get("planning_attempts", 1)
                or options["max_rounds"] < prior_options.get("max_rounds", 1)):
            raise RefinerError("Resume may increase planning/round budgets, but must keep frozen case limits")
        prior_limits = resume_contract.get("limits", {})
        if any(limits[name] < prior_limits.get(name, 1) for name in limits):
            raise RefinerError("Resume budgets cannot be lower than the original run; pass equal or larger limits")
        resume_contract["options"] = options
        resume_contract["limits"] = limits
        write_json(work / "resume-contract.json", resume_contract)
    else:
        resume_contract = {"schema_version": "skill-refiner.resume/v1", "source": str(source),
                           "source_manifest": original_manifest, "inputs_sha256": digest(context),
                           "execution_sha256": digest(execution), "controller_sha256": controller_sha256,
                           "options": options, "limits": limits}
        write_json(work / "resume-contract.json", resume_contract)
    remaining_seconds = max(0.0, args.max_seconds - elapsed_before)
    agent.deadline = started + remaining_seconds
    stages = StageManifest(work, "refine", resume=bool(resume_from))
    stages.complete("inputs")
    print(json.dumps({"status": "started", "workspace": str(work),
                      "work_plan": str(work / "work-plan.md"),
                      "state_file": str(work / "state.json"),
                       "stage_manifest": str(work / "stages.json"), "limits": limits,
                      "max_rounds": args.max_rounds}, ensure_ascii=False), flush=True)
    locked = None
    checker_file = Path(__file__).with_name("checks.py")
    checker_hash = hashlib.sha256(checker_file.read_bytes()).hexdigest()
    helper_file = Path(__file__).with_name('python_probe.py')
    helper_hash = hashlib.sha256(helper_file.read_bytes()).hexdigest()
    if resume_from and (work / "oracle.lock.json").is_file():
        prior_lock = read_json(work / "oracle.lock.json")
        if prior_lock.get("checker_sha256") != checker_hash:
            raise RefinerError("Resume controller grader differs from the frozen run; start a new run")
        if prior_lock.get("python_probe_sha256") != helper_hash:
            raise RefinerError("Resume Python observer differs from the frozen run; start a new run")

    def evaluate(target, cases, run_dir, *, selected=None, existing=True):
        require_plan_gate(state)
        run_dir = Path(run_dir)
        suite_name = run_dir.relative_to(work).as_posix()
        suite = "acceptance" if "acceptance" in Path(suite_name).parts else "development"
        evaluation_phase = ("acceptance" if suite == "acceptance" else
                            "baseline" if Path(suite_name).parts[0] == "baseline" else "regression")
        set_phase_result(state, "skill_evaluation", "running", suite=evaluation_phase,
                         path=suite_name)
        state["phase_results"]["skill_evaluation"]["status"] = "running"
        write_json(work / "state.json", state)
        print(json.dumps({"stage": "phase", "phase": "skill_evaluation",
                          "status": "running", "suite": evaluation_phase}, ensure_ascii=False), flush=True)
        selected_ids = sorted(c["id"] for c in cases if selected is None or c["id"] in selected)
        cache_identity = {"target_manifest": manifest(target), "cases_sha256": digest(cases),
                          "selected": sorted(selected) if selected is not None else None,
                          "existing": bool(existing), "execution_sha256": digest(execution),
                          "checker_sha256": checker_hash, "python_probe_sha256": helper_hash}
        cache_sha = digest(cache_identity)
        cache_path, summary_path = run_dir / "resume-cache.json", run_dir / "summary.json"
        previous_evaluation_cost = 0
        if cache_path.is_file():
            try:
                previous_evaluation_cost = max(0, int(read_json(cache_path).get("evaluations_used", 0)))
            except (OSError, ValueError, TypeError):
                pass
        if resume_from and cache_path.is_file() and summary_path.is_file():
            try:
                saved_cache, saved_result = read_json(cache_path), read_json(summary_path)
                if (isinstance(saved_cache, dict) and isinstance(saved_result, dict)
                        and isinstance(saved_result.get("cases"), list)
                        and all(isinstance(row, dict) for row in saved_result["cases"])
                        and saved_cache.get("schema_version") == "skill-refiner.evaluation-cache/v1"
                        and saved_cache.get("cache_sha256") == cache_sha
                        and saved_cache.get("result_sha256") == digest(saved_result)
                        and not any(row.get("status") == "ERROR" for row in saved_result.get("cases", []))):
                    print(json.dumps({"stage": "evaluation", "cache_hit": True,
                                      "suite": suite_name, "path": str(run_dir)}, ensure_ascii=False), flush=True)
                    rows = saved_result.get("cases", [])
                    state.setdefault("evaluation_history", []).append({
                        "suite": suite, "label": suite_name, "result": "cached",
                        "cache_hit": True, "cases": compact_case_rows(rows),
                        "seconds": saved_result.get("seconds", 0)})
                    state["evaluation_history"] = state["evaluation_history"][-100:]
                    state["target_evaluations"] = backend.evaluations
                    state["agent_calls"] = agent.calls
                    state["elapsed_seconds"] = round(elapsed_before + time.monotonic() - started, 2)
                    set_phase_result(state, "skill_evaluation",
                                     "passed" if saved_result.get("passed") else "failed",
                                     suite=evaluation_phase, cache_hit=True)
                    write_json(work / "state.json", state)
                    return saved_result
            except (OSError, ValueError, TypeError, AttributeError):
                pass

        run_dir.mkdir(parents=True, exist_ok=True)
        actual_dir = run_dir
        if any(run_dir.iterdir()):
            attempts = run_dir / "resume-attempts"
            attempts.mkdir(exist_ok=True)
            numbers = [int(p.name.split("-", 1)[1]) for p in attempts.glob("attempt-*")
                       if p.is_dir() and p.name.split("-", 1)[1].isdigit()]
            actual_dir = attempts / f"attempt-{max(numbers, default=0)+1:03}"
            actual_dir.mkdir()
        expected_engines = len(config["executors"]) if config else 1
        state["active_evaluation"] = {"suite": suite_name, "case_count": len(selected_ids),
                                       "case_ids": selected_ids, "started_at": datetime.now(timezone.utc).isoformat(),
                                       "executors": ([profile["id"] for profile in config["executors"]]
                                                     if config else ["default"]),
                                       "expected_case_evaluations": len(selected_ids) * expected_engines}
        state["target_evaluations"] = backend.evaluations
        state["agent_calls"] = agent.calls
        state["elapsed_seconds"] = round(elapsed_before + time.monotonic() - started, 2)
        write_json(work / "state.json", state)
        print(json.dumps({"stage": "evaluation", "status": "started", "suite": suite_name,
                          "case_ids": selected_ids, "expected_case_evaluations": len(selected_ids) * expected_engines,
                          "max_evaluations": args.max_evaluations,
                          "time_remaining_seconds": max(0, round(args.max_seconds - state["elapsed_seconds"]))},
                         ensure_ascii=False), flush=True)
        before = backend.evaluations
        try:
            if config:
                result = backend.evaluate(target, cases, actual_dir, selected=selected, existing=existing)
            else:
                result = backend.evaluate(target, cases, actual_dir, selected=selected)
                prior = backend.evaluate_existing(target, actual_dir / "existing") if existing else None
                if prior:
                    result["cases"].extend(prior["cases"])
                    result["passed"] = result["passed"] and prior["passed"]
                    result["existing_report"] = prior["upstream_report"]
        except Exception as exc:
            state["active_evaluation"]["status"] = "error"
            state["active_evaluation"]["error"] = str(exc)
            state["target_evaluations"] = backend.evaluations
            state["elapsed_seconds"] = round(elapsed_before + time.monotonic() - started, 2)
            set_phase_result(state, "skill_evaluation", "error", suite=evaluation_phase,
                             error=str(exc))
            state["phase_results"]["skill_evaluation"]["status"] = "error"
            write_json(work / "state.json", state)
            raise
        write_json(summary_path, result)
        if "engines" in result:
            (run_dir / "comparison.md").write_text(comparison_markdown(result), encoding="utf-8")
        write_json(cache_path, {"schema_version": "skill-refiner.evaluation-cache/v1",
                   "cache_sha256": cache_sha, "result_sha256": digest(result),
                   "evaluations_used": previous_evaluation_cost + backend.evaluations - before})
        state.setdefault("evaluation_history", []).append({
            "suite": suite, "label": suite_name, "result": "passed" if result.get("passed") else "failed",
            "cache_hit": False, "cases": compact_case_rows(result.get("cases", [])),
            "seconds": result.get("seconds", 0)})
        state["evaluation_history"] = state["evaluation_history"][-100:]
        state.pop("active_evaluation", None)
        state["target_evaluations"] = backend.evaluations
        state["agent_calls"] = agent.calls
        state["elapsed_seconds"] = round(elapsed_before + time.monotonic() - started, 2)
        set_phase_result(state, "skill_evaluation",
                         "passed" if result.get("passed") else "failed",
                         suite=evaluation_phase, passed=result.get("passed"),
                         cases=len(result.get("cases", [])), cache_hit=False)
        write_json(work / "state.json", state)
        return result

    def event(status, **data):
        if status in REFINE_EVENTS:
            stage_id = REFINE_EVENTS[status]
            # A resumed controller may replay deterministic orchestration to
            # rebuild in-memory state. Keep completed stage checkpoints closed;
            # their external agent/evaluation work is loaded from validated caches.
            if not (resume_from and stages.is_completed(stage_id)):
                stages.activate(stage_id)
        state.update(status=status, agent_calls=agent.calls, cached_agent_calls=agent.cached_calls,
                     elapsed_seconds=round(elapsed_before + time.monotonic()-started, 2), **data)
        write_json(work / "state.json", state)
        print(json.dumps({"status": status, "elapsed_seconds": state["elapsed_seconds"],
                          "max_seconds": args.max_seconds, **data}, ensure_ascii=False), flush=True)

    phase_lock = threading.RLock()

    def phase_progress(name, status, **data):
        suite = data.pop("suite", None)
        with phase_lock:
            set_phase_result(state, name, status, suite=suite, **data)
            state["elapsed_seconds"] = round(elapsed_before + time.monotonic() - started, 2)
            write_json(work / "state.json", state)
            print(json.dumps({"stage": "phase", "phase": name, "status": status,
                              "suite": suite, **data}, ensure_ascii=False), flush=True)

    def planning_wave_progress(status, **data):
        with phase_lock:
            waves = state.setdefault("planning_waves", [])
            if status == "running":
                record = {"sequence": len(waves) + 1, "status": "running",
                          "started_at": datetime.now(timezone.utc).isoformat(),
                          "task_results": [{"id": task["id"], "status": "running"}
                                           for task in data.get("tasks", [])], **data}
                waves.append(record)
            elif status == "serial":
                waves.append({"sequence": len(waves) + 1, "status": "serial",
                              "task_results": [{"id": task["id"], "status": "pending"}
                                               for task in data.get("tasks", [])], **data})
            elif status in {"task_started", "task_finished"}:
                record = next((item for item in reversed(waves)
                               if item.get("status") in {"running", "serial"}), None)
                if record:
                    task_id = data["task_id"]
                    task_status = data["task_status"]
                    result = next((item for item in record.setdefault("task_results", [])
                                   if item.get("id") == task_id), None)
                    if result is None:
                        result = {"id": task_id}
                        record["task_results"].append(result)
                    result.update(status=task_status,
                                  **{key: value for key, value in data.items()
                                     if key not in {"task_id", "task_status"}})
            else:
                record = next((item for item in reversed(waves)
                               if item.get("status") in {"running", "serial"}), None)
                if record:
                    record.update(status=status, completed_at=datetime.now(timezone.utc).isoformat(), **data)
            state["elapsed_seconds"] = round(elapsed_before + time.monotonic() - started, 2)
            write_json(work / "state.json", state)
            print(json.dumps({"stage": "planning_wave", "status": status, **data},
                             ensure_ascii=False), flush=True)

    def budget():
        if elapsed_before + time.monotonic() - started > args.max_seconds or agent.calls > args.max_agent_calls:
            raise RefinerError("Iteration budget exhausted")
        if manifest(source) != original_manifest:
            raise RefinerError("Original Skill changed during the run; refusing adoption")
        if manifest(original) != original_manifest:
            raise RefinerError("Immutable baseline snapshot changed during the run")
        if locked and digest({"plan": read_json(work / "test-plan.json"),
                              "acceptance": read_json(work / "acceptance-plan.json"),
                              "inputs": read_json(work / "inputs.json")}) != locked:
            raise RefinerError("Frozen tests, acceptance or standards were modified")
        if hashlib.sha256(checker_file.read_bytes()).hexdigest() != checker_hash:
            raise RefinerError("Grader implementation changed during the run")
        if hashlib.sha256(helper_file.read_bytes()).hexdigest() != helper_hash:
            raise RefinerError('Frozen Python observer changed during the run')

    def compact_case_rows(rows):
        return [{key: row[key] for key in ("id", "case_id", "executor", "status", "passed") if key in row}
                for row in rows if isinstance(row, dict)]

    def record_round(record):
        for index, previous in enumerate(state["rounds"]):
            if (previous.get("number") == record["number"]
                    and previous.get("trigger") == record.get("trigger")):
                state["rounds"][index] = record
                break
        else:
            state["rounds"].append(record)

    try:
        backend = (Matrix(config["executors"], work, args.skill_up, args.timeout, args.parallelism) if config else
                   SkillUp(args.skill_up, args.engine, engine_config, args.timeout, args.parallelism))
        if config:
            state["backends"] = config
            write_json(work / "backend-config.json", config)
        backend.deadline = started + remaining_seconds
        backend.max_evaluations = args.max_evaluations
        if resume_from:
            backend.evaluations = _persisted_evaluation_cost(work)
        def evaluation_progress(elapsed, remaining, case_count, executor, evaluation_count):
            active = state.get("active_evaluation")
            if active:
                active.update(heartbeat_elapsed_seconds=elapsed,
                              heartbeat_remaining_timeout_seconds=remaining,
                              current_executor=executor,
                              reserved_case_evaluations=case_count)
            state["target_evaluations"] = evaluation_count
            state["agent_calls"] = agent.calls
            state["elapsed_seconds"] = round(elapsed_before + time.monotonic() - started, 2)
            write_json(work / "state.json", state)
        backend.progress_callback = evaluation_progress
        event("planning")
        phase_progress("plan_gate", "running")
        reuse = getattr(args, "reuse_plan", None)
        frozen_plan = False
        if (resume_from and all((work / name).is_file() for name in
                                ("test-plan.json", "acceptance-plan.json", "oracle.lock.json"))):
            prior_lock = read_json(work / "oracle.lock.json")
            plan = read_json(work / "test-plan.json")
            acceptance = read_json(work / "acceptance-plan.json")
            expected_lock = digest({"plan": plan, "acceptance": acceptance, "inputs": context})
            if (prior_lock.get("source_manifest") != original_manifest
                    or prior_lock.get("sha256") != expected_lock
                    or prior_lock.get("checker_sha256") != checker_hash
                    or prior_lock.get("python_probe_sha256") != helper_hash):
                raise RefinerError("Frozen plans or oracle lock changed; refusing to resume")
            for suite, frozen in (("development", plan), ("acceptance", acceptance)):
                control_path = work / "plan-controls" / f"{suite}.json"
                if not control_path.is_file():
                    raise PlanGateError(f"Cannot resume: frozen {suite} plan has no completed control evidence")
                control_record = read_json(control_path)
                if (control_record.get("status") != "passed"
                        or control_record.get("plan_sha256") != digest(frozen)
                        or control_record.get("plan_snapshot") != frozen):
                    raise PlanGateError(f"Cannot resume: frozen {suite} plan control checkpoint does not match")
                for phase_name in ("plan_generation", "plan_self_check", "independent_review"):
                    phase_entry = state.get("phase_results", {}).get(phase_name, {})
                    if phase_entry.get("suites", {}).get(suite) not in {"passed", "reused"}:
                        raise PlanGateError(f"Cannot resume: {suite} {phase_name} checkpoint is incomplete")
            frozen_plan = True
        elif reuse:
            previous = Path(reuse).resolve()
            prior_lock = read_json(previous / "oracle.lock.json")
            previous_inputs = read_json(previous / "inputs.json")
            plan = read_json(previous / "test-plan.json")
            acceptance = read_json(previous / "acceptance-plan.json")
            if prior_lock["source_manifest"] != original_manifest or previous_inputs != context:
                raise RefinerError("Cached plans refer to different Skill inputs or standards")
            if prior_lock["sha256"] != digest({"plan": plan, "acceptance": acceptance, "inputs": context}):
                raise RefinerError("Cached plan lock mismatch")
            if any(c.get('helpers') for c in plan['cases'] + acceptance['cases']) and prior_lock.get('python_probe_sha256') != helper_hash:
                raise RefinerError('Cached Python observer hash mismatch')
            state["plan_provenance"] = str(previous)
            state['plan_provenance_checker_sha256'] = prior_lock.get('checker_sha256')
            for suite, frozen, limit, suite_context in (
                    ("development", plan, args.max_cases, context),
                    ("acceptance", acceptance, args.acceptance_cases,
                     {**context, "contract": plan["requirements"]})):
                phase_progress("plan_generation", "reused", suite=suite,
                               source_run=str(previous), case_ids=[case["id"] for case in frozen["cases"]])
                phase_progress("plan_self_check", "running", suite=suite, reused=True)
                try:
                    business_analysis = context.get("business_analysis") or {}
                    source_scenarios = business_analysis.get("scenarios") or None
                    required_scenario_ids = context.get("required_scenario_ids")
                    if required_scenario_ids is None and source_scenarios:
                        required_scenario_ids = [row["id"] for row in source_scenarios if row["applicable"]]
                    controls = validate_plan(
                        frozen, work, limit,
                        required_case_kinds=context.get("required_case_kinds", []),
                        required_scenario_ids=required_scenario_ids or [],
                        expected_scenarios=source_scenarios,
                        expected_requirements=context.get("contract"))
                except PlanValidationError as exc:
                    failed_evidence = {**exc.evidence, "suite": suite, "attempt": 1,
                                       "plan_sha256": digest(frozen), "plan_snapshot": frozen}
                    write_json(work / "plan-controls" / "attempts" / f"{suite}-01.json", failed_evidence)
                    write_json(work / "plan-controls" / f"{suite}.json", failed_evidence)
                    phase_progress("plan_self_check", "failed", suite=suite, reused=True, reason=str(exc))
                    raise PlanGateError(f"Reused {suite} plan failed its complete control suite: {exc}") from exc
                control_evidence = {"schema_version": "skill-refiner.plan-controls/v1", "suite": suite,
                                    "attempt": 1, "status": "passed", "plan_sha256": digest(frozen),
                                    "plan_snapshot": frozen,
                                    "case_control_results": controls["case_control_results"],
                                    "control_failures": [], "coverage_failures": []}
                write_json(work / "plan-controls" / "attempts" / f"{suite}-01.json", control_evidence)
                write_json(work / "plan-controls" / f"{suite}.json", control_evidence)
                phase_progress("plan_self_check", "passed", suite=suite, reused=True,
                               controls=len(controls["case_control_results"]))
                _review_frozen_plan(agent, frozen, suite_context, controls,
                                    phase_callback=phase_progress, suite=suite)
        else:
            plan, acceptance = make_plan_suites(
                agent, context, work, args.max_cases, args.acceptance_cases,
                max_attempts=getattr(args, 'planning_attempts', 2),
                phase_callback=phase_progress,
                agent_parallelism=getattr(args, "agent_parallelism", 2),
                wave_callback=planning_wave_progress)
            budget()
        if not frozen_plan:
            write_json(work / "test-plan.json", plan)
            write_json(work / "acceptance-plan.json", acceptance)
        development_inputs = {digest(c["files"] or {"prompt": c["prompt"]}) for c in plan["cases"]}
        if all(digest(c["files"] or {"prompt": c["prompt"]}) in development_inputs for c in acceptance["cases"]):
            raise PlanGateError("Independent acceptance plan contains no fresh task inputs")
        if plan["coverage_gaps"] or acceptance["coverage_gaps"]:
            gaps = plan["coverage_gaps"] + acceptance["coverage_gaps"]
            phase_progress("plan_gate", "not_passed", reason="coverage_gap", gaps=gaps)
            state.update(status="plan_not_passed", gaps=gaps,
                         error="Plan gate did not pass because required coverage is missing.")
            write_json(work / "state.json", state)
            print(json.dumps({"stage": "plan_gate", "status": "not_passed", "gaps": gaps,
                              "skill_evaluation": "not_started"}, ensure_ascii=False), flush=True)
            return state
        locked = digest({"plan": plan, "acceptance": acceptance, "inputs": context})
        write_json(work / "oracle.lock.json", {"sha256": locked, "source_manifest": original_manifest,
                   "requirements": plan["requirements"], "checker_sha256": checker_hash,
                   "python_probe_sha256": helper_hash, "engine": backend.engine})
        stages.complete("planning")
        phase_progress("plan_gate", "passed", development_cases=len(plan["cases"]),
                       acceptance_cases=len(acceptance["cases"]), oracle_lock_sha256=locked)
        state["plan_ready"] = True
        write_work_plan(work, source=source, plan=plan, acceptance=acceptance, limits=limits,
                        agent_parallelism=getattr(args, "agent_parallelism", 2))
        write_test_scenarios(work, plan=plan, acceptance=acceptance)
        event("plan_ready", work_plan=str(work / "work-plan.md"),
              requirements=len(plan["requirements"]), development_cases=len(plan["cases"]),
              acceptance_cases=len(acceptance["cases"]), max_rounds=args.max_rounds,
              max_seconds=args.max_seconds, max_evaluations=args.max_evaluations)
        if getattr(args, "plan_only", False):
            state.update(status="planned", planned_only=True,
                         planned_development_cases=len(plan["cases"]),
                         planned_acceptance_cases=len(acceptance["cases"]),
                         skill_evaluation="not_started",
                         elapsed_seconds=round(elapsed_before + time.monotonic()-started, 2))
            write_json(work / "state.json", state)
            return state
        expected_name = next((f["content"] for f in context["files"] if f["path"] == "SKILL.md"), "")
        import yaml
        import re
        m = re.match(r"---\s*\n(.*?)\n---", expected_name, re.S)
        try:
            fm = yaml.safe_load(m[1]) if m else {}
        except yaml.YAMLError:
            fm = {}
        expected_name = fm.get("name") if isinstance(fm, dict) else None
        event("baseline")
        require_plan_gate(state)
        set_phase_result(state, "skill_evaluation", "running")
        set_phase_result(state, "skill_evaluation", "running", suite="baseline")
        write_json(work / "state.json", state)
        baseline_dir = work / "baseline"
        baseline_dir.mkdir(exist_ok=True)
        result = evaluate(original, plan["cases"], baseline_dir)
        set_phase_result(state, "skill_evaluation", "passed" if result.get("passed") else "failed",
                         suite="baseline", passed=result.get("passed"), cases=len(result.get("cases", [])))
        findings = standards_review(agent, original, context, baseline_dir, expected_name)
        original_result = result
        best, best_result, best_findings, best_unresolved = original, result, findings, []
        baseline_passed = {r["id"] for r in result["cases"] if r["passed"]}
        no_progress = 0
        for number in range(1, args.max_rounds + 1):
            budget()
            if qualifies(best_result, best_findings, best_unresolved):
                break
            if any(r["status"] == "ERROR" for r in best_result["cases"]):
                event("execution_error", message="Resolve engine/environment errors before modifying the Skill")
                set_phase_result(state, "skill_evaluation", "error", reason="execution_error")
                write_json(work / "state.json", state)
                return state
            event("repairing", round=number)
            round_dir = work / "iterations" / f"round-{number}"
            round_dir.mkdir(parents=True, exist_ok=True)
            failing = [r for r in best_result["cases"] if not r["passed"]]
            errors = [f for f in best_findings if f["severity"] == "error"]
            unresolved_evidence = [{"id": f"U-{i+1:03}", "message": issue,
                                    "origin": "previous repair report; not a standards finding"}
                                   for i, issue in enumerate(best_unresolved)]
            evidence_ids = ({r["id"] for r in failing} | {f["id"] for f in errors}
                            | {u["id"] for u in unresolved_evidence})
            repair_schema = copy.deepcopy(REPAIR)
            repair_schema["properties"]["changes"]["items"]["properties"]["evidence_ids"]["items"]["enum"] = sorted(evidence_ids)
            response = agent.ask("repair", "Diagnose the failed cases and material standards violations from "
                                 "the supplied frozen requirement contract and cited findings. Large source documents "
                                 "are retained as hash-addressed artifacts; do not assume omitted source text. "
                                 "Return narrow full-file replacements with concrete reasons and evidence_ids. "
                                 "Preserve the Skill's identity, scope and valid existing behavior. Fix supporting "
                                 "scripts if they cause failure, not just prose. Never edit evals, tests, requirements "
                                 "or grading criteria, and never hardcode known test inputs or expected answers. "
                                 "Do not add capabilities not required by the supplied contract. Return changes=[] "
                                 "and unresolved issues if repair cannot be justified. unresolved lists only remaining "
                                 "required behaviors, not intentional scope exclusions. The controller runs your patch "
                                 "afterwards: not having executed it yourself is not an unresolved behavior. "
                                 "Use only allowed_evidence_ids, not requirement IDs. Held-out acceptance cases are omitted "
                                 "until the candidate first qualifies on the development suite.",
                                 {"files": inventory(best, max_bytes=120_000,
                                                     priority_paths=linked_source_paths(best, errors)),
                                  "requirements": plan["requirements"],
                                  "requirement_sources": source_references(context["requirements"], "requirement"),
                                  "standard_sources": source_references(context["standards"], "standard"),
                                  "allowed_evidence_ids": sorted(evidence_ids),
                                  "failures": failing, "findings": errors,
                                  "previous_unresolved": unresolved_evidence,
                                  "failure_evidence": repair_evidence(failing, max_bytes=60_000),
                                  "failing_cases": [c for c in plan["cases"] if c["id"] in
                                                    {r.get("case_id", r["id"]) for r in failing}]}, repair_schema)
            write_json(round_dir / "changes.json", response)
            if not response["changes"]:
                event("no_progress", unresolved=response["unresolved"])
                return state
            candidate = work / "candidates" / str(number) / source.name
            candidate = resumable_candidate(best, response, evidence_ids, candidate)
            if manifest(candidate) == manifest(best):
                event("no_progress", message="Repair did not change any source file")
                return state
            budget()
            budget()
            event("regression", round=number)
            current = evaluate(candidate, plan["cases"], round_dir / "regression")
            current_findings = standards_review(agent, candidate, context, round_dir, expected_name)
            accepted, regressed = compare_candidate(baseline_passed, best_result, best_findings, current,
                                                   current_findings, best_unresolved, response["unresolved"])
            record_round({"number": number, "accepted": accepted, "regressed_cases": regressed,
                                    "score": score(current, current_findings), "candidate": str(candidate),
                                    "unresolved": response["unresolved"],
                                    "retained_for_next_round": accepted,
                                    "qualified_for_acceptance": qualifies(current, current_findings, response["unresolved"])})
            if accepted:
                best, best_result, best_findings = candidate, current, current_findings
                best_unresolved = response["unresolved"]
                no_progress = 0
            else:
                no_progress += 1
            write_json(work / "state.json", state)
            if no_progress >= 2:
                break
        budget()
        if not qualifies(best_result, best_findings, best_unresolved):
            event("not_converged", best_candidate=str(best), unresolved=best_unresolved)
            return state
        acceptance_dir = work / "acceptance"
        acceptance_dir.mkdir(parents=True, exist_ok=True)
        def acceptance_target(target, label):
            target_manifest = manifest(target)
            record = {"candidate": target.resolve().relative_to(work.resolve()).as_posix(),
                      "candidate_manifest": target_manifest,
                      "candidate_sha256": digest(target_manifest),
                      "suite_sha256": digest(acceptance["cases"])}
            attempt_dir = acceptance_dir / "attempts" / label
            attempt_dir.mkdir(parents=True, exist_ok=True)
            write_json(attempt_dir / "target.json", record)
            return record, attempt_dir

        accepted_manifest, initial_acceptance_dir = acceptance_target(best, "initial")
        write_json(acceptance_dir / "target.json", accepted_manifest)
        event("acceptance", attempt="initial")
        final_checks = evaluate(best, acceptance["cases"], initial_acceptance_dir / "evaluation")
        write_json(acceptance_dir / "summary.json", final_checks)
        budget()
        if any(row["status"] == "ERROR" for row in final_checks["cases"]):
            event("execution_error", message="Resolve acceptance engine/environment errors before modifying the Skill")
            return state

        # An independent acceptance failure is actionable evidence. Route only its failed cases
        # back through the same evidence-bound repair and full development regression gates.
        last_development_round = max((row.get("number", 0) for row in state["rounds"]
                                     if row.get("trigger") != "acceptance"), default=0)
        for number in range(last_development_round + 1, args.max_rounds + 1):
            if final_checks["passed"] and not best_unresolved:
                break
            failing_acceptance = [row for row in final_checks["cases"] if not row["passed"]]
            if not failing_acceptance and not best_unresolved:
                break
            event("repairing", round=number, trigger="acceptance")
            round_dir = work / "iterations" / f"round-{number}"
            round_dir.mkdir(parents=True, exist_ok=True)
            scoped_failures = []
            for row in failing_acceptance:
                case_id = row.get("case_id", row["id"])
                scoped_failures.append({**row, "id": f"acceptance::{case_id}",
                                        "case_id": case_id, "suite": "acceptance"})
            unresolved_evidence = [{"id": f"U-{i+1:03}", "message": issue,
                                    "origin": "previous repair report; not a standards finding"}
                                   for i, issue in enumerate(best_unresolved)]
            evidence_ids = ({row["id"] for row in scoped_failures}
                            | {finding["id"] for finding in best_findings if finding["severity"] == "error"}
                            | {item["id"] for item in unresolved_evidence})
            repair_schema = copy.deepcopy(REPAIR)
            repair_schema["properties"]["changes"]["items"]["properties"]["evidence_ids"]["items"]["enum"] = sorted(evidence_ids)
            response = agent.ask(
                "repair",
                "Diagnose these failures from the independently planned acceptance suite against the frozen "
                "requirements and cited source material. The exact failed acceptance cases are included because "
                "a previously qualified candidate failed final acceptance. Return narrow full-file replacements "
                "only for demonstrated target defects. Preserve identity, scope and valid behavior; fix supporting "
                "scripts when they cause a failure. Never edit tests, plans, requirements or grading criteria, and "
                "never hardcode fixture inputs or expected answers. If an observed response already satisfies the "
                "contract but a narrow oracle rejects equivalent wording, do not change target output to appease it; "
                "return changes=[] and describe the oracle problem as unresolved. Use only allowed_evidence_ids, "
                "not requirement IDs. The controller will run the complete development regression suite and rerun "
                "acceptance after applying a patch.",
                {"files": inventory(best, max_bytes=120_000,
                                    priority_paths=linked_source_paths(best, [])),
                 "requirements": plan["requirements"],
                 "requirement_sources": source_references(context["requirements"], "requirement"),
                 "standard_sources": source_references(context["standards"], "standard"),
                 "allowed_evidence_ids": sorted(evidence_ids),
                 "failures": scoped_failures,
                 "acceptance_failures": scoped_failures,
                 "findings": [finding for finding in best_findings if finding["severity"] == "error"],
                 "previous_unresolved": unresolved_evidence,
                 "failure_evidence": repair_evidence(scoped_failures, max_bytes=60_000),
                 "failing_cases": [case for case in acceptance["cases"]
                                   if case["id"] in {row["case_id"] for row in scoped_failures}]},
                repair_schema)
            write_json(round_dir / "changes.json", response)
            if not response["changes"]:
                event("failed_acceptance", best_candidate=str(best),
                      failed_cases=[row.get("case_id", row["id"]) for row in final_checks["cases"] if not row["passed"]],
                      unresolved=response["unresolved"], message="Acceptance repair produced no changes")
                return state
            candidate = work / "candidates" / str(number) / source.name
            candidate = resumable_candidate(best, response, evidence_ids, candidate)
            if manifest(candidate) == manifest(best):
                event("failed_acceptance", best_candidate=str(best),
                      failed_cases=[row.get("case_id", row["id"]) for row in final_checks["cases"] if not row["passed"]],
                      unresolved=response["unresolved"], message="Acceptance repair did not change any source file")
                return state
            budget()
            event("regression", round=number)
            current = evaluate(candidate, plan["cases"], round_dir / "regression")
            current_findings = standards_review(agent, candidate, context, round_dir, expected_name)
            if any(row["status"] == "ERROR" for row in current["cases"]):
                event("execution_error", message="Resolve regression engine/environment errors before continuing")
                return state
            candidate_acceptance_record, candidate_acceptance_dir = acceptance_target(candidate, f"round-{number}")
            event("acceptance", attempt=f"round-{number}")
            candidate_acceptance = evaluate(candidate, acceptance["cases"],
                                            candidate_acceptance_dir / "evaluation")
            budget()
            if any(row["status"] == "ERROR" for row in candidate_acceptance["cases"]):
                event("execution_error", message="Resolve acceptance engine/environment errors before continuing")
                return state
            before_result = combine_results(best_result, final_checks)
            after_result = combine_results(current, candidate_acceptance)
            accepted, regressed = compare_candidate(baseline_passed, before_result, best_findings,
                                                    after_result, current_findings,
                                                    best_unresolved, response["unresolved"])
            record_round({"number": number, "accepted": accepted,
                                    "regressed_cases": regressed,
                                    "score": score(after_result, current_findings),
                                    "candidate": str(candidate),
                                    "unresolved": response["unresolved"],
                                    "retained_for_next_round": accepted,
                                    "qualified_for_acceptance": (
                                        qualifies(current, current_findings, response["unresolved"])
                                        and candidate_acceptance["passed"]),
                                     "trigger": "acceptance"})
            if accepted:
                best, best_result, best_findings = candidate, current, current_findings
                best_unresolved = response["unresolved"]
                final_checks = candidate_acceptance
                write_json(acceptance_dir / "target.json", candidate_acceptance_record)
                write_json(acceptance_dir / "summary.json", final_checks)
                no_progress = 0
            else:
                no_progress += 1
            write_json(work / "state.json", state)
            if no_progress >= 2:
                break
        if not final_checks["passed"] or best_unresolved:
            event("failed_acceptance", best_candidate=str(best),
                  failed_cases=[row.get("case_id", row["id"]) for row in final_checks["cases"] if not row["passed"]],
                  unresolved=best_unresolved)
            return state
        stages.complete("acceptance")
        final = work / "final" / source.name
        final = resumable_snapshot(best, final, keep_destination=True)
        patch = []
        before, after = manifest(original), manifest(final)
        for path in sorted(before.keys() | after.keys()):
            if before.get(path) == after.get(path):
                continue
            old = (original/path).read_text(encoding="utf-8") if path in before else ""
            new = (final/path).read_text(encoding="utf-8") if path in after else ""
            patch.extend(difflib.unified_diff(old.splitlines(True), new.splitlines(True), "original/"+path, "candidate/"+path))
        (work / "final" / "changes.patch").write_text("".join(patch), encoding="utf-8")
        write_json(work / "final" / "evidence.json", {"original_manifest": original_manifest,
                   "candidate_manifest": manifest(final), "oracle_hash": locked,
                   "baseline": original_result, "regression": best_result,
                   "acceptance": final_checks, "standards": best_findings})
        event("passed" if before != after else "unchanged", final_skill=str(final))
        for stage_id in ("planning", "baseline"):
            stages.complete(stage_id)
        if before != after:
            stages.complete("repair")
            stages.complete("regression")
        stages.complete("acceptance")
        stages.complete("delivery")
        return state
    except PlanGateError as exc:
        phase_progress("plan_gate", "not_passed", reason=str(exc))
        if state.get("phase_results", {}).get("skill_evaluation", {}).get("status") == "not_started":
            state["status"] = "plan_not_passed"
            state["skill_evaluation"] = "not_started"
        state["error"] = str(exc)
        write_json(work / "state.json", state)
        print(json.dumps({"stage": "plan_gate", "status": "not_passed",
                          "reason": str(exc), "skill_evaluation": "not_started"},
                         ensure_ascii=False), flush=True)
        return state
    except Cancelled as exc:
        event("cancelled", error=str(exc))
        close_incomplete_planning_phases(state, str(exc))
        close_incomplete_plan_gate(state, str(exc), cause="cancelled")
        if state.get("phase_results", {}).get("skill_evaluation", {}).get("status") == "running":
            set_phase_result(state, "skill_evaluation", "error", cancellation=str(exc))
        write_json(work / "state.json", state)
        return state
    except Exception as exc:
        if state.get("phase_results", {}).get("plan_gate", {}).get("status") != "passed" and \
                state.get("phase_results", {}).get("skill_evaluation", {}).get("status") == "not_started":
            phases = state.get("phase_results", {})
            for name in ("plan_generation", "plan_self_check", "independent_review"):
                phase_entry = phases.get(name, {})
                if phase_entry.get("status") == "running":
                    phase_progress(name, "error", reason=str(exc))
                    break
            close_incomplete_plan_gate(state, str(exc))
            state.update(status="planning_error", error=str(exc), skill_evaluation="not_started")
            write_json(work / "state.json", state)
            print(json.dumps({"stage": "planning", "status": "error", "reason": str(exc),
                              "skill_evaluation": "not_started"}, ensure_ascii=False), flush=True)
        else:
            event("error", error=str(exc))
            if state.get("phase_results", {}).get("skill_evaluation", {}).get("status") == "running":
                set_phase_result(state, "skill_evaluation", "error", reason=str(exc))
        return state
    finally:
        state["agent_calls"] = agent.calls
        state["elapsed_seconds"] = round(elapsed_before + time.monotonic()-started, 2)
        state["cached_agent_calls"] = agent.cached_calls
        if "backend" in locals():
            state["target_evaluations"] = backend.evaluations
        write_json(work / "state.json", state)
        write_report(work, state)
        if state.get("status") in {"passed", "unchanged"}:
            set_phase_result(state, "skill_evaluation", "passed")
            set_phase_result(state, "final_verification", "running")
            write_json(work / "state.json", state)
            stages.activate("verification")
            try:
                from .gate import verify_run
                verification = verify_run(work)
                write_json(work / "verification.json", verification)
                state["verification_status"] = verification["status"]
                set_phase_result(state, "final_verification", "passed",
                                 verification_status=verification["status"])
            except Exception as exc:
                state.update(status="verification_failed", verification_error=str(exc))
                set_phase_result(state, "final_verification", "failed", reason=str(exc))
            write_json(work / "state.json", state)
            write_report(work, state)
        elif state.get("phase_results", {}).get("plan_gate", {}).get("status") == "passed" and \
                state.get("phase_results", {}).get("skill_evaluation", {}).get("status") == "running":
            outcome = "error" if state.get("status") in {"error", "cancelled", "execution_error"} else "failed"
            set_phase_result(state, "skill_evaluation", outcome, reason=state.get("error", state.get("status")))
            write_json(work / "state.json", state)
            write_report(work, state)
        stages.finish(state.get("status", "error"))


def write_report(work, state):
    lines = ["# Skill Refiner result", "", f"Status: **{state['status']}**", "",
             "[Design and test plan](work-plan.md)", "",
             f"Original: `{state['source']}`", f"Agent calls: {state.get('agent_calls', 0)}",
             f"Case-executor evaluations: {state.get('target_evaluations', 0)}",
             f"Elapsed: {state.get('elapsed_seconds', 0)} seconds", ""]
    if state.get("verification_status"):
        lines.insert(3, f"Verification: **{state['verification_status']}**")
    elif state.get("verification_error"):
        lines.insert(3, "Verification: **failed** — " + state["verification_error"])
    if state.get("final_skill"):
        lines += [f"Candidate: `{state['final_skill']}`", "Original files were not overwritten.", ""]
    phase_results = state.get("phase_results", {})
    if phase_results:
        lines += ["## Phase results", "", "| Phase | Status | Suite results |", "|---|---|---|"]
        phase_labels = [
            ("plan_generation", "Plan generation"),
            ("plan_self_check", "Plan self-check"),
            ("independent_review", "Independent review"),
            ("plan_gate", "Plan gate"),
            ("skill_evaluation", "Target Skill evaluation"),
            ("final_verification", "Final Refiner verify"),
        ]
        for key, label in phase_labels:
            entry = phase_results.get(key, {})
            status = entry.get("status", "not_started")
            if key == "plan_gate" and status == "not_passed":
                status = "计划未通过 (not_passed)"
            suites = entry.get("suites", {})
            suite_text = ", ".join(f"{name}={value}" for name, value in suites.items()) or "—"
            lines.append(f"| {label} | {status} | {suite_text} |")
        lines.append("")
        if phase_results.get("plan_gate", {}).get("status") != "passed":
            lines.append("Target Skill evaluation: **not started**; the plan gate did not pass.")
            lines.append("")
    phases = [("Baseline", work/"baseline"/"summary.json")]
    accepted_rounds = [r for r in state["rounds"] if r["accepted"]]
    if accepted_rounds:
        phases.append(("Full regression", work/"iterations"/f"round-{accepted_rounds[-1]['number']}"/"regression"/"summary.json"))
    phases.append(("Acceptance", work/"acceptance"/"summary.json"))
    for phase, path in phases:
        if path.exists():
            result = read_json(path)
            if "engines" in result:
                comparison = path.parent / "comparison.md"
                rendered = comparison.read_text(encoding="utf-8") if comparison.is_file() else comparison_markdown(result)
                lines += [f"## {phase}", "", rendered, ""]
                continue
            lines += [f"## {phase}", "", "| Case | Status |", "|---|---|"]
            lines += [f"| {r['id']} | {r['status']} |" for r in result["cases"]]
            lines += [""]
    lines += ["## Iterations", ""]
    for r in state["rounds"]:
        lines.append(f"- Round {r['number']}: accepted={r['accepted']}, score={r['score']}, regressions={r['regressed_cases']}")
    if accepted_rounds:
        lines += ["", "## Changes and evidence", ""]
        for r in accepted_rounds:
            changes = read_json(work/"iterations"/f"round-{r['number']}"/"changes.json")
            lines.append("Diagnosis: " + changes["diagnosis"])
            for change in changes["changes"]:
                lines.append(f"- `{change['path']}`: {change['reason']} (evidence: {', '.join(change['evidence_ids'])})")
    if (work/"test-plan.json").exists():
        plan = read_json(work/"test-plan.json")
        lines += ["", "## Planned requirement coverage", "", "| Requirement | Cases |", "|---|---|"]
        for requirement in plan["requirements"]:
            ids = [c["id"] for c in plan["cases"] if requirement["id"] in c["requirement_ids"]]
            lines.append(f"| {requirement['id']}: {requirement['behavior']} | {', '.join(ids)} |")
    if state.get("gaps"):
        lines += ["", "## Coverage gaps", ""] + ["- " + gap for gap in state["gaps"]]
    if state.get("error"):
        lines += ["", "Error: " + state["error"]]
    lines += ["", "## Limits", ""] + ["- " + x for x in state["limitations"]]
    (work / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

