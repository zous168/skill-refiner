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
    parts = [check["type"]]
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
                         if p.is_file() and "__pycache__" not in p.parts and ".pyc" not in p.suffixes)
    return description, [("section", item) for item in headings] + [("file", item) for item in files[:80]]


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
        lines += [
            f"### {case['id']} ({case['kind']})", "",
            f"Scenario: {case.get('scenario', '')}",
            f"Expected outcome: {case.get('expected_outcome', '')}",
            "Linked requirements:", *req_lines,
            "Machine checks:", *check_lines,
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


