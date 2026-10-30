"""Business analysis and first implementation, gated by the real refinement pipeline."""
from __future__ import annotations

import copy
from datetime import datetime, timezone
import difflib
import hashlib
import json
from pathlib import Path
import re
import shutil
import time

from .agent import Agent
from .common import Cancelled, RefinerError, digest, manifest, read_json, snapshot, write_json
from .pipeline import apply_changes, run as refine
from .profiles import load_config
from .schemas import PLAN, REQUIREMENTS, REVIEW, SCENARIO_INTENT
from .standards import AUTHORING, discover_project_standards, lint
from .stages import StageManifest, CREATE_EVENTS
from .plan import write_test_scenarios, write_work_plan

STRING = {"type": "string", "minLength": 1}
STRINGS = {"type": "array", "items": STRING}
IDS = {"type": "array", "minItems": 1, "uniqueItems": True, "items": STRING}
PYTHON_OBSERVER_GUIDE = """For Python business scripts, cases may set helpers=[\"python_probe\"].
The controller supplies frozen refiner-python-probe.py in each task workspace; do not put its
code in files or modify it. Invocation: python3 refiner-python-probe.py --evidence observed.json
--track input.json --protect-inputs --deny-network --deny-subprocess INSTALLED_SCRIPT input.json.
Resolve INSTALLED_SCRIPT under the authoritative installed root. The helper really executes it
as __main__ with its argv and forwards complete stdout unchanged. Observe the helper command,
not an unobservable inner shell command. tool_output_matches can check its forwarded stdout.
The helper's own exit code is zero for observation transport; assert observed.json /exit_code=0
using file_json_path_equal to check the BUSINESS script's actual exit code. Other evidence keys:
stdout, stdout_json (parsed JSON or null), stderr, inputs_unchanged, write_events, network_events,
subprocess_events, script, script_sha256, argv, input_sha256_before, input_sha256_after.
For no-input-change requirements assert /inputs_unchanged=true, and file_text_equal can independently
check exact UTF-8 input content including whitespace. For no-network tasks assert /network_events=[]
and /subprocess_events=[] with the deny flags. Attempts are recorded and blocked by Python audit
hooks. This observes the tested Python execution, not arbitrary native code or an OS read barrier;
do not invent an absolute guarantee or declare a finite behavioral task impossible for that reason.
Use file_json_path_equal /stdout_json with independent business expectations and response checks.
For multiple invocations use distinct evidence files. Several concrete invalid fixtures may share
one category case; forward complete stdout in invocation order and use json_sequence_equal if
multiple JSON values are requested, with an array of independent expected JSON values. Do not
use text_equal for insignificant key order or whitespace. tool_output_matches supports ordered complete outputs; do not wrap them in
an array unless the script's business contract does. A synthetic invalid-UTF8 fixture can be
created by a small UTF-8 setup Python file writing bytes; the executor runs setup before the
observer. This does not require external binary assets. Keep invalid-byte inputs otherwise
schema-valid if a permissive decoder ignores/replaces the bad byte. Never claim exhaustive coverage from category
labels alone. Do not require an exact newline count or extra features absent from frozen business
requirements. Direct managed command success is already based on actual parsed exit status zero;
observer invocation success alone does not establish the observed script's exit status.
"""
ANALYSIS = {
    "type": "object", "additionalProperties": False,
    "required": ["name", "goal", "requirements", "workflow", "scenarios", "assumptions",
                 "external_dependencies", "blocking_questions"],
    "properties": {
        "name": {"type": "string", "maxLength": 64, "pattern": "^[a-z0-9]+(?:-[a-z0-9]+)*$"},
        "goal": STRING, "requirements": REQUIREMENTS,
        "workflow": {"type": "array", "minItems": 1, "items": {
            "type": "object", "additionalProperties": False,
            "required": ["id", "action", "inputs", "outputs", "completion_check", "failure_behavior", "requirement_ids"],
            "properties": {"id": STRING, "action": STRING, "inputs": STRING, "outputs": STRING,
                           "completion_check": {"type": "string", "minLength": 8},
                           "failure_behavior": STRING, "requirement_ids": IDS}}},
        "scenarios": {"type": "array", "minItems": 3, "items": SCENARIO_INTENT},
        "assumptions": STRINGS, "external_dependencies": STRINGS, "blocking_questions": STRINGS,
    },
}
ANALYSIS_REVIEW = {
    **REVIEW,
    "properties": {**REVIEW["properties"], "requirement_assessments": {
        "type": "array", "minItems": 1, "items": {
            "type": "object", "additionalProperties": False,
            "required": ["requirement_id", "grounded", "acceptance_criteria_atomic",
                         "criteria_observable", "basis"],
            "properties": {"requirement_id": STRING, "grounded": {"type": "boolean"},
                           "acceptance_criteria_atomic": {"type": "boolean"},
                           "criteria_observable": {"type": "boolean"}, "basis": STRING}}},
        "scenario_assessments": {"type": "array", "minItems": 3, "items": {
            "type": "object", "additionalProperties": False,
            "required": ["scenario_id", "detail_executable", "risk_coverage_complete",
                         "traceability_complete", "basis"],
            "properties": {"scenario_id": STRING, "detail_executable": {"type": "boolean"},
                           "risk_coverage_complete": {"type": "boolean"},
                           "traceability_complete": {"type": "boolean"}, "basis": STRING}}},
    },
    "required": ["approved", "issues", "requirement_assessments", "scenario_assessments"],
}
DRAFT = {
    "type": "object", "additionalProperties": False,
    "required": ["implementation_notes", "files"],
    "properties": {"implementation_notes": STRING, "files": {"type": "array", "minItems": 1,
        "maxItems": 20, "items": {"type": "object", "additionalProperties": False,
            "required": ["path", "content", "reason", "requirement_ids"],
            "properties": {"path": STRING, "content": {"type": "string"},
                           "reason": {"type": "string", "minLength": 8}, "requirement_ids": IDS}}}},
}
DRAFT_INSTRUCTION = (
    'Create the complete first implementation of the requested Agent Skill from the approved business '
    'analysis and original requirements. All file paths are relative to the Skill root: use SKILL.md, '
    'scripts/NAME.py and references/NAME.md without a surrounding Skill-name directory. '
    'Return full UTF-8 files, including SKILL.md with valid YAML '
    'name/description matching analysis.name, focused instructions, discoverable references, and working '
    'support scripts wherever deterministic business work benefits from them. Implement the actual workflow, '
    'not placeholders, TODOs, mock results, hardcoded inputs or documentation-only scaffolding. Use the local '
    'runtime and standard library where sufficient. Define concrete invocation, input/output and error '
    'contracts consistent with the user request; record nonmaterial assumptions. A local script should '
    'return machine-checkable results and instructions must require using it when it implements the workflow. '
    'Preserve the requested error output and exit-status contract rather than inventing one. '
    'When a missing business choice, permission, runtime setup, configuration, or dependency can materially '
    'block correct or safe use, include a conditional help-request procedure in the Skill: inspect available '
    'evidence first, explain the concrete blocker and smallest safe next action, ask through the host native '
    'input tool when available (otherwise conversation), and rerun affected checks after the answer. Ask only '
    'for decisions the user must make; record safe local configuration steps, and never request secrets in chat. '
    'Do not add questions for deterministic details that can be handled as explicit assumptions. '
    'Do not instruct an executor to edit its installed Skill. Do not create tests/evals, graders, credentials, '
    'global configuration or fabricated service adapters. Independent tests are generated later. Every file '
    'must cite valid requirement IDs.'
)


def normalize_draft(draft, name):
    """Accept one named packaging directory, preserving every file's contents."""
    result = copy.deepcopy(draft)
    paths = [f['path'] for f in result['files']]
    prefix = name + '/'
    wrapped = prefix + 'SKILL.md' in paths and all(path.startswith(prefix) for path in paths)
    mapping = {}
    for file in result['files']:
        original = file['path']
        if wrapped:
            file['path'] = original[len(prefix):]
        mapping[original] = file['path']
    return result, mapping


def validate_analysis(analysis, requested_name=None, source_ids=None):
    import jsonschema
    jsonschema.validate(analysis, ANALYSIS)
    if requested_name and analysis["name"] != requested_name:
        raise RefinerError("Business analysis changed the requested Skill name")
    ids = [r["id"] for r in analysis["requirements"]]
    if source_ids and any(not any(r['source'].startswith(source + ':') for source in source_ids)
                          for r in analysis['requirements']):
        raise RefinerError('Business requirements must cite an original business source ID followed by a colon; '
                           'authoring standards belong in standards review, not in the business test contract')
    if len(set(ids)) != len(ids) or any(not x.strip() for x in ids):
        raise RefinerError("Business requirement IDs must be nonempty and unique")
    if not {"normal", "boundary", "error"} <= {s["kind"] for s in analysis["scenarios"]}:
        raise RefinerError("Analysis must assess normal, boundary and error scenario categories")
    if not any(s["kind"] == "normal" and s["applicable"] for s in analysis["scenarios"]):
        raise RefinerError("Normal business behavior must have an executable scenario")
    criteria_by_requirement = {r["id"]: {c["id"] for c in r["acceptance_criteria"]}
                               for r in analysis["requirements"]}
    criterion_ids = [criterion["id"] for req in analysis["requirements"]
                     for criterion in req["acceptance_criteria"]]
    if len(criterion_ids) != len(set(criterion_ids)):
        raise RefinerError("Acceptance criterion IDs must be globally unique")
    scenario_ids = [s["id"] for s in analysis["scenarios"]]
    if len(scenario_ids) != len(set(scenario_ids)):
        raise RefinerError("Scenario IDs must be unique")
    scenario_signatures = [json.dumps({"kind": row["kind"], "situation": row["situation"],
                                       "detail": row["detail"]}, ensure_ascii=False,
                                      sort_keys=True).casefold()
                           for row in analysis["scenarios"]]
    if len(scenario_signatures) != len(set(scenario_signatures)):
        raise RefinerError("Scenario matrix contains duplicate situations and execution details")
    for item in analysis["workflow"] + analysis["scenarios"]:
        if not set(item["requirement_ids"]) <= set(ids):
            raise RefinerError("Workflow or scenario references an unknown business requirement")
    for scenario in analysis["scenarios"]:
        allowed_criteria = set().union(*(criteria_by_requirement[rid]
                                         for rid in scenario["requirement_ids"])) if scenario["requirement_ids"] else set()
        if not set(scenario["acceptance_criteria_ids"]) <= allowed_criteria:
            raise RefinerError(f"Scenario {scenario['id']} references an acceptance criterion outside its requirements")
        if scenario["applicable"] and (not scenario["requirement_ids"] or not scenario["acceptance_criteria_ids"]):
            raise RefinerError(f"Applicable scenario {scenario['id']} must map to requirements and acceptance criteria")
        if not scenario["applicable"] and not scenario["exclusion_reason"].strip():
            raise RefinerError(f"Excluded scenario {scenario['id']} needs a scope-based reason")
    required_criteria = {c["id"] for r in analysis["requirements"] if r["required"]
                         for c in r["acceptance_criteria"]}
    covered_criteria = {criterion_id for scenario in analysis["scenarios"] if scenario["applicable"]
                        for criterion_id in scenario["acceptance_criteria_ids"]}
    if not required_criteria <= covered_criteria:
        raise RefinerError("Scenario matrix omits required acceptance criteria: " +
                           ", ".join(sorted(required_criteria - covered_criteria)))


def validate_analysis_review(review, analysis):
    import jsonschema
    jsonschema.validate(review, ANALYSIS_REVIEW)
    rows = review["requirement_assessments"]
    expected = {r["id"] for r in analysis["requirements"]}
    if len(rows) != len(expected) or {r["requirement_id"] for r in rows} != expected:
        raise RefinerError("Analysis review must assess every requirement exactly once")
    scenarios = review["scenario_assessments"]
    expected_scenarios = {row["id"] for row in analysis["scenarios"]}
    assessed_scenarios = [row["scenario_id"] for row in scenarios]
    if len(assessed_scenarios) != len(set(assessed_scenarios)) or set(assessed_scenarios) != expected_scenarios:
        raise RefinerError("Analysis review must assess every scenario exactly once")
    req_ok = all(all(row[key] for key in ("grounded", "acceptance_criteria_atomic", "criteria_observable"))
                 for row in rows)
    scenario_ok = all(all(row[key] for key in
                          ("detail_executable", "risk_coverage_complete", "traceability_complete"))
                      for row in scenarios)
    if review["approved"] and not (req_ok and scenario_ok):
        raise RefinerError("Analysis approval contradicts requirement or scenario completeness assessments")


def analysis_markdown(analysis):
    lines = ["# Business analysis", "", analysis["goal"], "", "## Requirements", ""]
    for req in analysis["requirements"]:
        lines.append(f"- {req['id']}: {req['behavior']} (source: {req['source']}; required={req['required']})")
        lines.extend(f"  - {criterion['id']}: {criterion['statement']} Evidence: {criterion['evidence']}"
                     for criterion in req["acceptance_criteria"])
    lines += ["", "## Workflow", ""]
    for step in analysis["workflow"]:
        lines += [f"- {step['id']}: {step['action']}", f"  Input: {step['inputs']}; output: {step['outputs']}",
                  f"  Completion check: {step['completion_check']}",
                  f"  Failure: {step['failure_behavior']}; requirements: {', '.join(step['requirement_ids'])}"]
    lines += ["", "## Detailed scenario matrix", ""]
    for scenario in analysis["scenarios"]:
        detail = scenario["detail"]
        lines += [f"- {scenario['id']} ({scenario['kind']}; applicable={scenario['applicable']}): {scenario['situation']}",
                  f"  Requirements: {', '.join(scenario['requirement_ids'])}; criteria: "
                  f"{', '.join(scenario['acceptance_criteria_ids'])}",
                  f"  Preconditions: {'; '.join(detail['preconditions'])}",
                  f"  Stimulus: {detail['stimulus']}",
                  f"  Actions: {'; '.join(detail['actions'])}",
                  f"  Expected: {'; '.join(detail['expected_observables'])}",
                  f"  Forbidden: {'; '.join(detail['forbidden_outcomes'])}",
                  f"  Risk dimensions: {', '.join(detail['risk_dimensions'])}; basis: {scenario['basis']}"]
        if not scenario["applicable"]:
            lines.append(f"  Excluded because: {scenario['exclusion_reason']}")
    for title, key in [("Assumptions", "assumptions"), ("External dependencies", "external_dependencies"),
                       ("Blocking questions", "blocking_questions")]:
        lines += ["", "## " + title, ""] + ["- " + x for x in analysis[key]]
    return "\n".join(lines) + "\n"


def run(args):
    resume = Path(args.resume_from).resolve() if getattr(args,'resume_from',None) else None
    resume_run = Path(args.resume_run).resolve() if getattr(args, 'resume_run', None) else None
    resume_was_verified = False
    if resume and resume_run:
        raise RefinerError("Use either --resume-from or --resume-run, not both")
    if not (args.description or args.spec or resume or resume_run):
        raise RefinerError("create needs a business description or --spec")
    if args.name and not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", args.name):
        raise RefinerError("--name must be kebab-case")
    if args.name and len(args.name) > 64:
        raise RefinerError("--name must be at most 64 characters")
    config = load_config(args.config)
    if config and (args.agent_command or args.engine_config or args.engine is not None):
        raise RefinerError("Use --config alone for backend selection")
    if not config and not args.agent_command:
        raise RefinerError("Select a controller backend with --config or --agent-command")
    if not config and not args.engine_config and args.engine is None:
        raise RefinerError("Select an evaluation backend with --config, --engine-config or --engine")
    if resume_run:
        work = resume_run
        if not work.is_dir() or not (work / "state.json").is_file():
            raise RefinerError("--resume-run must name an existing creation workspace")
        if args.output and Path(args.output).resolve() != work:
            raise RefinerError("--output must match --resume-run when continuing a creation workspace")
        standard_paths = []
        destination = None
        inputs = read_json(work / "inputs.json")
        previous_contract = read_json(work / "creation-resume-contract.json")
        previous_state = read_json(work / "state.json")
        resume_was_verified = (previous_state.get("status") == "created"
                               and previous_state.get("verification_status") == "verified")
        if args.description is not None and args.description != inputs.get("business_description"):
            raise RefinerError("Resume business description differs from the frozen creation inputs")
        if args.name and args.name != inputs.get("requested_name"):
            raise RefinerError("Resume name differs from the frozen creation inputs")
        if args.spec:
            supplied = [{"source": str(Path(p).resolve()), "content": Path(p).read_text(encoding="utf-8")} for p in args.spec]
            if supplied != inputs.get("requirements"):
                raise RefinerError("Resume requirements differ from the frozen creation inputs")
        if args.standard:
            supplied = [{"source": str(Path(p).resolve()), "content": Path(p).read_text(encoding="utf-8")} for p in args.standard]
            frozen_standards = {row.get("source"): row.get("content")
                                for row in inputs.get("standards", [])[1:] if isinstance(row, dict)}
            if any(frozen_standards.get(row["source"]) != row["content"] for row in supplied):
                raise RefinerError("Resume standards differ from the frozen creation inputs")
        saved_destination = previous_contract.get("destination")
        if args.destination and str(Path(args.destination).resolve()) != saved_destination:
            raise RefinerError("Resume destination differs from the frozen creation contract")
        destination = Path(saved_destination).resolve() if saved_destination else None
    else:
        standard_target = getattr(args, "destination", None) or getattr(args, "output", None)
        standard_paths = discover_project_standards(standard_target, getattr(args, "standard", ()), Path.cwd())
        for reference in args.spec + standard_paths:
            if not Path(reference).is_file():
                raise RefinerError("Reference file does not exist: " + reference)
        work = Path(args.output).resolve() if args.output else Path.cwd() / '.refiner-runs' / (
            'create-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ'))
        if work.exists():
            raise RefinerError("Creation output must be a new directory")
        destination = Path(args.destination).resolve() if args.destination else None
        if destination and (destination.exists() or destination.is_relative_to(work) or work.is_relative_to(destination)):
            raise RefinerError("Destination must be new and outside the creation workspace")
        work.mkdir(parents=True)
    started = time.monotonic()
    elapsed_before = float(previous_state.get("elapsed_seconds", 0.0)) if resume_run else 0.0
    deadline = started + max(0.0, args.max_seconds - elapsed_before)
    if resume_run:
        state = copy.deepcopy(previous_state)
        state.setdefault("resume_history", []).append({"previous_status": previous_state.get("status"),
                                                        "elapsed_seconds": elapsed_before})
        state.update(status="resuming", elapsed_seconds=elapsed_before)
        for key in ("error", "verification_status", "verification_error"):
            state.pop(key, None)
    else:
        state = {"status": "analyzing", "mode": "create", "workspace": str(work), "questions": []}
    agent = Agent(work, args.agent_command, args.timeout, config=config)
    agent.max_calls, agent.deadline = args.max_agent_calls, deadline
    if not resume_run:
        inputs = {"business_description": args.description or "", "requested_name": args.name,
                  "requirements": [{"source": str(Path(p).resolve()), "content": Path(p).read_text(encoding="utf-8")} for p in args.spec],
                  "standards": [AUTHORING] + [{"source": str(Path(p).resolve()), "content": Path(p).read_text(encoding="utf-8")} for p in standard_paths]}
    engine_config = read_json(args.engine_config) if args.engine_config else None
    execution = {"backend_config": config, "agent_command": args.agent_command, "engine": args.engine,
                 "engine_config": engine_config, "skill_up": args.skill_up, "timeout": args.timeout,
                 "parallelism": args.parallelism,
                 "agent_parallelism": getattr(args, "agent_parallelism", 2)}
    options = {"max_rounds": args.max_rounds, "planning_attempts": args.planning_attempts,
               "max_cases": args.max_cases, "acceptance_cases": args.acceptance_cases}
    limits = {"max_seconds": args.max_seconds, "max_agent_calls": args.max_agent_calls,
              "max_evaluations": args.max_evaluations, "max_rounds": args.max_rounds,
              "planning_attempts": args.planning_attempts, "max_cases": args.max_cases,
              "acceptance_cases": args.acceptance_cases}
    state["limits"] = limits
    state["agent_parallelism"] = getattr(args, "agent_parallelism", 2)
    state.setdefault("evaluation_history", [])
    controller_root = Path(__file__).resolve().parent.parent
    controller_sha256 = digest({path.relative_to(controller_root).as_posix():
        hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(controller_root.rglob("*.py")) if "__pycache__" not in path.parts})
    if resume_run:
        if (previous_contract.get("schema_version") != "skill-refiner.creation-resume/v1"
                or previous_contract.get("controller_sha256") != controller_sha256):
            raise RefinerError("Creation controller or resume contract changed; start a new creation")
        if previous_contract.get("inputs_sha256") != digest(inputs):
            raise RefinerError("Frozen creation inputs changed; start a new creation")
        if previous_contract.get("execution_sha256") != digest(execution):
            raise RefinerError("Resume backend or execution settings differ from the original creation")
        prior_options = previous_contract.get("options", {})
        if (options["max_cases"] != prior_options.get("max_cases")
                or options["acceptance_cases"] != prior_options.get("acceptance_cases")
                or options["planning_attempts"] < prior_options.get("planning_attempts", 1)
                or options["max_rounds"] < prior_options.get("max_rounds", 1)):
            raise RefinerError("Resume may increase planning/round budgets, but must keep frozen case limits")
        prior_limits = previous_contract.get("limits", {})
        if any(limits[name] < prior_limits.get(name, 1) for name in limits):
            raise RefinerError("Resume budgets cannot be lower than the original creation")
        if resume_was_verified:
            from .gate import verify_run
            verify_run(work)
            return previous_state
        previous_contract["options"], previous_contract["limits"] = options, limits
        write_json(work / "creation-resume-contract.json", previous_contract)
    else:
        previous_contract = {"schema_version": "skill-refiner.creation-resume/v1",
                             "inputs_sha256": digest(inputs), "execution_sha256": digest(execution),
                             "controller_sha256": controller_sha256, "destination": str(destination) if destination else None,
                             "options": options, "limits": limits}
        write_json(work / "creation-resume-contract.json", previous_contract)
    resumed_analysis, resumed_draft, resumed_suite = None, None, None
    if resume:
        previous_inputs = read_json(resume/'inputs.json')
        resumed_analysis = read_json(resume/'analysis.json')
        previous_lock = read_json(resume/'business.lock.json')
        if previous_lock['sha256'] != digest({'inputs':previous_inputs,'analysis':resumed_analysis}) or previous_lock['input_sha256'] != digest(previous_inputs):
            raise RefinerError('Resume business contract hash mismatch')
        if (args.description or args.spec or standard_paths or args.name) and inputs != previous_inputs:
            raise RefinerError('Resume requires identical original business inputs; start a new creation for changed requirements')
        inputs = previous_inputs
        previous_state = read_json(resume/'state.json')
        resumed_draft = Path(previous_state['draft_root'])
        if manifest(resumed_draft) != read_json(resume/'draft-manifest.json'):
            raise RefinerError('Resume first-draft hash mismatch')
        validate_analysis(resumed_analysis, inputs['requested_name'])
        if resumed_analysis['blocking_questions'] or any(f['severity']=='error' for f in lint(resumed_draft,resumed_analysis['name'])):
            raise RefinerError('Resume requires approved analysis and a statically valid first draft')
        state['creation_provenance'] = {'workspace':str(resume),'business_lock':previous_lock['sha256'],
                                        'prior_creation_agent_calls':previous_state.get('creation_agent_calls',0),
                                        'reuses_verdicts':False}
        if (resume/'refinement/oracle.lock.json').is_file():
            resumed_suite = resume/'refinement'
        review_path = resume / 'analysis-review.json'
        if review_path.is_file():
            shutil.copy2(review_path, work / 'analysis-review.json')
    if not resume_run:
        previous_contract["inputs_sha256"] = digest(inputs)
        write_json(work / "creation-resume-contract.json", previous_contract)
    resume_analysis_complete = False
    resume_analysis_lock = None
    if resume_run and (work / "analysis.json").is_file() and (work / "business.lock.json").is_file():
        import jsonschema
        try:
            resumed_analysis = read_json(work / "analysis.json")
            saved_business_lock = read_json(work / "business.lock.json")
            saved_review = read_json(work / "analysis-review.json")
            validate_analysis(resumed_analysis, inputs["requested_name"])
            validate_analysis_review(saved_review, resumed_analysis)
            resume_analysis_complete = (
                not resumed_analysis["blocking_questions"] and saved_review["approved"] is True
                and saved_business_lock.get("input_sha256") == digest(inputs)
                and saved_business_lock.get("sha256") == digest({"inputs": inputs, "analysis": resumed_analysis}))
            if resume_analysis_complete:
                resume_analysis_lock = saved_business_lock["sha256"]
        except (OSError, ValueError, KeyError, TypeError, jsonschema.ValidationError, jsonschema.SchemaError):
            resumed_analysis = None
            resume_analysis_complete = False
    if resume_run and resume_analysis_complete and (work / "draft-manifest.json").is_file():
        expected_draft_manifest = read_json(work / "draft-manifest.json")
        draft_candidates = []
        if previous_state.get("draft_root"):
            draft_candidates.append(Path(previous_state["draft_root"]).resolve())
        draft_candidates.extend(sorted((work / "drafts").glob("*/" + resumed_analysis["name"]), reverse=True)
                                if (work / "drafts").is_dir() else [])
        for candidate_draft in draft_candidates:
            if (candidate_draft.is_relative_to(work) and candidate_draft.is_dir()
                    and manifest(candidate_draft) == expected_draft_manifest
                    and not any(f["severity"] == "error" for f in lint(candidate_draft, resumed_analysis["name"]))):
                resumed_draft = candidate_draft
                break
    has_stage_manifest = resume_run and (work / "stages.json").is_file()
    stages = StageManifest(work, "create", resume=bool(has_stage_manifest))
    print(json.dumps({"status": "started", "workspace": str(work),
                      "work_plan": str(work / "work-plan.md"),
                      "state_file": str(work / "state.json"),
                       "stage_manifest": str(work / "stages.json"), "limits": limits,
                      "max_rounds": args.max_rounds}, ensure_ascii=False), flush=True)
    materialize_inputs = not stages.is_completed("inputs")
    if materialize_inputs:
        write_json(work / "inputs.json", inputs)
        (work / 'inputs').mkdir(exist_ok=True)
    request_file = work / 'inputs/business-request.md'
    if materialize_inputs:
        request_file.write_text(inputs['business_description'] + '\n', encoding='utf-8')
    spec_paths = []
    for index, requirement in enumerate(inputs['requirements']):
        path = work / 'inputs' / f'requirement-{index+1}.md'
        if materialize_inputs:
            path.write_text(requirement['content'], encoding='utf-8')
        spec_paths.append(str(path))
    standard_paths = []
    for index, standard in enumerate(inputs['standards'][1:]):
        path = work / 'inputs' / f'standard-{index+1}.md'
        if materialize_inputs:
            path.write_text(standard['content'], encoding='utf-8')
        standard_paths.append(str(path))
    if materialize_inputs:
        stages.complete("inputs")
    input_hash = digest(inputs)
    business_sources = ([{'id': 'business_description', 'content': inputs['business_description']}] if inputs['business_description'] else []) + [
        {'id': f'spec-{index+1}', 'source': row['source'], 'content': row['content']}
        for index, row in enumerate(inputs['requirements'])]
    refinement = None
    analysis = resumed_analysis
    locked = resume_analysis_lock

    def event(status, **extra):
        if status in CREATE_EVENTS:
            stage_id = CREATE_EVENTS[status]
            if not (resume_run and stages.is_completed(stage_id)):
                stages.activate(stage_id)
        state.update(status=status, elapsed_seconds=round(elapsed_before + time.monotonic() - started, 2),
                     creation_agent_calls=agent.calls, agent_calls=agent.calls + (refinement or {}).get("agent_calls", 0),
                     cached_agent_calls=agent.cached_calls,
                     target_evaluations=(refinement or {}).get("target_evaluations", 0), **extra)
        write_json(work / 'state.json', state)
        print(json.dumps({'status': status, 'elapsed_seconds': state['elapsed_seconds'],
                          'max_seconds': args.max_seconds, **extra}, ensure_ascii=False), flush=True)

    def integrity():
        if digest(read_json(work / 'inputs.json')) != input_hash or request_file.read_text(encoding="utf-8") != inputs['business_description'] + '\n':
            raise RefinerError('Frozen business inputs changed')
        if locked and digest({'inputs': inputs, 'analysis': read_json(work / 'analysis.json')}) != locked:
            raise RefinerError('Frozen business contract changed')
        if time.monotonic() >= deadline or elapsed_before + time.monotonic() - started > args.max_seconds:
            raise RefinerError('Creation wall-clock budget exhausted')

    event('resuming' if resume or resume_run else 'analyzing')
    try:
        feedback = []
        for attempt in range(0 if resume or resume_analysis_complete else 2):
            analysis = agent.ask('analyze',
                'Analyze the supplied business request into a scoped executable Agent Skill. Choose a concise kebab-case name '
                'unless requested_name is supplied. Extract all business requirements with stable IDs and concrete source '
                'citations into business_sources. Every requirement.source must begin with its source ID plus a colon '
                '(for example spec-1: followed by the supporting passage). Include only business requirements from '
                'these sources. For every workflow step, include a concrete completion_check that names observable '
                'evidence proving the step is complete; do not use generic wording such as "done". Authoring standards, trigger description quality, script organization and controller '
                'integrity remain in standards review; do not add them as business test requirements unless the user '
                'explicitly requested that as an externally observable business capability. Split each material business '
                'requirement into small, independently observable acceptance_criteria; each criterion has a stable ID, one '
                'outcome and evidence that could prove it. Do not combine separate decisions, outputs, permissions or side '
                'effects into one criterion. Analyze the full workflow: actors, input variants, preconditions, state transitions, '
                'decisions, outputs, side effects, dependencies, permissions, failure behavior, retries, interruption and '
                'recovery where they apply. Build a scenario inventory with stable IDs; use multiple scenarios per category '
                'when distinct requirements or risk conditions need different evidence. Assess normal, boundary and error '
                'categories, and give a concrete scope reason for exclusions. Each applicable scenario states preconditions, '
                'a concrete stimulus, ordered actions, observable expected outcomes, forbidden outcomes, risk dimensions, '
                'linked requirement IDs and acceptance criterion IDs, and a source-based basis. Every required criterion must '
                'appear in at least one applicable scenario. Distinguish scenarios that exercise different actors, states, '
                'inputs, dependencies, side effects or recovery paths; paraphrases and cosmetic data changes do not add coverage. '
                'These are detailed scenario intents, not frozen executable tests or implementation. '
                'Do not add arbitrary business rules, integrations or approval policies. Explicitly record reasonable '
                'nonmaterial implementation assumptions; missing material business rules belong in blocking_questions. '
                'Each blocking question must be concise and answerable, state the material choice needed, and avoid '
                'requesting credentials or asking the user to diagnose an operational failure that can be checked. '
                'Do not claim an external dependency is verified or replace real services with fabricated success. '
                'Keep dependencies to the user scope. A category is inapplicable only with a concrete scope-based reason; '
                'normal behavior is always applicable. Do not implement code or provide test answers at this stage.',
                {**inputs, 'business_sources': business_sources, 'validation_feedback': feedback}, ANALYSIS)
            write_json(work / f'analysis-attempt-{attempt+1}.json', analysis)
            try:
                validate_analysis(analysis, args.name, {s['id'] for s in business_sources})
                if analysis['blocking_questions']:
                    write_json(work / 'analysis.json', analysis)
                    event('needs_clarification', questions=analysis['blocking_questions'])
                    return state
                review = agent.ask('analysis-review',
                    'Independently review business analysis against the original request and supplied standards. '
                    'Check every requested behavior is retained, requirements are decomposed into atomic acceptance '
                    'criteria with independently observable evidence, and every criterion is grounded; cite the '
                    'specific request or explicit reasonable implementation assumption for every assessment. Assess every '
                    'scenario ID exactly once. Reject scenarios lacking preconditions, a concrete stimulus, ordered actions, '
                    'observable expected outcomes, forbidden outcomes, risk dimensions or requirement/criterion traceability. '
                    'Confirm every required criterion is covered by an applicable scenario; inspect workflow-state, permissions, '
                    'dependencies, cross-requirement effects, side effects, retries/idempotency and recovery when they apply. '
                    'Reject duplicate scenarios that only paraphrase the same situation and evidence. An excluded scenario '
                    'needs a concrete scope-based explanation. Check '
                    'that each workflow step has a concrete observable completion_check, and that its inputs, outputs, '
                    'failure behavior and requirement links are clear. Reject '
                    'invented material business rules, omitted workflows, fabricated dependencies, arbitrary error '
                    'contracts and unjustified exclusions of normal/boundary/error scenarios. Do not require features '
                    'the user excluded. Approve only a clear, scoped contract suitable for independent test planning.',
                    {'inputs': inputs, 'analysis': analysis}, ANALYSIS_REVIEW)
                write_json(work / 'analysis-review.json', review)
                validate_analysis_review(review, analysis)
                if not review['approved']:
                    raise RefinerError('; '.join(review['issues']))
            except RefinerError as exc:
                if attempt:
                    raise
                feedback = [str(exc)]
                continue
            break
        write_json(work / 'analysis.json', analysis)
        (work / 'business-analysis.md').write_text(analysis_markdown(analysis), encoding="utf-8")
        write_work_plan(work, analysis=analysis, limits=limits,
                        agent_parallelism=getattr(args, "agent_parallelism", 2))
        locked = digest({'inputs': inputs, 'analysis': analysis})
        write_json(work / 'business.lock.json', {'sha256': locked, 'input_sha256': input_hash})
        integrity()
        event('reusing_draft' if resumed_draft else 'creating', skill_name=analysis['name'])
        feedback = []
        previous_draft = None
        if resume_run and resumed_draft:
            draft_root = resumed_draft
        elif resumed_draft:
            draft_root = work/'drafts/reused'/analysis['name']
            snapshot(resumed_draft,draft_root)
        draft_numbers = [int(path.name) for path in (work / "drafts").iterdir()
                         if path.is_dir() and path.name.isdigit()] if (work / "drafts").is_dir() else []
        draft_offset = max(draft_numbers, default=0)
        for attempt in range(0 if resumed_draft else 2):
            draft = agent.ask('draft',
                DRAFT_INSTRUCTION,
                {'inputs': inputs, 'analysis': analysis, 'validation_feedback': feedback,
                 **({'previous_draft': previous_draft} if previous_draft else {})}, DRAFT)
            previous_draft = draft
            draft, mapping = normalize_draft(draft, analysis['name'])
            draft_root = work / 'drafts' / str(draft_offset + attempt + 1) / analysis['name']
            draft_root.mkdir(parents=True)
            write_json(draft_root.parent / 'path-mapping.json', mapping)
            try:
                apply_changes(draft_root, {'changes': [{'path': f['path'], 'content': f['content'],
                    'reason': f['reason'], 'evidence_ids': f['requirement_ids']} for f in draft['files']]},
                    {r['id'] for r in analysis['requirements']})
                findings = lint(draft_root, analysis['name'])
                write_json(draft_root.parent / 'static-findings.json', findings)
                if any(f['severity'] == 'error' for f in findings):
                    raise RefinerError('Invalid first implementation: ' + json.dumps(findings, ensure_ascii=False))
            except RefinerError as exc:
                if attempt:
                    raise
                feedback = [str(exc)]
                continue
            break
        initial_manifest = manifest(draft_root)
        state['draft_root'] = str(draft_root)
        write_json(work / 'draft-manifest.json', initial_manifest)
        integrity()
        remaining = args.max_agent_calls - agent.calls
        if remaining < 1:
            raise RefinerError('No role-call budget left for tests and refinement')
        child = copy.copy(args)
        child.target, child.output, child.reuse_plan = str(draft_root), str(work / 'refinement'), None
        child.resume_from = None
        # Use the saved original requirements, not an implementation-derived spec.
        child.spec = ([str(request_file)] if inputs['business_description'] else []) + spec_paths
        child.standard = standard_paths
        outer_remaining = max(1, int(deadline - time.monotonic()))
        child.max_seconds, child.max_agent_calls = outer_remaining, remaining
        child.plan_only = bool(getattr(args, 'plan_only', False))
        if resume_run and Path(child.output).is_dir():
            child_contract_path = Path(child.output) / "resume-contract.json"
            if child_contract_path.is_file():
                child_state = read_json(Path(child.output) / "state.json")
                child.resume_from = child.output
                child.max_seconds = max(1, int(float(child_state.get("elapsed_seconds", 0.0)) + outer_remaining))
            else:
                attempts = Path(child.output).parent / "refinement-resume-attempts"
                attempts.mkdir(exist_ok=True)
                prior_attempts = [int(path.name.split("-", 1)[1]) for path in attempts.glob("attempt-*")
                                  if path.is_dir() and path.name.split("-", 1)[1].isdigit()]
                archived = attempts / f"attempt-{max(prior_attempts, default=0)+1:03}"
                shutil.move(child.output, archived)
        applicable_scenarios = [s for s in analysis['scenarios'] if s['applicable']]
        child.creation_context = {'contract': analysis['requirements'], 'business_analysis': analysis,
                                  'execution_helpers': PYTHON_OBSERVER_GUIDE,
                                  'required_case_kinds': sorted({s['kind'] for s in applicable_scenarios}),
                                  'required_scenario_ids': [s['id'] for s in applicable_scenarios]}
        if resumed_suite:
            prior_context = read_json(resumed_suite/'inputs.json')
            child.spec = [row['source'] for row in prior_context['requirements']]
            child.standard = [row['source'] for row in prior_context['standards'] if isinstance(row,dict)]
            child.reuse_plan = str(resumed_suite)
        scenario_minimum = max(len(child.creation_context['required_scenario_ids']), 2)
        if scenario_minimum > min(args.max_cases, args.acceptance_cases):
            raise RefinerError('Development/acceptance case limits cannot cover every applicable scenario ID; '
                               f'{scenario_minimum} scenarios require at least that many cases in each suite')
        event('refining', refinement_workspace=child.output)
        refinement = refine(child)
        integrity()
        if manifest(draft_root) != initial_manifest:
            raise RefinerError('Original generated draft changed during refinement')
        state['refinement_status'] = refinement['status']
        state['evaluation_history'] = refinement.get('evaluation_history', state.get('evaluation_history', []))
        state['refinement_rounds'] = refinement.get('rounds', [])
        state['target_evaluations'] = refinement.get('target_evaluations', 0)
        write_work_plan(work, source=draft_root, analysis=analysis, limits=limits,
                        agent_parallelism=getattr(args, "agent_parallelism", 2))
        if refinement['status'] == 'planned':
            state['refinement_workspace'] = str(child.output)
            state['plan_ready'] = True
            event('plan_ready', refinement_workspace=str(child.output),
                  work_plan=str(Path(child.output) / 'work-plan.md'),
                  development_cases=refinement.get('planned_development_cases'),
                  acceptance_cases=refinement.get('planned_acceptance_cases'),
                  max_rounds=args.max_rounds, max_seconds=args.max_seconds,
                  max_evaluations=args.max_evaluations)
            state.update(status='planned', planned_only=True,
                         elapsed_seconds=round(elapsed_before + time.monotonic() - started, 2))
            write_json(work / 'state.json', state)
            return state
        if refinement['status'] not in {'passed', 'unchanged'}:
            event(refinement['status'], error=refinement.get('error'), questions=refinement.get('gaps', []))
            return state
        final = work / 'final' / analysis['name']
        refinement_final = Path(refinement['final_skill'])
        if final.is_dir() and manifest(final) == manifest(refinement_final):
            pass
        else:
            if final.exists():
                attempts = work / 'final' / 'resume-attempts'
                attempts.mkdir(parents=True, exist_ok=True)
                prior_attempts = [int(path.name.split('-', 1)[1]) for path in attempts.glob('attempt-*')
                                  if path.is_dir() and path.name.split('-', 1)[1].isdigit()]
                archived = attempts / f"attempt-{max(prior_attempts, default=0)+1:03}"
                archived.mkdir()
                shutil.move(str(final), str(archived / final.name))
            snapshot(refinement_final, final)
        # The repair diff may be empty when the first implementation already passes.
        (work / 'final/changes.patch').write_text((work / 'refinement/final/changes.patch').read_text(encoding="utf-8"), encoding="utf-8")
        full_patch = []
        for path in sorted(manifest(final)):
            full_patch.extend(difflib.unified_diff([], (final / path).read_text(encoding="utf-8").splitlines(True),
                                                  '/dev/null', 'created/' + path))
        (work / 'final/creation.patch').write_text(''.join(full_patch), encoding="utf-8")
        write_json(work / 'final/evidence.json', {'business_lock': locked, 'draft_manifest': initial_manifest,
                   'candidate_manifest': manifest(final), 'refinement_evidence': str(work / 'refinement/final/evidence.json'),
                   'refinement_evidence_sha256': digest(read_json(work / 'refinement/final/evidence.json'))})
        event('created', final_skill=str(final))
        return state
    except Cancelled as exc:
        event('cancelled', error=str(exc))
        return state
    except Exception as exc:
        event('error', error=str(exc))
        return state
    finally:
        if analysis is not None and not (work / 'business-analysis.md').exists():
            (work / 'business-analysis.md').write_text(analysis_markdown(analysis), encoding="utf-8")
        state.update(agent_calls=agent.calls + (refinement or {}).get('agent_calls', 0),
                     creation_agent_calls=agent.calls, target_evaluations=(refinement or {}).get('target_evaluations', 0),
                     cached_agent_calls=agent.cached_calls,
                     elapsed_seconds=round(elapsed_before + time.monotonic() - started, 2))
        write_json(work / 'state.json', state)
        lines = ['# Business Skill creation', '', 'Status: **' + state['status'] + '**', '',
                 'Business analysis: [business-analysis.md](business-analysis.md)',
                 'Design and tests: [work-plan.md](work-plan.md)',
                 'Refinement results: [refinement/report.md](refinement/report.md)', '',
                 'Role calls: ' + str(state['agent_calls']), 'Evaluations: ' + str(state['target_evaluations']), '',
                 'First drafts and original business inputs are preserved. Failed or incomplete runs are not delivered as final Skills.']
        if state.get('final_skill'):
            lines += ['', 'Final candidate: `' + state['final_skill'] + '`',
                      'Creation diff: [final/creation.patch](final/creation.patch)',
                      'Repair diff: [final/changes.patch](final/changes.patch)']
        if state.get('error'):
            lines += ['', 'Error: ' + state['error']]
        lines += ['', '## Questions / gaps', ''] + ['- ' + q for q in state.get('questions', [])]
        (work / 'report.md').write_text('\n'.join(lines) + '\n')
        write_test_scenarios(work)
        if state.get('status') == 'created':
            stages.activate('verification')
            try:
                from .gate import verify_run
                verification = verify_run(work)
                write_json(work / 'verification.json', verification)
                state['verification_status'] = verification['status']
                if destination:
                    if destination.exists():
                        if not destination.is_dir() or manifest(destination) != manifest(state['final_skill']):
                            raise RefinerError('Saved destination already exists with different contents')
                    else:
                        snapshot(state['final_skill'], destination)
                    state['destination'] = str(destination)
            except Exception as exc:
                state.update(status='verification_failed', verification_error=str(exc))
            write_json(work / 'state.json', state)
            lines.insert(3, 'Verification: **' + state.get('verification_status', 'failed') + '**')
            if state.get('verification_error'):
                lines += ['', 'Verification error: ' + state['verification_error']]
            (work / 'report.md').write_text('\n'.join(lines) + '\n')
        stages.finish(state.get('status', 'error'))
