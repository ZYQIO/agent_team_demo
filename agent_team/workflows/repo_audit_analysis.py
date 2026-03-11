from __future__ import annotations

import pathlib
import time
from typing import Any, Dict, List

from ..core import Task


def _normalize_extension(path: pathlib.Path) -> str:
    suffix = path.suffix.strip().lower()
    return suffix or "<no_ext>"


def _top_level_dir(relative_path: pathlib.Path) -> str:
    if len(relative_path.parts) > 1:
        return relative_path.parts[0]
    return "."


def handle_discover_repository(context: Any, _task: Task) -> Dict[str, Any]:
    time.sleep(0.2)
    inventory: List[Dict[str, Any]] = []
    ignore_prefix = str(context.output_dir.resolve())
    for path in sorted(p for p in context.target_dir.rglob("*") if p.is_file()):
        absolute = str(path.resolve())
        if absolute.startswith(ignore_prefix):
            continue
        relative_path = path.relative_to(context.target_dir)
        if ".git" in relative_path.parts:
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        lines = text.splitlines()
        inventory.append(
            {
                "path": str(relative_path),
                "extension": _normalize_extension(path),
                "line_count": len(lines),
                "byte_count": path.stat().st_size,
                "top_level_dir": _top_level_dir(relative_path),
            }
        )
    context.shared_state.set("repository_inventory", inventory)
    return {"repository_files": len(inventory), "sample": inventory[:3]}


def handle_extension_audit(context: Any, _task: Task) -> Dict[str, Any]:
    time.sleep(0.4)
    inventory = context.shared_state.get("repository_inventory", [])
    totals: Dict[str, Dict[str, Any]] = {}
    for item in inventory:
        extension = str(item.get("extension", "<no_ext>") or "<no_ext>")
        bucket = totals.setdefault(
            extension,
            {
                "extension": extension,
                "file_count": 0,
                "total_lines": 0,
                "total_bytes": 0,
            },
        )
        bucket["file_count"] += 1
        bucket["total_lines"] += int(item.get("line_count", 0))
        bucket["total_bytes"] += int(item.get("byte_count", 0))
    ranked = sorted(
        totals.values(),
        key=lambda row: (int(row["file_count"]), int(row["total_lines"]), str(row["extension"])),
        reverse=True,
    )
    result = {
        "total_files": len(inventory),
        "unique_extensions": len(totals),
        "files_without_extension": int(totals.get("<no_ext>", {}).get("file_count", 0)),
        "top_extensions": ranked[:6],
    }
    context.shared_state.set("repository_extension_summary", result)
    return result


def handle_large_file_audit(context: Any, task: Task) -> Dict[str, Any]:
    time.sleep(0.4)
    inventory = context.shared_state.get("repository_inventory", [])
    line_threshold = int(task.payload.get("line_threshold", 320))
    byte_threshold = int(task.payload.get("byte_threshold", 20000))
    large_files = [
        item
        for item in inventory
        if int(item.get("line_count", 0)) >= line_threshold
        or int(item.get("byte_count", 0)) >= byte_threshold
    ]
    ranked = sorted(
        large_files,
        key=lambda row: (int(row.get("line_count", 0)), int(row.get("byte_count", 0)), str(row.get("path", ""))),
        reverse=True,
    )
    context.shared_state.set("repository_large_files", ranked)
    return {
        "line_threshold": line_threshold,
        "byte_threshold": byte_threshold,
        "oversized_files": len(ranked),
        "examples": [str(item.get("path", "")) for item in ranked[:10]],
    }


def handle_repo_dynamic_planning(context: Any, _task: Task) -> Dict[str, Any]:
    inventory = context.shared_state.get("repository_inventory", [])
    extension_summary = context.shared_state.get("repository_extension_summary", {})
    large_files = context.shared_state.get("repository_large_files", [])
    policies = context.shared_state.get("policies", {})
    teammate_plan_required = (
        isinstance(policies, dict)
        and bool(policies.get("teammate_plan_required", False))
    )

    if not context.runtime_config.enable_dynamic_tasks:
        result = {
            "enabled": False,
            "reason": "dynamic task insertion disabled by runtime config",
            "inserted_tasks": [],
            "peer_challenge_dependencies_added": [],
        }
        context.shared_state.set("repo_dynamic_plan", result)
        return result

    unique_directories = sorted({str(item.get("top_level_dir", ".")) for item in inventory})
    candidate_tasks: List[Task] = []
    if int(extension_summary.get("unique_extensions", 0)) >= 2 and not context.board.has_task(
        "extension_hotspot_followup"
    ):
        candidate_tasks.append(
            Task(
                task_id="extension_hotspot_followup",
                title="Review repository extension hotspots",
                task_type="extension_hotspot_followup",
                required_skills={"analysis"},
                dependencies=["repo_dynamic_planning"],
                payload={"top_n": 6},
                locked_paths=[],
                allowed_agent_types={"analyst"},
            )
        )
    if (large_files or len(unique_directories) >= 2) and not context.board.has_task("directory_hotspot_followup"):
        candidate_tasks.append(
            Task(
                task_id="directory_hotspot_followup",
                title="Review repository directory hotspots",
                task_type="directory_hotspot_followup",
                required_skills={"analysis"},
                dependencies=["repo_dynamic_planning"],
                payload={"top_n": 6},
                locked_paths=[],
                allowed_agent_types={"analyst"},
            )
        )

    candidate_task_ids = [task.task_id for task in candidate_tasks]
    task_mutations = {
        "insert_tasks": [task.to_dict() for task in candidate_tasks],
        "add_dependencies": [
            {"task_id": "peer_challenge", "dependency_id": inserted_id}
            for inserted_id in candidate_task_ids
        ],
    }

    if teammate_plan_required:
        result = {
            "enabled": True,
            "approval_required": True,
            "inserted_tasks": list(candidate_task_ids),
            "peer_challenge_dependencies_added": list(candidate_task_ids),
            "unique_directories": len(unique_directories),
            "unique_extensions": int(extension_summary.get("unique_extensions", 0)),
            "oversized_files": len(large_files),
        }
        return {
            "result": result,
            "state_updates": {"repo_dynamic_plan": dict(result)},
            "task_mutations": task_mutations,
        }

    inserted_tasks = context.board.add_tasks(tasks=candidate_tasks, inserted_by=context.profile.name)
    peer_gate_dependencies: List[str] = []
    for inserted_id in inserted_tasks:
        if context.board.add_dependency(
            task_id="peer_challenge",
            dependency_id=inserted_id,
            updated_by=context.profile.name,
        ):
            peer_gate_dependencies.append(inserted_id)

    result = {
        "enabled": True,
        "inserted_tasks": inserted_tasks,
        "peer_challenge_dependencies_added": peer_gate_dependencies,
        "unique_directories": len(unique_directories),
        "unique_extensions": int(extension_summary.get("unique_extensions", 0)),
        "oversized_files": len(large_files),
    }
    context.shared_state.set("repo_dynamic_plan", result)
    return result


def handle_extension_hotspot_followup(context: Any, task: Task) -> Dict[str, Any]:
    time.sleep(0.3)
    top_n = int(task.payload.get("top_n", 6))
    inventory = context.shared_state.get("repository_inventory", [])
    totals: Dict[str, Dict[str, Any]] = {}
    for item in inventory:
        extension = str(item.get("extension", "<no_ext>") or "<no_ext>")
        bucket = totals.setdefault(
            extension,
            {
                "extension": extension,
                "file_count": 0,
                "total_lines": 0,
                "total_bytes": 0,
            },
        )
        bucket["file_count"] += 1
        bucket["total_lines"] += int(item.get("line_count", 0))
        bucket["total_bytes"] += int(item.get("byte_count", 0))
    hotspots = sorted(
        totals.values(),
        key=lambda row: (int(row["total_lines"]), int(row["file_count"]), str(row["extension"])),
        reverse=True,
    )[:top_n]
    result = {"top_n": top_n, "extension_hotspots": hotspots}
    context.shared_state.set("repo_extension_hotspots", result)
    return result


def handle_directory_hotspot_followup(context: Any, task: Task) -> Dict[str, Any]:
    time.sleep(0.3)
    top_n = int(task.payload.get("top_n", 6))
    inventory = context.shared_state.get("repository_inventory", [])
    totals: Dict[str, Dict[str, Any]] = {}
    for item in inventory:
        top_level_dir = str(item.get("top_level_dir", ".") or ".")
        bucket = totals.setdefault(
            top_level_dir,
            {
                "top_level_dir": top_level_dir,
                "file_count": 0,
                "total_lines": 0,
                "total_bytes": 0,
            },
        )
        bucket["file_count"] += 1
        bucket["total_lines"] += int(item.get("line_count", 0))
        bucket["total_bytes"] += int(item.get("byte_count", 0))
    busiest = sorted(
        totals.values(),
        key=lambda row: (int(row["total_lines"]), int(row["file_count"]), str(row["top_level_dir"])),
        reverse=True,
    )[:top_n]
    result = {"top_n": top_n, "busiest_directories": busiest}
    context.shared_state.set("repo_directory_hotspots", result)
    return result
