"""Generate the frozen design and test checklist artifact."""
from __future__ import annotations

from pathlib import Path
import re

from .common import read_json


def _md(value):
    return str(value).replace("|", "\\|").replace("\n", " ").strip()


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
    if acceptance:
        lines += ["", "### Frozen acceptance cases", ""]
        for case in acceptance.get("cases", []):
            lines.append(f"- **{case['id']} ({case['kind']})** — requirements: "
                         f"{', '.join(case['requirement_ids'])}; checks: {len(case['checks'])}; "
                         f"oracle: {_md(case['oracle_reason'])}")
    lines += ["", "## Test checklist", ""]
    for label, suite_plan in (("Development / baseline and regression", plan),
                              ("Independent acceptance", acceptance)):
        lines += [f"### {label}", ""]
        if suite_plan:
            for case in suite_plan.get("cases", []):
                lines.append(f"- [ ] **{case['id']} ({case['kind']})**; requirements: "
                             f"{', '.join(case['requirement_ids'])}; checks: {len(case['checks'])}; "
                             f"{_md(case['oracle_reason'])}")
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


