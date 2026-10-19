from __future__ import annotations

import ast
from pathlib import Path
import re
import yaml

from .common import files


AUTHORING = """Maintain the user's scope and public input/output contract. Make trigger descriptions
specific. Keep the entrypoint focused and put substantial conditional details in discoverable
references. Explain important decisions; avoid arbitrary mandatory headings, length padding,
or adding examples/configuration where irrelevant. Prefer narrow fixes supported by failures.
Use reusable scripts for demonstrated repeated deterministic work. Do not delete capabilities
or weaken existing tests. Normative requirements come from supplied requirements and standards;
the current implementation is evidence, not an infallible source of truth. For Skills that can
be blocked by a material business decision, local configuration, permissions, or an unavailable
dependency, include a conditional help-request path: inspect evidence first, explain the concrete
blocker, ask only for the smallest safe choice or action, and continue with fresh checks after
the answer. Use a host-native input tool when available and conversation otherwise. Never request
secrets in chat. Do not add unnecessary questions to deterministic workflows."""


def lint(root, expected_name=None):
    root = Path(root)
    findings = []

    def add(rule, path, message, severity="error"):
        findings.append({"id": f"S-{len(findings)+1:03}", "rule": rule,
                         "path": path, "message": message, "severity": severity})

    entry = root / "SKILL.md"
    if not entry.exists():
        add("format.entrypoint", "SKILL.md", "Missing SKILL.md")
        return findings
    text = entry.read_text(encoding="utf-8")
    match = re.match(r"\A---\r?\n(.*?)\r?\n---(?:\r?\n|$)", text, re.S)
    front = {}
    if not match:
        add("format.frontmatter", "SKILL.md", "Missing YAML frontmatter")
    else:
        try:
            front = yaml.safe_load(match[1])
            if not isinstance(front, dict):
                raise ValueError("Frontmatter must be a mapping")
        except (yaml.YAMLError, ValueError) as exc:
            add("format.frontmatter", "SKILL.md", str(exc))
            front = {}
    name = front.get("name")
    if not isinstance(name, str) or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", name) or len(name) > 64:
        add("format.name", "SKILL.md", "Name must be nonempty kebab-case, at most 64 characters")
    elif expected_name and name != expected_name:
        add("contract.identity", "SKILL.md", f"Preserve skill name {expected_name!r}")
    for key, limit, required in [("description", 1024, True), ("compatibility", 500, False)]:
        value = front.get(key)
        if required or value is not None:
            if not isinstance(value, str) or not value.strip() or len(value) > limit:
                add(f"format.{key}", "SKILL.md", f"{key} must be a nonempty string of at most {limit} characters")
    if "metadata" in front and (not isinstance(front["metadata"], dict) or
                               any(not isinstance(v, str) for v in front["metadata"].values())):
        add("format.metadata", "SKILL.md", "metadata must map string keys to string values")
    if len(text.splitlines()) > 500:
        add("authoring.entrypoint", "SKILL.md", "Consider moving conditional detail to references", "warning")
    for p in files(root):
        rel = str(p.relative_to(root))
        if p.suffix == ".py":
            try:
                ast.parse(p.read_text(encoding="utf-8"))
            except (SyntaxError, UnicodeError) as exc:
                add("script.syntax", rel, str(exc))
        if p.suffix == ".md":
            content = p.read_text(encoding="utf-8")
            for ref in re.findall(r"\]\(([^)#\s]+)(?:#[^)]*)?\)", content):
                if "://" not in ref and not ref.startswith(("/", "#", "mailto:")) and not (p.parent / ref).exists():
                    add("resource.reference", rel, f"Broken relative reference: {ref}")
    return findings

