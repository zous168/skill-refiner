#!/usr/bin/env python3
"""Build a read-only, local project/task summary for GoalExecutor Viewer."""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

TASK_ID = re.compile(r"^[A-Za-z0-9_-]{1,96}$")
MAX_GOAL_CHARS = 1200
MAX_DETAIL_ITEMS = 100
RUNTIME_FIELDS = (
    "round", "max_rounds", "stagnant_streak", "started_at", "deadline_at",
    "agent_calls", "max_agent_calls", "evaluations", "max_evaluations", "last_heartbeat",
)


def as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    return json.dumps(value, ensure_ascii=False)


def as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def short_text(value: Any, limit: int = 1200) -> str:
    return value.strip()[:limit] if isinstance(value, str) else ""


def viewer_snapshot(snapshot: dict[str, Any], task_id: str, goal: str, subtasks: list[dict[str, Any]]) -> dict[str, Any]:
    criteria = []
    for item in as_list(snapshot.get("success_criteria"))[:MAX_DETAIL_ITEMS]:
        if not isinstance(item, dict):
            continue
        criteria.append({
            "id": short_text(item.get("id"), 96),
            "criterion": short_text(item.get("criterion")),
            "status": short_text(item.get("status"), 32),
            "evidence_ref": short_text(item.get("evidence_ref")),
        })
    todo_specs = []
    for item in as_list(snapshot.get("todo_specs"))[:MAX_DETAIL_ITEMS]:
        if not isinstance(item, dict):
            continue
        todo_specs.append({
            "subtask_id": short_text(item.get("subtask_id"), 96),
            "title": short_text(item.get("title"), 500),
            "goal_refs": [short_text(ref, 96) for ref in as_list(item.get("goal_refs"))[:20] if isinstance(ref, str)],
            "done_when": short_text(item.get("done_when")),
        })
    changes = []
    for item in as_list(snapshot.get("goal_changes"))[:MAX_DETAIL_ITEMS]:
        if isinstance(item, dict):
            changes.append({
                "at": short_text(item.get("at"), 96),
                "change": short_text(item.get("change") or item.get("description")),
            })
        elif isinstance(item, str):
            changes.append({"change": short_text(item)})
    calls = []
    for item in as_list(snapshot.get("child_agent_calls"))[:MAX_DETAIL_ITEMS]:
        if not isinstance(item, dict):
            continue
        calls.append({
            "subtask_id": short_text(item.get("subtask_id"), 96),
            "title": short_text(item.get("title") or item.get("role"), 500),
            "done_when": short_text(item.get("done_when")),
        })
    artifacts = snapshot.get("artifacts_ref")
    safe_artifacts = {}
    if isinstance(artifacts, dict):
        for key, value in list(artifacts.items())[:MAX_DETAIL_ITEMS]:
            if isinstance(key, str):
                safe_artifacts[key[:96]] = short_text(value, 500)
    return {
        "task_id": task_id,
        "global_goal": goal[:MAX_GOAL_CHARS],
        "constraints": [short_text(item, 800) for item in as_list(snapshot.get("constraints"))[:MAX_DETAIL_ITEMS] if isinstance(item, str)],
        "success_criteria": criteria,
        "goal_changes": changes,
        "todo_specs": todo_specs,
        "tasks": subtasks[:MAX_DETAIL_ITEMS],
        "current_subtask": short_text(snapshot.get("current_subtask"), 500) or None,
        "done": [short_text(item, 800) for item in as_list(snapshot.get("done"))[:MAX_DETAIL_ITEMS] if isinstance(item, str)],
        "pending": [short_text(item, 800) for item in as_list(snapshot.get("pending"))[:MAX_DETAIL_ITEMS] if isinstance(item, str)],
        "status": short_text(snapshot.get("status"), 32) or "continue",
        "block_reason": short_text(snapshot.get("block_reason"), 1200) or None,
        "artifacts_ref": safe_artifacts,
        "child_agent_calls": calls,
    }


def viewer_runtime(snapshot_path: Path, task_id: str) -> dict[str, Any] | None:
    runtime_path = snapshot_path.parent.parent / "runtime" / f"{task_id}.json"
    if runtime_path.is_symlink() or not runtime_path.is_file():
        return None
    try:
        runtime = json.loads(runtime_path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(runtime, dict):
        return None
    return {key: runtime[key] for key in RUNTIME_FIELDS if key in runtime and isinstance(runtime[key], (str, int, float, bool))}


def task_summary(snapshot_path: Path) -> dict[str, Any] | None:
    if snapshot_path.is_symlink() or not snapshot_path.is_file():
        return None
    try:
        snapshot = json.loads(snapshot_path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(snapshot, dict):
        return None
    task_id = as_text(snapshot.get("task_id")) or snapshot_path.stem
    if not TASK_ID.fullmatch(task_id):
        return None
    goal = as_text(snapshot.get("global_goal"))
    if not goal:
        return None
    criteria = [item for item in as_list(snapshot.get("success_criteria"))
                if isinstance(item, dict) and item.get("status") != "superseded"]
    verified = sum(item.get("status") == "verified" for item in criteria)
    subtasks = []
    for item in as_list(snapshot.get("tasks")):
        if not isinstance(item, dict):
            continue
        item_id = as_text(item.get("id") or item.get("subtask_id"))
        if not item_id:
            continue
        subtasks.append({
            "id": item_id,
            "parentId": as_text(item.get("parentId") or item.get("parent_id")) or None,
            "title": as_text(item.get("title")),
            "status": as_text(item.get("status")) or "pending",
        })
    result = {
        "task_id": task_id,
        "global_goal": goal[:MAX_GOAL_CHARS],
        "status": as_text(snapshot.get("status")) or "continue",
        "current_subtask": as_text(snapshot.get("current_subtask")) or None,
        "pending_count": len(as_list(snapshot.get("pending"))),
        "done_count": len(as_list(snapshot.get("done"))),
        "criteria_verified": verified,
        "criteria_total": len(criteria),
        "progress_percent": round(verified / len(criteria) * 100) if criteria else 0,
        "subtasks": subtasks,
        "updated_at": datetime.fromtimestamp(snapshot_path.stat().st_mtime).astimezone().isoformat(timespec="seconds"),
    }
    result["viewer_snapshot"] = viewer_snapshot(snapshot, task_id, goal, subtasks)
    result["viewer_runtime"] = viewer_runtime(snapshot_path, task_id)
    return result


def build(sources_path: Path, output_path: Path) -> dict[str, Any]:
    sources = json.loads(sources_path.read_text(encoding="utf-8-sig"))
    if not isinstance(sources, dict) or not isinstance(sources.get("projects"), list):
        raise ValueError("project-sources.json must contain a projects array")
    projects = []
    seen_ids: set[str] = set()
    for entry in sources["projects"]:
        if not isinstance(entry, dict):
            continue
        project_id = as_text(entry.get("id"))
        name = as_text(entry.get("name"))
        raw_root = as_text(entry.get("path"))
        if not project_id or not name or not raw_root or project_id in seen_ids:
            continue
        seen_ids.add(project_id)
        root = Path(raw_root).expanduser()
        if not root.is_absolute():
            continue
        root = root.resolve(strict=False)
        snapshot_dir = root / ".tasks" / "snapshots"
        goals = []
        if snapshot_dir.is_dir():
            for path in snapshot_dir.glob("GE-*.json"):
                summary = task_summary(path)
                if summary:
                    goals.append(summary)
        goals.sort(key=lambda item: item["updated_at"], reverse=True)
        projects.append({
            "id": project_id,
            "name": name,
            "path": str(root),
            "goal_tasks": goals,
        })
    projects.sort(key=lambda item: (not any(t["status"] not in ("complete", "done") for t in item["goal_tasks"]), item["name"].casefold()))
    return {
        "version": 1,
        "updated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "host_root": str(output_path.resolve(strict=False).parent.parent),
        "projects": projects,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sources", type=Path, default=Path(".tasks/project-sources.json"))
    parser.add_argument("--output", type=Path, default=Path(".tasks/project-dashboard.json"))
    args = parser.parse_args()
    try:
        result = build(args.sources, args.output)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        temporary = args.output.with_name(args.output.name + ".tmp")
        temporary.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, args.output)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        print(f"project dashboard update failed: {exc}", file=sys.stderr)
        return 1
    task_count = sum(len(project["goal_tasks"]) for project in result["projects"])
    print(f"updated {args.output}: {len(result['projects'])} projects, {task_count} goal tasks")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
