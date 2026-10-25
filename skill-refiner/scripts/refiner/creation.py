"""Business analysis and first implementation, gated by the real refinement pipeline."""
from __future__ import annotations

import copy
from datetime import datetime, timezone
import difflib
import json
from pathlib import Path
import re
import shutil
import time

from .agent import Agent
from .common import Cancelled, RefinerError, digest, manifest, read_json, snapshot, write_json
from .pipeline import apply_changes, run as refine
from .profiles import load_config
from .schemas import PLAN, REVIEW
from .standards import AUTHORING, discover_project_standards, lint
from .stages import StageManifest, CREATE_EVENTS

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
        "goal": STRING, "requirements": PLAN["properties"]["requirements"],
        "workflow": {"type": "array", "minItems": 1, "items": {
            "type": "object", "additionalProperties": False,
            "required": ["id", "action", "inputs", "outputs", "failure_behavior", "requirement_ids"],
            "properties": {"id": STRING, "action": STRING, "inputs": STRING, "outputs": STRING,
                           "failure_behavior": STRING, "requirement_ids": IDS}}},
        "scenarios": {"type": "array", "minItems": 3, "maxItems": 3, "items": {
            "type": "object", "additionalProperties": False,
            "required": ["kind", "applicable", "situation", "expected_behavior", "basis", "requirement_ids"],
            "properties": {"kind": {"enum": ["normal", "boundary", "error"]},
                           "applicable": {"type": "boolean"}, "situation": STRING,
                           "expected_behavior": STRING, "basis": STRING,
                           "requirement_ids": STRINGS}}},
        "assumptions": STRINGS, "external_dependencies": STRINGS, "blocking_questions": STRINGS,
    },
}
ANALYSIS_REVIEW = {
    **REVIEW, "required": ["approved", "issues", "requirement_assessments"],
    "properties": {**REVIEW["properties"], "requirement_assessments": {
        "type": "array", "minItems": 1, "items": {
            "type": "object", "additionalProperties": False,
            "required": ["requirement_id", "grounded", "basis"],
            "properties": {"requirement_id": STRING, "grounded": {"type": "boolean"}, "basis": STRING}}}},
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
    if {s["kind"] for s in analysis["scenarios"]} != {"normal", "boundary", "error"}:
        raise RefinerError("Analyze normal, boundary and error categories exactly once")
    if not next(s for s in analysis["scenarios"] if s["kind"] == "normal")["applicable"]:
        raise RefinerError("Normal business behavior must have an executable scenario")
    for item in analysis["workflow"] + analysis["scenarios"]:
        if not set(item["requirement_ids"]) <= set(ids):
            raise RefinerError("Workflow or scenario references an unknown business requirement")
    if any(s["applicable"] and not s["requirement_ids"] for s in analysis["scenarios"]):
        raise RefinerError("Applicable scenarios must map to business requirements")
    required = {r["id"] for r in analysis["requirements"] if r["required"]}
    covered = {rid for s in analysis["scenarios"] if s["applicable"] for rid in s["requirement_ids"]}
    if not required <= covered:
        raise RefinerError("Business scenario intents omit required behaviors: " + ", ".join(sorted(required - covered)))


def validate_analysis_review(review, analysis):
    import jsonschema
    jsonschema.validate(review, ANALYSIS_REVIEW)
    rows = review["requirement_assessments"]
    expected = {r["id"] for r in analysis["requirements"]}
    if len(rows) != len(expected) or {r["requirement_id"] for r in rows} != expected:
        raise RefinerError("Analysis review must assess every requirement exactly once")
    if review["approved"] and not all(r["grounded"] for r in rows):
        raise RefinerError("Analysis approval contradicts requirement grounding")


def analysis_markdown(analysis):
    lines = ["# Business analysis", "", analysis["goal"], "", "## Requirements", ""]
    lines += [f"- {r['id']}: {r['behavior']} (source: {r['source']}; required={r['required']})"
              for r in analysis["requirements"]]
    lines += ["", "## Workflow", ""]
    for step in analysis["workflow"]:
        lines += [f"- {step['id']}: {step['action']}", f"  Input: {step['inputs']}; output: {step['outputs']}",
                  f"  Failure: {step['failure_behavior']}; requirements: {', '.join(step['requirement_ids'])}"]
    lines += ["", "## Scenario intents", ""]
    for scenario in analysis["scenarios"]:
        lines += [f"- {scenario['kind']} (applicable={scenario['applicable']}): {scenario['situation']}",
                  f"  Expected: {scenario['expected_behavior']}; basis: {scenario['basis']}"]
    for title, key in [("Assumptions", "assumptions"), ("External dependencies", "external_dependencies"),
                       ("Blocking questions", "blocking_questions")]:
        lines += ["", "## " + title, ""] + ["- " + x for x in analysis[key]]
    return "\n".join(lines) + "\n"


def run(args):
    resume = Path(args.resume_from).resolve() if getattr(args,'resume_from',None) else None
    if not (args.description or args.spec or resume):
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
    deadline = started + args.max_seconds
    state = {"status": "analyzing", "mode": "create", "workspace": str(work), "questions": []}
    agent = Agent(work, args.agent_command, args.timeout, config=config)
    agent.max_calls, agent.deadline = args.max_agent_calls, deadline
    inputs = {"business_description": args.description or "", "requested_name": args.name,
              "requirements": [{"source": str(Path(p).resolve()), "content": Path(p).read_text()} for p in args.spec],
              "standards": [AUTHORING] + [{"source": str(Path(p).resolve()), "content": Path(p).read_text()} for p in standard_paths]}
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
    write_json(work / "inputs.json", inputs)
    (work / 'inputs').mkdir()
    request_file = work / 'inputs/business-request.md'
    request_file.write_text(inputs['business_description'] + '\n', encoding='utf-8')
    spec_paths = []
    for index, requirement in enumerate(inputs['requirements']):
        path = work / 'inputs' / f'requirement-{index+1}.md'
        path.write_text(requirement['content'], encoding='utf-8')
        spec_paths.append(str(path))
    standard_paths = []
    for index, standard in enumerate(inputs['standards'][1:]):
        path = work / 'inputs' / f'standard-{index+1}.md'
        path.write_text(standard['content'], encoding='utf-8')
        standard_paths.append(str(path))
    stages = StageManifest(work, "create")
    stages.complete("inputs")
    input_hash = digest(inputs)
    business_sources = ([{'id': 'business_description', 'content': inputs['business_description']}] if inputs['business_description'] else []) + [
        {'id': f'spec-{index+1}', 'source': row['source'], 'content': row['content']}
        for index, row in enumerate(inputs['requirements'])]
    refinement = None
    analysis = resumed_analysis
    locked = None

    def event(status, **extra):
        if status in CREATE_EVENTS:
            stages.activate(CREATE_EVENTS[status])
        state.update(status=status, **extra)
        write_json(work / 'state.json', state)
        print(json.dumps({'status': status, **extra}, ensure_ascii=False), flush=True)

    def integrity():
        if digest(read_json(work / 'inputs.json')) != input_hash or request_file.read_text() != inputs['business_description'] + '\n':
            raise RefinerError('Frozen business inputs changed')
        if locked and digest({'inputs': inputs, 'analysis': read_json(work / 'analysis.json')}) != locked:
            raise RefinerError('Frozen business contract changed')
        if time.monotonic() >= deadline:
            raise RefinerError('Creation wall-clock budget exhausted')

    event('resuming' if resume else 'analyzing')
    try:
        feedback = []
        for attempt in range(0 if resume else 2):
            analysis = agent.ask('analyze',
                'Analyze the supplied business request into a scoped executable Agent Skill. Choose a concise kebab-case name '
                'unless requested_name is supplied. Extract all business requirements with stable IDs and concrete source '
                'citations into business_sources. Every requirement.source must begin with its source ID plus a colon '
                '(for example spec-1: followed by the supporting passage). Include only business requirements from '
                'these sources. Authoring standards, trigger description quality, script organization and controller '
                'integrity remain in standards review; do not add them as business test requirements unless the user '
                'explicitly requested that as an externally observable business capability. Analyze workflow inputs, outputs, order, decisions and failure '
                'behavior. Assess normal, boundary and error scenario categories exactly once each. Map all required '
                'behaviors to applicable scenario intents. These are scenario intents, not frozen tests or implementation. '
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
                    'Check every requested behavior is retained and each derived requirement is grounded; cite the '
                    'specific request or explicit reasonable implementation assumption for every assessment. Reject '
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
        (work / 'business-analysis.md').write_text(analysis_markdown(analysis))
        locked = digest({'inputs': inputs, 'analysis': analysis})
        write_json(work / 'business.lock.json', {'sha256': locked, 'input_sha256': input_hash})
        integrity()
        event('reusing_draft' if resumed_draft else 'creating', skill_name=analysis['name'])
        feedback = []
        previous_draft = None
        if resumed_draft:
            draft_root = work/'drafts/reused'/analysis['name']
            snapshot(resumed_draft,draft_root)
        for attempt in range(0 if resumed_draft else 2):
            draft = agent.ask('draft',
                DRAFT_INSTRUCTION,
                {'inputs': inputs, 'analysis': analysis, 'validation_feedback': feedback,
                 **({'previous_draft': previous_draft} if previous_draft else {})}, DRAFT)
            previous_draft = draft
            draft, mapping = normalize_draft(draft, analysis['name'])
            draft_root = work / 'drafts' / str(attempt+1) / analysis['name']
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
        # Use the saved original requirements, not an implementation-derived spec.
        child.spec = ([str(request_file)] if inputs['business_description'] else []) + spec_paths
        child.standard = standard_paths
        child.max_seconds, child.max_agent_calls = max(1, int(deadline - time.monotonic())), remaining
        child.creation_context = {'contract': analysis['requirements'], 'business_analysis': analysis,
                                  'execution_helpers': PYTHON_OBSERVER_GUIDE,
                                  'required_case_kinds': [s['kind'] for s in analysis['scenarios'] if s['applicable']]}
        if resumed_suite:
            prior_context = read_json(resumed_suite/'inputs.json')
            child.spec = [row['source'] for row in prior_context['requirements']]
            child.standard = [row['source'] for row in prior_context['standards'] if isinstance(row,dict)]
            child.reuse_plan = str(resumed_suite)
        if max(len(child.creation_context['required_case_kinds']), 2) > min(args.max_cases, args.acceptance_cases):
            raise RefinerError('Case limits cannot cover all applicable scenario categories')
        event('refining', refinement_workspace=child.output)
        refinement = refine(child)
        integrity()
        if manifest(draft_root) != initial_manifest:
            raise RefinerError('Original generated draft changed during refinement')
        state['refinement_status'] = refinement['status']
        if refinement['status'] not in {'passed', 'unchanged'}:
            event(refinement['status'], error=refinement.get('error'), questions=refinement.get('gaps', []))
            return state
        final = work / 'final' / analysis['name']
        snapshot(refinement['final_skill'], final)
        # The repair diff may be empty when the first implementation already passes.
        (work / 'final/changes.patch').write_text((work / 'refinement/final/changes.patch').read_text())
        full_patch = []
        for path in sorted(manifest(final)):
            full_patch.extend(difflib.unified_diff([], (final / path).read_text().splitlines(True),
                                                  '/dev/null', 'created/' + path))
        (work / 'final/creation.patch').write_text(''.join(full_patch))
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
            (work / 'business-analysis.md').write_text(analysis_markdown(analysis))
        state.update(agent_calls=agent.calls + (refinement or {}).get('agent_calls', 0),
                     creation_agent_calls=agent.calls, target_evaluations=(refinement or {}).get('target_evaluations', 0),
                     elapsed_seconds=round(time.monotonic() - started, 2))
        write_json(work / 'state.json', state)
        lines = ['# Business Skill creation', '', 'Status: **' + state['status'] + '**', '',
                 'Business analysis: [business-analysis.md](business-analysis.md)',
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
        scenarios = ['# Executable scenario inventory', '']
        for phase, filename in [('Development', 'test-plan.json'), ('Independent acceptance', 'acceptance-plan.json')]:
            path = work / 'refinement' / filename
            if path.is_file():
                scenarios += ['## ' + phase, '']
                for case in read_json(path)['cases']:
                    scenarios += [f"### {case['id']} ({case['kind']})", '',
                                  'Requirements: ' + ', '.join(case['requirement_ids']), case['oracle_reason'],
                                  '```json', json.dumps({'prompt': case['prompt'], 'inputs': case['files'],
                                                       'checks': case['checks']}, ensure_ascii=False, indent=2), '```', '']
        (work / 'test-scenarios.md').write_text('\n'.join(scenarios) + '\n')
        if state.get('status') == 'created':
            stages.activate('verification')
            try:
                from .gate import verify_run
                verification = verify_run(work)
                write_json(work / 'verification.json', verification)
                state['verification_status'] = verification['status']
                if destination:
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
