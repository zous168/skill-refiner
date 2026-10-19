from __future__ import annotations

from datetime import datetime, timezone
import difflib
import json
import hashlib
import copy
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
from .matrix import Matrix


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
When the response must preserve that script's output, use tool_output_matches
in addition to a content assertion. Its value is a command regex and control_command is a concrete
matching example invocation for positive/negative trace controls. This checks that the final JSON/text
actually equals the successful tool output, rejecting manual correction of a defective script.
Use text_equal when exact non-whitespace response text is required. Insignificant
JSON formatting differences are not missing coverage unless explicitly required by business rules.
At least one case must have kind=normal and at least one kind=boundary, error or negative.
Use only supported declarative checks. Provide good_response and clearly wrong bad_response controls
for response checks. JSON checks should prefer json_path_equal if the output has optional fields.
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
only the requested JSON/stdout. tool_command sees direct recorded tool-call commands, not
subprocess argv constructed inside a Python helper or heredoc. If a task legitimately delegates
CLI execution to a verifier helper, check that helper's observable invocation and its independent
captured-result assertions instead of adding an unobservable inner-command regex. A fixture
verifier must inspect actual subprocess argv, exit status and output rather than return an
unsubstantiated attestation. Avoid prescribing an arbitrary invocation style solely to satisfy
a regex when equivalent execution is part of the allowed target behavior."""


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
                           "explanations/links/summaries alongside the answer. Assess trace_checks_observable: tool_command "
                           "only sees direct tool-call command strings. A CLI invoked through a helper's Python "
                           "subprocess argv or heredoc is not observable as a literal shell CLI command. If that is "
                           "allowed by the task, an inner-command regex makes a valid execution fail; instead check "
                           "the visible helper and its actual captured-process assertions. Cite concrete prompt and "
                           "checker pairs in the basis. Approve only a useful executable suite.",
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
                       {"files": inventory(target), "standards": context["standards"],
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
    context = {"files": inventory(original), "requirements": [], "standards": [AUTHORING]}
    context.update(getattr(args, "creation_context", {}))
    for path in args.spec:
        context["requirements"].append({"source": str(Path(path).resolve()), "content": Path(path).read_text()})
    for path in args.standard:
        context["standards"].append({"source": str(Path(path).resolve()), "content": Path(path).read_text()})
    write_json(work / "inputs.json", context)
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
            response = agent.ask("repair", "Diagnose the failed cases and material standards violations. "
                                 "Return narrow full-file replacements with concrete reasons and evidence_ids. "
                                 "Preserve the Skill's identity, scope and valid existing behavior. Fix supporting "
                                 "scripts if they cause failure, not just prose. Never edit evals, tests, requirements "
                                 "or grading criteria, and never hardcode known test inputs or expected answers. "
                                 "Do not add capabilities not required by the supplied contract. Return changes=[] "
                                 "and unresolved issues if repair cannot be justified. unresolved lists only remaining "
                                 "required behaviors, not intentional scope exclusions. The controller runs your patch "
                                 "afterwards: not having executed it yourself is not an unresolved behavior. "
                                 "Use only allowed_evidence_ids, not requirement IDs. You do not see acceptance cases.",
                                 {"files": inventory(best), "requirements": plan["requirements"],
                                  "external_requirements": context["requirements"], "standards": context["standards"],
                                  "allowed_evidence_ids": sorted(evidence_ids),
                                  "failures": failing, "findings": errors,
                                  "previous_unresolved": unresolved_evidence,
                                  "failure_evidence": repair_evidence(failing),
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
        event("acceptance")
        final_checks = evaluate(best, acceptance["cases"], work / "acceptance")
        budget()
        if not final_checks["passed"]:
            event("failed_acceptance", best_candidate=str(best))
            return state
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


def write_report(work, state):
    lines = ["# Skill Refiner result", "", f"Status: **{state['status']}**", "",
             f"Original: `{state['source']}`", f"Agent calls: {state.get('agent_calls', 0)}",
             f"Elapsed: {state.get('elapsed_seconds', 0)} seconds", ""]
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
                lines += [f"## {phase}", "", (path.parent / "comparison.md").read_text(), ""]
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

