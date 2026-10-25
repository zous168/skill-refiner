from __future__ import annotations

from datetime import datetime, timezone
import difflib
import json
import hashlib
import copy
import re
from pathlib import Path
import time

from .agent import Agent
from .checks import validate_plan
from .common import (Cancelled, RefinerError, digest, inventory, manifest, read_json, relative_path,
                     snapshot, write_json)
from .schemas import PLAN, PLAN_REVIEW, REPAIR, REVIEW
from .evidence import repair_evidence
from .skillup import SkillUp
from .standards import AUTHORING, lint
from .profiles import load_config
from .matrix import Matrix, comparison_markdown
from .stages import StageManifest, REFINE_EVENTS


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
files, not SKILL.md text. Cover normal behavior and applicable boundaries/errors/negative cases.
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


def validate_plan_review(review, plan):
    import jsonschema
    jsonschema.validate(review, PLAN_REVIEW)
    contracts = review["case_contracts"]
    expected = {c["id"] for c in plan["cases"]}
    if len(contracts) != len(expected) or {c["case_id"] for c in contracts} != expected:
        raise RefinerError("Plan review must assess the execution contract of every case exactly once")
    if review["approved"] and any(not all(c[k] for k in ("installed_source_unchanged", "fixture_contract_consistent",
                                                         "response_contract_consistent", "trace_checks_observable"))
                                  for c in contracts):
        raise RefinerError("Approved plan review contains a conflicting execution contract")


def make_plan(agent, context, work, max_cases, role="plan", max_attempts=2):
    feedback = []
    previous_plan = None
    for attempt in range(max_attempts):
        plan = agent.ask(role, PLANNING + f" Generate 2 to {max_cases} cases, never more. " +
                         ("Copy the supplied contract requirement list exactly, preserving IDs and all properties. "
                          if "contract" in context else "") +
                         ("Include at least one executable case of EACH required_case_kinds category: "
                          + ", ".join(context["required_case_kinds"]) + ". "
                          if context.get("required_case_kinds") else "") +
                         ("Use different inputs from common examples; these are independent acceptance cases. "
                          "Copy the supplied contract requirement list exactly, preserving IDs and all properties."
                          if role == "acceptance-plan" else ""),
                         {**context, "validation_feedback": feedback, **(
                             {'previous_plan': previous_plan} if previous_plan else {})}, PLAN)
        previous_plan = plan
        try:
            validate_plan(plan, work, max_cases)
            if "contract" in context and plan["requirements"] != context["contract"]:
                raise RefinerError("Planner changed the frozen capability contract")
            missing_kinds = set(context.get("required_case_kinds", [])) - {c["kind"] for c in plan["cases"]}
            if missing_kinds:
                raise RefinerError("Missing required scenario categories: " + ", ".join(sorted(missing_kinds)))
        except Exception as exc:
            # One bounded correction for malformed coverage/oracles; never silently accept a bad suite.
            if attempt + 1 >= max_attempts:
                raise RefinerError(f"Invalid autonomous test plan: {exc}") from exc
            feedback.append(str(exc))
            continue
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
                           "Approve only a useful executable suite. " + HELPER_EXECUTION_AUDIT + " "
                           + CONVERSATIONAL_JSON_AUDIT + " " + INPUT_BOUNDARY_AUDIT,
                           {"context": context, "plan": plan}, PLAN_REVIEW)
        try:
            validate_plan_review(review, plan)
        except RefinerError as exc:
            if attempt + 1 >= max_attempts:
                raise
            feedback.append(str(exc))
            continue
        if review["approved"]:
            return plan
        if attempt + 1 >= max_attempts:
            raise RefinerError("Independent test review rejected the plan: " + "; ".join(review["issues"]))
        feedback.extend(review["issues"])
    raise RefinerError("Unable to construct a valid plan")


def apply_changes(candidate, response, evidence_ids):
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
        collect(yaml.safe_load(config.read_text()))
    changes = response["changes"]
    if len(changes) > 20 or sum(len(c["content"].encode()) for c in changes) > 500_000:
        raise RefinerError("Repair exceeds bounded patch size")
    seen = set()
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
    for change in changes:
        path = relative_path(candidate, change["path"])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(change["content"], encoding="utf-8")


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
    for reference in args.spec + args.standard:
        if not Path(reference).is_file():
            raise RefinerError(f"Reference file does not exist: {reference}")
    work = Path(args.output).resolve() if args.output else source.parent / (
        source.name + "-refiner-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ"))
    if work.exists() or work.is_relative_to(source):
        raise RefinerError("Output must be a new directory outside the target Skill")
    work.mkdir(parents=True)
    started = time.monotonic()
    original_manifest = manifest(source)
    original = work / "original" / source.name
    snapshot(source, original)
    state = {"status": "planning", "source": str(source), "workspace": str(work),
             "rounds": [], "limitations": ["Uses local environment.type=none; no container isolation",
             "Agent role separation uses fresh contexts; it is not an OS-level read barrier",
             "Declarative response/file checks only; no automatic domain-specific executable graders",
             "Small acceptance sets provide observed evidence, not statistical reliability proof"]}
    write_json(work / "state.json", state)
    agent = Agent(work, args.agent_command, args.timeout, config=config)
    agent.max_calls = args.max_agent_calls
    agent.deadline = started + args.max_seconds
    engine_config = read_json(args.engine_config) if args.engine_config else None
    context = {"files": inventory(original, max_bytes=300_000), "requirements": [], "standards": [AUTHORING]}
    context.update(getattr(args, "creation_context", {}))
    for path in args.spec:
        context["requirements"].append({"source": str(Path(path).resolve()), "content": Path(path).read_text()})
    for path in args.standard:
        context["standards"].append({"source": str(Path(path).resolve()), "content": Path(path).read_text()})
    write_json(work / "inputs.json", context)
    stages = StageManifest(work, "refine")
    stages.complete("inputs")
    locked = None
    checker_file = Path(__file__).with_name("checks.py")
    checker_hash = hashlib.sha256(checker_file.read_bytes()).hexdigest()
    helper_file = Path(__file__).with_name('python_probe.py')
    helper_hash = hashlib.sha256(helper_file.read_bytes()).hexdigest()

    def evaluate(target, cases, run_dir):
        if config:
            return backend.evaluate(target, cases, run_dir)
        result = backend.evaluate(target, cases, run_dir)
        existing = backend.evaluate_existing(target, Path(run_dir) / "existing")
        if existing:
            result["cases"].extend(existing["cases"])
            result["passed"] = result["passed"] and existing["passed"]
            result["existing_report"] = existing["upstream_report"]
            write_json(Path(run_dir) / "summary.json", result)
        return result

    def event(status, **data):
        if status in REFINE_EVENTS:
            stages.activate(REFINE_EVENTS[status])
        state.update(status=status, agent_calls=agent.calls, elapsed_seconds=round(time.monotonic()-started, 2), **data)
        write_json(work / "state.json", state)
        print(json.dumps({"status": status, **data}, ensure_ascii=False), flush=True)

    def budget():
        if time.monotonic() - started > args.max_seconds or agent.calls > args.max_agent_calls:
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

    try:
        backend = (Matrix(config["executors"], work, args.skill_up, args.timeout, args.parallelism) if config else
                   SkillUp(args.skill_up, args.engine, engine_config, args.timeout, args.parallelism))
        if config:
            state["backends"] = config
            write_json(work / "backend-config.json", config)
        backend.deadline = started + args.max_seconds
        backend.max_evaluations = args.max_evaluations
        event("planning")
        reuse = getattr(args, "reuse_plan", None)
        if reuse:
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
            validate_plan(plan, work, args.max_cases)
            validate_plan(acceptance, work, args.acceptance_cases)
            state["plan_provenance"] = str(previous)
            state['plan_provenance_checker_sha256'] = prior_lock.get('checker_sha256')
        else:
            plan = make_plan(agent, context, work, args.max_cases, max_attempts=getattr(args,'planning_attempts',2))
            budget()
            acceptance = make_plan(agent, {**context, "contract": plan["requirements"]}, work,
                                   args.acceptance_cases, "acceptance-plan", max_attempts=getattr(args,'planning_attempts',2))
        write_json(work / "test-plan.json", plan)
        write_json(work / "acceptance-plan.json", acceptance)
        development_inputs = {digest(c["files"] or {"prompt": c["prompt"]}) for c in plan["cases"]}
        if all(digest(c["files"] or {"prompt": c["prompt"]}) in development_inputs for c in acceptance["cases"]):
            raise RefinerError("Acceptance set contains no new task inputs")
        if plan["coverage_gaps"] or acceptance["coverage_gaps"]:
            event("coverage_gap", gaps=plan["coverage_gaps"] + acceptance["coverage_gaps"])
            return state
        locked = digest({"plan": plan, "acceptance": acceptance, "inputs": context})
        write_json(work / "oracle.lock.json", {"sha256": locked, "source_manifest": original_manifest,
                   "requirements": plan["requirements"], "checker_sha256": checker_hash,
                   "python_probe_sha256": helper_hash, "engine": backend.engine})
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
        baseline_dir = work / "baseline"
        baseline_dir.mkdir()
        result = evaluate(original, plan["cases"], baseline_dir)
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
                return state
            event("repairing", round=number)
            round_dir = work / "iterations" / f"round-{number}"
            round_dir.mkdir(parents=True)
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
            snapshot(best, candidate)
            apply_changes(candidate, response, evidence_ids)
            if manifest(candidate) == manifest(best):
                event("no_progress", message="Repair did not change any source file")
                return state
            budget()
            selected = {r.get("case_id", r["id"]) for r in failing} & {c["id"] for c in plan["cases"]}
            if selected and selected != {c["id"] for c in plan["cases"]}:
                event("focused", round=number)
                if config:
                    backend.evaluate(candidate, plan["cases"], round_dir / "focused", selected=selected, existing=False)
                else:
                    backend.evaluate(candidate, plan["cases"], round_dir / "focused", selected=selected)
            budget()
            event("regression", round=number)
            current = evaluate(candidate, plan["cases"], round_dir / "regression")
            current_findings = standards_review(agent, candidate, context, round_dir, expected_name)
            accepted, regressed = compare_candidate(baseline_passed, best_result, best_findings, current,
                                                   current_findings, best_unresolved, response["unresolved"])
            state["rounds"].append({"number": number, "accepted": accepted, "regressed_cases": regressed,
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
        for number in range(len(state["rounds"]) + 1, args.max_rounds + 1):
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
            snapshot(best, candidate)
            apply_changes(candidate, response, evidence_ids)
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
            state["rounds"].append({"number": number, "accepted": accepted,
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
        snapshot(best, final)
        patch = []
        before, after = manifest(original), manifest(final)
        for path in sorted(before.keys() | after.keys()):
            if before.get(path) == after.get(path):
                continue
            old = (original/path).read_text() if path in before else ""
            new = (final/path).read_text() if path in after else ""
            patch.extend(difflib.unified_diff(old.splitlines(True), new.splitlines(True), "original/"+path, "candidate/"+path))
        (work / "final" / "changes.patch").write_text("".join(patch))
        write_json(work / "final" / "evidence.json", {"original_manifest": original_manifest,
                   "candidate_manifest": manifest(final), "oracle_hash": locked,
                   "baseline": original_result, "regression": best_result,
                   "acceptance": final_checks, "standards": best_findings})
        event("passed" if before != after else "unchanged", final_skill=str(final))
        return state
    except Cancelled as exc:
        event("cancelled", error=str(exc))
        return state
    except Exception as exc:
        event("error", error=str(exc))
        return state
    finally:
        state["agent_calls"] = agent.calls
        state["elapsed_seconds"] = round(time.monotonic()-started, 2)
        if "backend" in locals():
            state["target_evaluations"] = backend.evaluations
        write_json(work / "state.json", state)
        write_report(work, state)
        if state.get("status") in {"passed", "unchanged"}:
            stages.activate("verification")
            try:
                from .gate import verify_run
                verification = verify_run(work)
                write_json(work / "verification.json", verification)
                state["verification_status"] = verification["status"]
            except Exception as exc:
                state.update(status="verification_failed", verification_error=str(exc))
            write_json(work / "state.json", state)
            write_report(work, state)
        stages.finish(state.get("status", "error"))


def write_report(work, state):
    lines = ["# Skill Refiner result", "", f"Status: **{state['status']}**", "",
             f"Original: `{state['source']}`", f"Agent calls: {state.get('agent_calls', 0)}",
             f"Elapsed: {state.get('elapsed_seconds', 0)} seconds", ""]
    if state.get("verification_status"):
        lines.insert(3, f"Verification: **{state['verification_status']}**")
    elif state.get("verification_error"):
        lines.insert(3, "Verification: **failed** — " + state["verification_error"])
    if state.get("final_skill"):
        lines += [f"Candidate: `{state['final_skill']}`", "Original files were not overwritten.", ""]
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

