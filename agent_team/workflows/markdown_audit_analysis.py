from __future__ import annotations

import time
from typing import Any, Dict, List

from ..core import Task


def handle_discover_markdown(context: Any, _task: Task) -> Dict[str, Any]:
    time.sleep(0.2)
    inventory: List[Dict[str, Any]] = []
    ignore_prefix = str(context.output_dir.resolve())
    for path in sorted(p for p in context.target_dir.rglob("*.md") if p.is_file()):
        absolute = str(path.resolve())
        if absolute.startswith(ignore_prefix):
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        lines = text.splitlines()
        headings = sum(1 for line in lines if line.lstrip().startswith("#"))
        inventory.append(
            {
                "path": str(path.relative_to(context.target_dir)),
                "line_count": len(lines),
                "heading_count": headings,
            }
        )
    context.shared_state.set("markdown_inventory", inventory)
    return {"markdown_files": len(inventory), "sample": inventory[:3]}


def handle_heading_audit(context: Any, _task: Task) -> Dict[str, Any]:
    time.sleep(0.5)
    inventory = context.shared_state.get("markdown_inventory", [])
    missing = [item for item in inventory if item["heading_count"] == 0]
    context.shared_state.set("heading_issues", missing)
    return {
        "files_without_headings": len(missing),
        "examples": [item["path"] for item in missing[:10]],
    }


def handle_length_audit(context: Any, task: Task) -> Dict[str, Any]:
    time.sleep(0.5)
    inventory = context.shared_state.get("markdown_inventory", [])
    threshold = int(task.payload.get("line_threshold", 200))
    long_files = [item for item in inventory if item["line_count"] >= threshold]
    context.shared_state.set("length_issues", long_files)
    return {
        "line_threshold": threshold,
        "long_files": len(long_files),
        "examples": [item["path"] for item in long_files[:10]],
    }


def handle_dynamic_planning(context: Any, _task: Task) -> Dict[str, Any]:
    heading_issues = context.shared_state.get("heading_issues", [])
    length_issues = context.shared_state.get("length_issues", [])

    if not context.runtime_config.enable_dynamic_tasks:
        result = {
            "enabled": False,
            "reason": "dynamic task insertion disabled by runtime config",
            "inserted_tasks": [],
            "peer_challenge_dependencies_added": [],
        }
        context.shared_state.set("dynamic_plan", result)
        return result

    candidate_tasks: List[Task] = []
    if heading_issues and not context.board.has_task("heading_structure_followup"):
        candidate_tasks.append(
            Task(
                task_id="heading_structure_followup",
                title="Run heading structure follow-up audit",
                task_type="heading_structure_followup",
                required_skills={"analysis"},
                dependencies=["dynamic_planning"],
                payload={"top_n": 8},
                locked_paths=[],
                allowed_agent_types={"analyst"},
            )
        )
    if length_issues and not context.board.has_task("length_risk_followup"):
        candidate_tasks.append(
            Task(
                task_id="length_risk_followup",
                title="Run length risk follow-up audit",
                task_type="length_risk_followup",
                required_skills={"analysis"},
                dependencies=["dynamic_planning"],
                payload={"line_threshold": 180, "top_n": 8},
                locked_paths=[],
                allowed_agent_types={"analyst"},
            )
        )

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
        "heading_issue_count": len(heading_issues),
        "length_issue_count": len(length_issues),
    }
    context.shared_state.set("dynamic_plan", result)
    return result


def handle_heading_structure_followup(context: Any, task: Task) -> Dict[str, Any]:
    time.sleep(0.3)
    top_n = int(task.payload.get("top_n", 8))
    inventory = context.shared_state.get("markdown_inventory", [])

    scored: List[Dict[str, Any]] = []
    for item in inventory:
        line_count = max(1, int(item.get("line_count", 0)))
        heading_count = int(item.get("heading_count", 0))
        density = round(heading_count / line_count, 4)
        scored.append(
            {
                "path": item.get("path", ""),
                "line_count": line_count,
                "heading_count": heading_count,
                "heading_density": density,
            }
        )

    lowest_density = sorted(scored, key=lambda row: (row["heading_density"], row["line_count"]), reverse=False)[
        :top_n
    ]
    result = {
        "top_n": top_n,
        "lowest_heading_density": lowest_density,
    }
    context.shared_state.set("heading_followup", result)
    return result


def handle_length_risk_followup(context: Any, task: Task) -> Dict[str, Any]:
    time.sleep(0.3)
    top_n = int(task.payload.get("top_n", 8))
    threshold = int(task.payload.get("line_threshold", 180))
    inventory = context.shared_state.get("markdown_inventory", [])

    risk_rows: List[Dict[str, Any]] = []
    for item in inventory:
        line_count = int(item.get("line_count", 0))
        if line_count < threshold:
            continue
        heading_count = int(item.get("heading_count", 0))
        heading_density = heading_count / max(1, line_count)
        risk_score = round(line_count * (1.0 - min(heading_density * 25.0, 1.0)), 2)
        risk_rows.append(
            {
                "path": item.get("path", ""),
                "line_count": line_count,
                "heading_count": heading_count,
                "heading_density": round(heading_density, 4),
                "risk_score": risk_score,
            }
        )

    top_risky = sorted(risk_rows, key=lambda row: (row["risk_score"], row["line_count"]), reverse=True)[:top_n]
    result = {
        "line_threshold": threshold,
        "top_n": top_n,
        "high_risk_long_files": top_risky,
    }
    context.shared_state.set("length_followup", result)
    return result
