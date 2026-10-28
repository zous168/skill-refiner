"""Generate the frozen design and test checklist artifact."""
from __future__ import annotations

import json
from pathlib import Path
import re

from .common import read_json


def _md(value):
    return str(value).replace("|", "\\|").replace("\n", " ").strip()


def _format_check_value(value):
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def _format_check_line(check):
    parts = [f"assertion={check.get('id', 'missing-id')}", check["type"]]
    for key in ("path", "pointer", "control_command", "control_tool_name"):
        if key in check:
            parts.append(f"{key}={check[key]!r}")
    if "expected_exit_code" in check:
        parts.append(f"expected_exit_code={check['expected_exit_code']}")
    if "literal_command" in check:
        parts.append(f"literal_command={check['literal_command']}")
    parts.append(f"expected={_format_check_value(check.get('value'))}")
    return "; ".join(parts)


def _requirement_detail(requirement_ids, requirements):
    by_id = {row["id"]: row for row in requirements}
    lines = []
    for rid in requirement_ids:
        row = by_id.get(rid)
        if row:
            lines.append(f"  - **{rid}:** {_md(row['behavior'])} (source: {_md(row.get('source', ''))})")
        else:
            lines.append(f"  - **{rid}:** unknown requirement")
    return lines


def _render_case_block(case, requirements):
    lines = [f"- [ ] **{case['id']} ({case['kind']})**",
             f"  - Scenario: {_md(case.get('scenario', ''))}",
             f"  - Expected outcome: {_md(case.get('expected_outcome', ''))}",
             "  - Linked requirements:"]
    lines.extend(_requirement_detail(case["requirement_ids"], requirements))
    lines.append("  - Machine checks:")
    for check in case.get("checks", []):
        lines.append(f"    - {_md(_format_check_line(check))}")
    audit = case.get("asset_audit", {})
    lines += ["  - Asset audit:",
              f"    - Status: {_md(audit.get('status', 'missing'))}",
              f"    - Required: {_md(', '.join(audit.get('required_paths', [])) or 'none')}",
              f"    - Available: {_md(', '.join(audit.get('available_paths', [])) or 'none')}",
              f"    - Source refs: {_md(', '.join(audit.get('source_refs', [])) or 'none')}",
              f"    - Basis: {_md(audit.get('basis', ''))}"]
    lines.append("  - Positive controls:")
    for control in case.get("positive_controls", []):
        lines.append(f"    - {_md(control.get('id', 'missing-id'))}: {_md(control.get('response', ''))}")
    lines.append("  - Negative controls:")
    for control in case.get("negative_controls", []):
        lines.append(f"    - {_md(control.get('id', 'missing-id'))} targets assertion "
                     f"{_md(control.get('assertion_id', 'missing-id'))}: {_md(control.get('response', ''))}")
        lines.append(f"      - Rationale: {_md(control.get('rationale', ''))}")
    lines.append(f"  - Oracle rationale: {_md(case.get('oracle_reason', ''))}")
    return lines


def _source_overview(source):
    if not source:
        return [], []
    root = Path(source)
    if not root.is_dir():
        return [], []
    description, headings, files = [], [], []
    skill_md = root / "SKILL.md"
    if skill_md.is_file():
        text = skill_md.read_text(encoding="utf-8")
        text = re.sub(r"\A---\s*\n.*?\n---\s*\n", "", text, count=1, flags=re.S)
        description = [line.strip() for line in text.splitlines() if line.strip() and not line.lstrip().startswith("#")][:2]
        headings = [line.lstrip("# ").strip() for line in text.splitlines() if line.startswith("## ")][:20]
    for base in ("references", "scripts"):
        folder = root / base
        if folder.is_dir():
            files.extend(p.relative_to(root).as_posix() for p in sorted(folder.rglob("*"))
                         if p.is_file() and "__pycache__" not in p.parts
                         and ".pyc" not in p.suffixes and ".before-" not in p.name)
    return description, [("section", item) for item in headings] + [("file", item) for item in files[:80]]


def _execution_phases(analysis):
    if analysis:
        first = (
            "Analyze and review the business request",
            "Turn the user request into grounded, testable business requirements.",
            "Map the workflow, decisions, inputs, outputs and applicable normal/boundary/error scenarios; obtain an independent analysis review.",
            "Original request, cited sources and discovered standards.",
            "analysis.json, analysis-review.json, business-analysis.md and business.lock.json.",
            "Every required behavior has a source and workflow mapping; review approves; no material blocking question remains.",
            "A material unanswered question ends this run at needs_clarification; do not invent a business rule.",
        )
        second = (
            "Build and statically check the first Skill draft",
            "Create the first complete implementation from the reviewed contract.",
            "Write the Skill files, validate paths and metadata, and run static authoring checks.",
            "Approved business contract and original requirements.",
            "drafts/ and draft-manifest.json.",
            "All required files exist, are traceable to requirements, and static checks pass.",
            "Preserve the draft and report the concrete finding; do not enter refinement with an invalid draft.",
        )
        planning = (
            "Freeze development and independent acceptance plans",
            "Convert each required behavior into complete, executable cases.",
            "Read the actual schema first; create the full matrix, positive and targeted negative controls, and asset audits.",
            "Reviewed requirements, current Skill source, actual plan schema and standards.",
            "test-plan.json, acceptance-plan.json, plan-controls and work-plan.md.",
            "Every requirement maps to scenarios, expected correct and incorrect results, assertions, controls and audited assets; the frozen inputs are hashed.",
            "Keep target evaluation not_started if coverage or schema completeness is unclear.",
        )
    else:
        first = (
            "Inspect the request and map the existing Skill",
            "Understand the requested behavior and the Skill's real implementation.",
            "Read SKILL.md, linked references, scripts, configuration, standards and available execution evidence; map requirements to source locations.",
            "User request, target Skill, project standards and available run evidence.",
            "Requirement/source map, assumptions and identified dependencies.",
            "Each requirement is grounded and has an observable expected outcome; material unknowns are separated from safe assumptions.",
            "Ask only for a material business decision or access choice that evidence cannot resolve.",
        )
        second = (
            "Freeze the complete scenario matrix",
            "Define development and independent acceptance cases before changing the Skill.",
            "Read the actual schema first; map each requirement to normal/boundary/error scenarios, correct and incorrect outcomes, semantic assertions, controls and asset audits.",
            "Requirement/source map, actual plan schema, Skill snapshot and standards.",
            "test-plan.json, acceptance-plan.json, plan-controls and work-plan.md.",
            "All required behaviors are covered, every case is schema-complete, controls are targeted, assets are audited and inputs are hash-frozen.",
            "Do not run the target Skill while required coverage, schema fields or oracle evidence are missing.",
        )
        planning = (
            "Self-check plans and complete independent review",
            "Prove that the frozen cases distinguish intended behavior from plausible mistakes.",
            "Run every positive and negative control against all assertions; review each suite independently; repair the plan and rerun the complete control set after any revision.",
            "Frozen development and acceptance matrices, assertions and control responses.",
            "plan-controls results, plan-reviews and structured failure diagnostics.",
            "Every positive passes all required assertions, every negative triggers its designated semantic assertion, and both reviews pass.",
            "A failed control or review keeps the plan gate closed; do not label it as a Skill failure.",
        )
    phases = [first, second, planning]
    phases.extend([
        (
            "Pass the plan gate before target evaluation",
            "Aggregate schema, control, asset and independent-review evidence.",
            "Recompute required checks from the frozen artifacts and record the separate phase result.",
            "Frozen plans, control results and review reports.",
            "plan-gate result in state.json and stages.json.",
            "Only an explicit passed plan gate allows target evaluation to start.",
            "On plan_not_passed, leave skill_evaluation not_started and report the exact failed assertion and evidence.",
        ),
        (
            "Run the baseline",
            "Measure the original Skill against the complete development suite.",
            "Execute every frozen development case on every configured engine and preserve outputs and judgments.",
            "Passing plan gate, original Skill snapshot and development plan.",
            "Baseline summaries, per-case results and raw engine evidence.",
            "Every configured engine has an observed result; classify Skill defects separately from test gaps and environment errors.",
            "An unavailable engine or execution error is an error state, not a pass; preserve evidence and stop dependent work.",
        ),
        (
            "Repair and run full regression",
            "Improve only behavior tied to an evidenced failed case or material standards finding.",
            "Make a bounded repair, then rerun the entire development suite, including previously passing cases, for each repair round.",
            "Baseline failures, frozen requirements and cited findings.",
            "Candidate changes, repair rationale, full regression results and per-round evidence.",
            "The cited defect is corrected and no previously passing behavior regresses.",
            "Stop at the configured round/call/evaluation budget or repeated lack of improvement; never reset budgets or rerun completed evidence without a changed contract.",
        ),
        (
            "Run independent acceptance",
            "Check the qualified candidate against the separately frozen acceptance suite.",
            "Run every held-out acceptance case on every configured engine and preserve the raw evidence.",
            "Candidate with passing full regression and independent acceptance plan.",
            "Acceptance summary, per-case judgments and immutable attempt evidence.",
            "Every configured engine passes every required independent case.",
            "If acceptance fails, expose only failed acceptance evidence to repair, then repeat full regression and the full acceptance suite within the same run budget.",
        ),
        (
            "Verify and deliver",
            "Bind the delivered candidate to its frozen plans and observed test evidence.",
            "Recompute final hashes, stage contracts, regression, acceptance and delivery artifacts.",
            "Qualified candidate and complete stage evidence.",
            "verification.json, final report, candidate and diff.",
            "Only machine status=verified permits a completion claim.",
            "If verification fails or evidence is missing, report the exact gate and keep the run incomplete.",
        ),
    ])
    return phases


def _execution_phase_markdown(analysis):
    lines = [
        "## Execution phases and step conclusions", "",
        "This is the ordered execution map. Each step must end with its stated conclusion and evidence before dependent work advances.", "",
        "### Autonomous progression", "",
        "Unless the user explicitly asks for plan-only, showing this plan is a visibility checkpoint, not an approval wait. Continue the same run through implementation, tests, repair and verification. Pause only for a material unanswered business decision, a required permission or local action, an explicit user stop, a failed gate, an environment blocker, or an exhausted configured budget.", "",
        "At each meaningful transition, report the step number and name, live status, one-sentence conclusion, evidence path and next step. Read live status from structured events, state.json and stages.json; never infer completion from a static plan, elapsed time or heartbeat count.", "",
    ]
    for index, (title, goal, actions, inputs, outputs, conclusion, stop) in enumerate(_execution_phases(analysis), 1):
        lines += [
            f"### Step {index} — {title}", "",
            f"- Goal: {_md(goal)}",
            f"- Actions: {_md(actions)}",
            f"- Inputs: {_md(inputs)}",
            f"- Outputs and evidence: {_md(outputs)}",
            f"- Conclusion required to advance: {_md(conclusion)}",
            f"- If the conclusion is not met: {_md(stop)}", "",
        ]
    lines += [
        "Completed stages and their evidence are reused on resume when hashes and contracts still match. Resume the same run after interruption; do not repeat completed stages. If a frozen input or controller contract changes, preserve the old evidence and start a new run only for the changed contract.",
        "",
    ]
    return lines


def write_work_plan(work, *, source=None, plan=None, acceptance=None, analysis=None, limits=None):
    """Write the visible, frozen design and test checklist before test execution."""
    work = Path(work)
    if plan is None and (work / "test-plan.json").is_file():
        plan = read_json(work / "test-plan.json")
    if acceptance is None and (work / "acceptance-plan.json").is_file():
        acceptance = read_json(work / "acceptance-plan.json")
    analysis = analysis or (read_json(work / "analysis.json") if (work / "analysis.json").is_file() else None)
    if plan is None and (work / "refinement" / "test-plan.json").is_file():
        plan = read_json(work / "refinement" / "test-plan.json")
    if acceptance is None and (work / "refinement" / "acceptance-plan.json").is_file():
        acceptance = read_json(work / "refinement" / "acceptance-plan.json")
    lines = ["# Skill design and test plan", ""]
    if analysis:
        lines += ["## Goal", "", analysis.get("goal", "Not recorded."), "", "## Proposed workflow", ""]
        for step in analysis.get("workflow", []):
            lines += [f"- **{_md(step['id'])}:** {_md(step['action'])}",
                      f"  - Input: {_md(step['inputs'])}; output: {_md(step['outputs'])}",
                      f"  - Completion check: {_md(step['completion_check'])}",
                      f"  - Failure handling: {_md(step['failure_behavior'])}",
                      f"  - Requirements: {', '.join(step['requirement_ids'])}"]
        _, outline = _source_overview(source)
        if outline:
            lines += ["", "## Implementation structure", ""]
            lines += [f"- `{value}`" if label == "file" else f"- SKILL.md section: {value}"
                      for label, value in outline]
    else:
        description, outline = _source_overview(source)
        lines += ["## Current Skill design", ""]
        lines += description or ["Design summary is derived from the current SKILL.md and source inventory."]
        if outline:
            lines += ["", "### Structure", ""]
            lines += [f"- {label}: `{value}`" if label == "file" else f"- SKILL.md section: {value}"
                      for label, value in outline]
        if plan and plan.get("assumptions"):
            lines += ["", "### Planning assumptions", ""] + [f"- {_md(x)}" for x in plan["assumptions"]]
    requirements = (analysis or {}).get("requirements") or (plan or {}).get("requirements") or []
    lines += ["", "## Requirements", ""]
    if requirements:
        lines += ["| ID | Required behavior | Source | Required |", "|---|---|---|---|"]
        for row in requirements:
            lines.append(f"| {_md(row['id'])} | {_md(row['behavior'])} | {_md(row.get('source', ''))} | "
                         f"{'yes' if row.get('required', True) else 'no'} |")
    else:
        lines.append("Requirements have not been frozen yet.")
    if analysis:
        lines += ["", "## Scenario coverage intent", ""]
        for item in analysis.get("scenarios", []):
            applicability = "included" if item["applicable"] else "excluded"
            lines.append(f"- **{item['kind']} — {applicability}:** {_md(item['situation'])} "
                         f"Expected: {_md(item['expected_behavior'])} "
                         f"(requirements: {', '.join(item['requirement_ids'])})")
    if analysis and analysis.get("assumptions"):
        lines += ["", "## Assumptions", ""] + [f"- {_md(x)}" for x in analysis["assumptions"]]
    if analysis and analysis.get("external_dependencies"):
        lines += ["", "## External dependencies", ""] + [f"- {_md(x)}" for x in analysis["external_dependencies"]]
    lines += [""] + _execution_phase_markdown(analysis)
    lines += ["", "## Acceptance criteria", "",
              "- Every required behavior is covered by the frozen, independently reviewed cases.",
              "- Each configured executor passes the full independent acceptance suite.",
              "- The delivered candidate matches the verified source and its evidence."]
    req_rows = list(requirements)
    if acceptance:
        acceptance_reqs = acceptance.get("requirements") or req_rows
        lines += ["", "### Frozen acceptance cases", ""]
        for case in acceptance.get("cases", []):
            lines.extend(_render_case_block(case, acceptance_reqs))
            lines.append("")
    lines += ["", "## Test checklist", ""]
    for label, suite_plan in (("Development / baseline and regression", plan),
                              ("Independent acceptance", acceptance)):
        lines += [f"### {label}", ""]
        if suite_plan:
            suite_reqs = suite_plan.get("requirements") or req_rows
            for case in suite_plan.get("cases", []):
                lines.extend(_render_case_block(case, suite_reqs))
                lines.append("")
        else:
            lines.append("- Waiting for the reviewed, frozen plan.")
            lines.append("")
    lines += ["## Execution limits", ""]
    limits = limits or {}
    for key, label in (("max_rounds", "Repair rounds"), ("max_seconds", "Wall-clock seconds"),
                       ("max_agent_calls", "Agent calls"), ("max_evaluations", "Case-executor evaluations"),
                       ("max_cases", "Development cases"), ("acceptance_cases", "Acceptance cases")):
        if key in limits:
            lines.append(f"- {label}: at most {limits[key]}")
    (work / "work-plan.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _suite_case_markdown(phase, suite):
    lines = [f"## {phase}", ""]
    req_by_id = {row["id"]: row for row in suite.get("requirements", [])}
    for case in suite["cases"]:
        req_lines = []
        for rid in case["requirement_ids"]:
            row = req_by_id.get(rid, {})
            req_lines.append(
                f"- {rid}: {row.get('behavior', 'unknown')} (source: {row.get('source', '')})"
            )
        check_lines = ["- " + _format_check_line(check) for check in case["checks"]]
        audit = case.get("asset_audit", {})
        positive = [f"- {item.get('id')}: {item.get('response', '')}"
                    for item in case.get("positive_controls", [])]
        negative = [f"- {item.get('id')} targets {item.get('assertion_id')}: {item.get('response', '')}; "
                    f"rationale: {item.get('rationale', '')}"
                    for item in case.get("negative_controls", [])]
        lines += [
            f"### {case['id']} ({case['kind']})", "",
            f"Scenario: {case.get('scenario', '')}",
            f"Expected outcome: {case.get('expected_outcome', '')}",
            "Linked requirements:", *req_lines,
            "Machine checks:", *check_lines,
            "Asset audit:",
            f"- Status: {audit.get('status', 'missing')}",
            f"- Required paths: {', '.join(audit.get('required_paths', [])) or 'none'}",
            f"- Available paths: {', '.join(audit.get('available_paths', [])) or 'none'}",
            f"- Source refs: {', '.join(audit.get('source_refs', [])) or 'none'}",
            f"- Basis: {audit.get('basis', '')}",
            "Positive controls:", *positive,
            "Negative controls:", *negative,
            f"Oracle rationale: {case['oracle_reason']}",
            "```json",
            json.dumps({"prompt": case["prompt"], "inputs": case["files"]}, ensure_ascii=False, indent=2),
            "```", "",
        ]
    return lines


def write_test_scenarios(work, *, plan=None, acceptance=None):
    """Write the frozen development and acceptance case inventory."""
    work = Path(work)
    if plan is None:
        for candidate in (work / "test-plan.json", work / "refinement" / "test-plan.json"):
            if candidate.is_file():
                plan = read_json(candidate)
                break
    if acceptance is None:
        for candidate in (work / "acceptance-plan.json", work / "refinement" / "acceptance-plan.json"):
            if candidate.is_file():
                acceptance = read_json(candidate)
                break
    lines = ["# Executable scenario inventory", ""]
    if plan:
        lines.extend(_suite_case_markdown("Development", plan))
    if acceptance:
        lines.extend(_suite_case_markdown("Independent acceptance", acceptance))
    (work / "test-scenarios.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


