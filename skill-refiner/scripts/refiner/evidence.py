"""Bounded excerpts of actual failing executions, never of the held-out suite."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path, PurePosixPath
import zipfile
import tempfile


def workspace_paths(root):
    root = Path(root).resolve()
    return {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()}


def archive_diagnostics(root, before, max_bytes=8_000_000):
    """Retain newly produced text diagnostics before upstream removes its workspace."""
    root = Path(root).resolve()
    # Upstream requires a workspace-relative path. ZIP files are excluded from
    # diagnostic collection, so this temporary archive cannot include itself.
    handle = tempfile.NamedTemporaryFile(prefix="refiner-diagnostics-", suffix=".zip", dir=root, delete=False)
    output = Path(handle.name)
    handle.close()
    entries, omissions, used = [], [], 0
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        paths = sorted(root.rglob("*"))
        for path in paths:
            rel = path.relative_to(root)
            if (rel.as_posix() in before or path.is_symlink() or not path.is_file()
                    or not path.resolve().is_relative_to(root)
                    or any(part in {"__pycache__", ".git", ".venv", "node_modules"} for part in rel.parts)
                    or path.suffix not in {".json", ".jsonl", ".log", ".md", ".txt", ".patch", ".yaml", ".yml"}):
                continue
            size = path.stat().st_size
            if size > 1_000_000 or used + size > max_bytes or len(entries) >= 200:
                omissions.append({"path": rel.as_posix(), "bytes": size, "reason": "diagnostic archive budget"})
                continue
            data = path.read_bytes()
            try:
                data.decode("utf-8")
            except UnicodeError:
                continue
            archive.writestr(rel.as_posix(), data)
            entries.append({"path": rel.as_posix(), "bytes": len(data),
                            "sha256": hashlib.sha256(data).hexdigest()})
            used += len(data)
        archive.writestr("refiner-diagnostic-index.json", json.dumps({"files": entries, "omissions": omissions}))
    return {"name": "refiner-diagnostics.zip", "path": output.relative_to(root).as_posix()}


def failure_bundle(upstream, case_id, max_bytes=40_000):
    """Only inspect this development execution's archived outputs; do not follow paths in logs."""
    root = Path(upstream).resolve()
    records, omissions, seen = [], [], set()
    used = 0

    def add(source, data):
        nonlocal used
        sha = hashlib.sha256(data).hexdigest()
        if sha in seen:
            return
        seen.add(sha)
        try:
            text = data.decode("utf-8")
        except UnicodeError:
            return
        # Planning requests repeat the entire source inventory. The corresponding
        # response/review and the validation feedback carry the diagnostic signal.
        if source.endswith("request.json"):
            try:
                request = json.loads(text)
                payload = request.get("input", {})
                text = json.dumps({"role": request.get("role"),
                    "validation_feedback": payload.get("validation_feedback"),
                    "contract_error": payload.get("contract_error"),
                    "plan": payload.get("plan")}, ensure_ascii=False)
            except (ValueError, AttributeError):
                pass
        available = min(12_000, max_bytes - used)
        if available <= 0:
            omissions.append({"source": source, "bytes": len(data), "sha256": sha, "reason": "bundle budget"})
            return
        encoded = text.encode("utf-8")
        # Keep both ends: headers identify the stage; tails contain the failure.
        excerpt = text if len(encoded) <= available else (
            encoded[:available // 2].decode("utf-8", errors="ignore") + "\n[excerpt omitted]\n" +
            encoded[-available // 2:].decode("utf-8", errors="ignore"))
        records.append({"source": source, "bytes": len(data), "sha256": sha,
                        "excerpt": excerpt, "truncated": len(encoded) > available})
        used += len(excerpt.encode("utf-8"))

    # Never accept a case ID as an arbitrary filesystem path.
    if Path(case_id).name != case_id or case_id in {".", ".."}:
        return {"records": [], "omissions": [{"reason": "unsafe case ID"}]}
    directories = sorted(root.glob("iteration-*/" + case_id + "/with_skill"))
    for directory in directories:
        paths = sorted(p for p in directory.rglob("*") if p.is_file() and not p.is_symlink()
                       and p.resolve().is_relative_to(root))
        # Explicit task-workspace archives are evidence, not a source of verdicts.
        for path in paths:
            if path.suffix != ".zip":
                continue
            try:
                with zipfile.ZipFile(path) as archive:
                    selected = []
                    for item in archive.infolist():
                        parts = PurePosixPath(item.filename).parts
                        if (not parts or item.is_dir() or ".." in parts or item.filename.startswith("/")
                                or item.file_size > 1_000_000 or item.compress_size > 16_000_000):
                            continue
                        # Only diagnostics from workflows actually run inside this
                        # case, never installed tests/oracles or arbitrary fixture code.
                        name = parts[-1]
                        role_call = "agent-calls" in parts and name in {"response.json", "request.json"}
                        summary = name in {"state.json", "standards.json", "report.md", "summary.json"}
                        if role_call or summary:
                            selected.append(item)
                    def priority(item):
                        name = item.filename
                        return (0 if name.endswith("state.json") else
                                1 if "plan-review" in name and name.endswith("response.json") else
                                2 if "-plan/response.json" in name else
                                3 if name.endswith("response.json") else 4, name)
                    for item in sorted(selected, key=priority)[:80]:
                        add(str(path) + "!" + item.filename, archive.read(item))
            except (OSError, zipfile.BadZipFile, RuntimeError) as exc:
                omissions.append({"source": str(path), "reason": str(exc)})
        for path in paths:
            if path.suffix not in {".json", ".jsonl", ".txt", ".md"} or path.stat().st_size > 4_000_000:
                continue
            add(str(path), path.read_bytes())
    return {"scope": "Artifacts of this executed case only; held-out controller cases are not loaded",
            "records": records, "omissions": omissions}


def repair_evidence(failures, max_bytes=100_000):
    """Share the budget across failed engine/case pairs and preserve their identity."""
    share = max_bytes // max(1, len(failures))
    result = []
    for row in failures:
        upstream = row.get("evidence_directory")
        bundle = failure_bundle(upstream, row.get("case_id", row["id"]), share) if upstream else {
            "records": [], "omissions": [{"reason": "No archived execution directory available"}]}
        result.append({"evidence_id": row["id"], **bundle})
    return result
