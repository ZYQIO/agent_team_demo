from __future__ import annotations

from typing import Any, Dict, List, Mapping

from ..core import SharedState, TaskBoard, task_from_dict


def _dynamic_plan_state_key(task_type: str) -> str:
    if task_type == "dynamic_planning":
        return "dynamic_plan"
    if task_type == "repo_dynamic_planning":
        return "repo_dynamic_plan"
    return ""


def proposed_task_mutation_summary(task_mutations: Any) -> Dict[str, List[str]]:
    normalized_mutations = task_mutations if isinstance(task_mutations, Mapping) else {}
    proposed_task_ids: List[str] = []
    proposed_dependency_ids: List[str] = []

    raw_insert_tasks = normalized_mutations.get("insert_tasks", [])
    if isinstance(raw_insert_tasks, list):
        for item in raw_insert_tasks:
            if not isinstance(item, Mapping):
                continue
            task_id = str(item.get("task_id", "") or "")
            if task_id:
                proposed_task_ids.append(task_id)

    raw_dependencies = normalized_mutations.get("add_dependencies", [])
    if isinstance(raw_dependencies, list):
        for item in raw_dependencies:
            if not isinstance(item, Mapping):
                continue
            dependency_id = str(item.get("dependency_id", "") or "")
            if dependency_id:
                proposed_dependency_ids.append(dependency_id)

    return {
        "proposed_task_ids": proposed_task_ids,
        "proposed_dependency_ids": proposed_dependency_ids,
    }


def apply_task_mutation_payload(
    board: TaskBoard,
    shared_state: SharedState,
    task_type: str,
    updated_by: str,
    result: Any,
    state_updates: Any,
    task_mutations: Any,
) -> Dict[str, Any]:
    normalized_state_updates = state_updates if isinstance(state_updates, dict) else {}
    normalized_mutations = task_mutations if isinstance(task_mutations, dict) else {}
    normalized_result = result if isinstance(result, dict) else {"raw_result": result}
    inserted_task_ids: List[str] = []
    added_dependency_ids: List[str] = []

    raw_insert_tasks = normalized_mutations.get("insert_tasks", [])
    if isinstance(raw_insert_tasks, list):
        tasks_to_insert = [task_from_dict(item) for item in raw_insert_tasks if isinstance(item, dict)]
        if tasks_to_insert:
            inserted_task_ids = board.add_tasks(tasks=tasks_to_insert, inserted_by=updated_by)

    raw_dependencies = normalized_mutations.get("add_dependencies", [])
    if isinstance(raw_dependencies, list):
        for item in raw_dependencies:
            if not isinstance(item, dict):
                continue
            dependency_task_id = str(item.get("task_id", "") or "")
            dependency_id = str(item.get("dependency_id", "") or "")
            if not dependency_task_id or not dependency_id:
                continue
            if board.add_dependency(
                task_id=dependency_task_id,
                dependency_id=dependency_id,
                updated_by=updated_by,
            ):
                added_dependency_ids.append(dependency_id)

    state_update_key = _dynamic_plan_state_key(str(task_type or ""))
    if state_update_key and isinstance(normalized_result, dict):
        normalized_result["inserted_tasks"] = inserted_task_ids
        normalized_result["peer_challenge_dependencies_added"] = added_dependency_ids
        state_value = normalized_state_updates.get(state_update_key)
        if isinstance(state_value, dict):
            state_value["inserted_tasks"] = list(inserted_task_ids)
            state_value["peer_challenge_dependencies_added"] = list(added_dependency_ids)

    for key, value in normalized_state_updates.items():
        shared_state.set(str(key), value)

    return {
        "result": normalized_result,
        "inserted_task_ids": inserted_task_ids,
        "added_dependency_ids": added_dependency_ids,
    }
