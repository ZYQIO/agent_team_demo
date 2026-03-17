from __future__ import annotations

import json
import pathlib
from typing import Any, Dict, List, Mapping, MutableMapping

from ..core import SharedState, utc_now
from .task_mutations import proposed_task_mutation_summary


LEAD_INTERACTION_STATE_KEY = "lead_interaction"
LEAD_COMMANDS_FILENAME = "lead_commands.jsonl"
LEAD_PLAN_REPLY_SUBJECT = "lead_plan_reply"
LEAD_PLAN_REQUEST_SUBJECT = "lead_plan_request"
LEAD_STATUS_REQUEST_SUBJECT = "lead_status_request"
LEAD_STATUS_REPLY_SUBJECT = "lead_status_reply"
PLAN_APPROVAL_STATUS_PENDING = "pending"
PLAN_APPROVAL_STATUS_APPLIED = "applied"
PLAN_APPROVAL_STATUS_REJECTED = "rejected"


def _empty_state() -> Dict[str, Any]:
    return {
        "updated_at": "",
        "command_path": "",
        "command_cursor": 0,
        "last_command_at": "",
        "recent_commands": [],
        "plan_approval_requests": {},
    }


def _task_mutation_preview(task_mutations: Any) -> Dict[str, List[Dict[str, Any]]]:
    normalized_mutations = task_mutations if isinstance(task_mutations, Mapping) else {}
    proposed_tasks_preview: List[Dict[str, Any]] = []
    raw_insert_tasks = normalized_mutations.get("insert_tasks", [])
    if isinstance(raw_insert_tasks, list):
        for item in raw_insert_tasks:
            if not isinstance(item, Mapping):
                continue
            task_id = str(item.get("task_id", "") or "")
            if not task_id:
                continue
            proposed_tasks_preview.append(
                {
                    "task_id": task_id,
                    "task_type": str(item.get("task_type", "") or ""),
                    "title": str(item.get("title", "") or ""),
                    "allowed_agent_types": [
                        str(agent_type)
                        for agent_type in item.get("allowed_agent_types", [])
                        if str(agent_type)
                    ]
                    if isinstance(item.get("allowed_agent_types", []), list)
                    else [],
                    "dependencies": [
                        str(dep_id)
                        for dep_id in item.get("dependencies", [])
                        if str(dep_id)
                    ]
                    if isinstance(item.get("dependencies", []), list)
                    else [],
                }
            )

    proposed_dependencies_preview: List[Dict[str, Any]] = []
    raw_dependencies = normalized_mutations.get("add_dependencies", [])
    if isinstance(raw_dependencies, list):
        for item in raw_dependencies:
            if not isinstance(item, Mapping):
                continue
            task_id = str(item.get("task_id", "") or "")
            dependency_id = str(item.get("dependency_id", "") or "")
            if not task_id or not dependency_id:
                continue
            proposed_dependencies_preview.append(
                {
                    "task_id": task_id,
                    "dependency_id": dependency_id,
                }
            )

    return {
        "proposed_tasks_preview": proposed_tasks_preview,
        "proposed_dependencies_preview": proposed_dependencies_preview,
    }


def _normalized_state(value: Any) -> Dict[str, Any]:
    if not isinstance(value, Mapping):
        return _empty_state()
    state = _empty_state()
    raw_requests = value.get("plan_approval_requests", {})
    if isinstance(raw_requests, Mapping):
        state["plan_approval_requests"] = {
            str(task_id): dict(item)
            for task_id, item in raw_requests.items()
            if str(task_id) and isinstance(item, Mapping)
        }
    state["command_path"] = str(value.get("command_path", "") or "")
    try:
        state["command_cursor"] = int(value.get("command_cursor", 0) or 0)
    except (TypeError, ValueError):
        state["command_cursor"] = 0
    state["last_command_at"] = str(value.get("last_command_at", "") or "")
    raw_recent_commands = value.get("recent_commands", [])
    if isinstance(raw_recent_commands, list):
        state["recent_commands"] = [dict(item) for item in raw_recent_commands if isinstance(item, Mapping)]
    state["updated_at"] = str(value.get("updated_at", "") or "")
    return state


def get_lead_interaction_state(shared_state: SharedState) -> Dict[str, Any]:
    return _normalized_state(shared_state.get(LEAD_INTERACTION_STATE_KEY, {}))


def set_lead_interaction_state(shared_state: SharedState, state: Mapping[str, Any]) -> Dict[str, Any]:
    normalized = _normalized_state(state)
    normalized["updated_at"] = utc_now()
    shared_state.set(LEAD_INTERACTION_STATE_KEY, normalized)
    return normalized


def ensure_lead_command_channel(output_dir: pathlib.Path, shared_state: SharedState) -> pathlib.Path:
    command_path = pathlib.Path(output_dir) / LEAD_COMMANDS_FILENAME
    command_path.parent.mkdir(parents=True, exist_ok=True)
    command_path.touch(exist_ok=True)
    state = get_lead_interaction_state(shared_state)
    if str(state.get("command_path", "") or "") != str(command_path):
        state["command_path"] = str(command_path)
        set_lead_interaction_state(shared_state=shared_state, state=state)
    return command_path


def record_lead_command(
    shared_state: SharedState,
    *,
    command: str,
    task_ids: List[str] | None = None,
    agent: str = "",
    raw: str = "",
    valid: bool = True,
    source: str = "interactive",
    line_index: int = -1,
    recent_limit: int = 20,
) -> Dict[str, Any]:
    state = get_lead_interaction_state(shared_state)
    recent_commands = list(state.get("recent_commands", []))
    record = {
        "line_index": int(line_index),
        "received_at": utc_now(),
        "raw": str(raw or ""),
        "valid": bool(valid),
        "command": str(command or ""),
        "task_ids": [str(task_id) for task_id in (task_ids or []) if str(task_id)],
        "agent": str(agent or ""),
        "source": str(source or "interactive"),
    }
    recent_commands.append(record)
    state["recent_commands"] = recent_commands[-max(1, int(recent_limit)) :]
    state["last_command_at"] = str(record["received_at"])
    set_lead_interaction_state(shared_state=shared_state, state=state)
    return record


def consume_lead_commands(
    output_dir: pathlib.Path,
    shared_state: SharedState,
    logger: Any,
    recent_limit: int = 20,
) -> Dict[str, Any]:
    command_path = ensure_lead_command_channel(output_dir=output_dir, shared_state=shared_state)
    state = get_lead_interaction_state(shared_state)
    cursor = max(0, int(state.get("command_cursor", 0) or 0))
    recent_commands = list(state.get("recent_commands", []))
    approve_task_ids: List[str] = []
    reject_task_ids: List[str] = []
    plan_request_agents: List[str] = []
    status_request_agents: List[str] = []
    approve_all_pending = False
    consumed_count = 0

    lines = command_path.read_text(encoding="utf-8").splitlines()
    for line_index, raw_line in enumerate(lines[cursor:], start=cursor):
        line = raw_line.strip()
        if not line:
            continue
        received_at = utc_now()
        consumed_count += 1
        command_record: Dict[str, Any] = {
            "line_index": line_index,
            "received_at": received_at,
            "raw": line,
            "valid": False,
            "command": "",
            "task_ids": [],
            "source": "file",
        }
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            logger.log(
                "lead_command_invalid",
                line_index=line_index,
                error="invalid_json",
            )
            recent_commands.append(command_record)
            continue
        if not isinstance(payload, Mapping):
            logger.log(
                "lead_command_invalid",
                line_index=line_index,
                error="invalid_payload",
            )
            recent_commands.append(command_record)
            continue
        command = str(payload.get("command", "") or "").strip().lower()
        raw_task_ids = payload.get("task_ids", [])
        task_ids: List[str] = []
        if isinstance(raw_task_ids, list):
            task_ids.extend(str(item) for item in raw_task_ids if str(item))
        task_id = str(payload.get("task_id", "") or "")
        if task_id:
            task_ids.append(task_id)
        command_record.update(
            {
                "valid": True,
                "command": command,
                "task_ids": list(task_ids),
                "agent": "",
            }
        )
        agent = str(payload.get("agent", "") or payload.get("recipient", "") or "").strip()
        if agent:
            command_record["agent"] = agent
        recent_commands.append(command_record)
        if command == "approve_plan":
            approve_task_ids.extend(task_ids)
        elif command == "reject_plan":
            reject_task_ids.extend(task_ids)
        elif command == "approve_all_pending_plans":
            approve_all_pending = True
        elif command == "request_teammate_status":
            if agent:
                status_request_agents.append(agent)
            else:
                command_record["valid"] = False
                logger.log(
                    "lead_command_invalid",
                    line_index=line_index,
                    error="missing_agent",
                    command=command,
                )
                continue
        elif command == "request_teammate_plan":
            if agent:
                plan_request_agents.append(agent)
            else:
                command_record["valid"] = False
                logger.log(
                    "lead_command_invalid",
                    line_index=line_index,
                    error="missing_agent",
                    command=command,
                )
                continue
        else:
            command_record["valid"] = False
            logger.log(
                "lead_command_invalid",
                line_index=line_index,
                error="unsupported_command",
                command=command,
            )
            continue
        logger.log(
            "lead_command_received",
            line_index=line_index,
            command=command,
            task_ids=task_ids,
            agent=agent,
        )

    state["command_cursor"] = len(lines)
    if consumed_count:
        state["last_command_at"] = utc_now()
    state["recent_commands"] = recent_commands[-max(1, int(recent_limit)) :]
    set_lead_interaction_state(shared_state=shared_state, state=state)
    return {
        "approve_task_ids": approve_task_ids,
        "reject_task_ids": reject_task_ids,
        "plan_request_agents": plan_request_agents,
        "status_request_agents": status_request_agents,
        "approve_all_pending_plans": approve_all_pending,
        "consumed_count": consumed_count,
        "command_path": str(command_path),
    }


def plan_approval_required(shared_state: SharedState, requested_by: str, task_mutations: Any) -> bool:
    if not str(requested_by or ""):
        return False
    if not isinstance(task_mutations, Mapping) or not task_mutations:
        return False
    policies = shared_state.get("policies", {})
    if not isinstance(policies, Mapping):
        return False
    return bool(policies.get("teammate_plan_required", False))


def queue_plan_approval_request(
    shared_state: SharedState,
    logger: Any,
    requested_by: str,
    task_id: str,
    task_type: str,
    transport: str,
    result: Any,
    state_updates: Any,
    task_mutations: Any,
) -> Dict[str, Any]:
    state = get_lead_interaction_state(shared_state)
    requests = state.setdefault("plan_approval_requests", {})
    mutation_summary = proposed_task_mutation_summary(task_mutations)
    mutation_preview = _task_mutation_preview(task_mutations)
    record = {
        "task_id": str(task_id or ""),
        "task_type": str(task_type or ""),
        "requested_by": str(requested_by or ""),
        "transport": str(transport or ""),
        "status": PLAN_APPROVAL_STATUS_PENDING,
        "requested_at": utc_now(),
        "decision_at": "",
        "decision_source": "",
        "result": dict(result) if isinstance(result, Mapping) else {"raw_result": result},
        "state_updates": dict(state_updates) if isinstance(state_updates, Mapping) else {},
        "task_mutations": dict(task_mutations) if isinstance(task_mutations, Mapping) else {},
        "proposed_task_ids": list(mutation_summary.get("proposed_task_ids", [])),
        "proposed_dependency_ids": list(mutation_summary.get("proposed_dependency_ids", [])),
        "proposed_tasks_preview": list(mutation_preview.get("proposed_tasks_preview", [])),
        "proposed_dependencies_preview": list(mutation_preview.get("proposed_dependencies_preview", [])),
        "applied_task_ids": [],
        "applied_dependency_ids": [],
    }
    requests[record["task_id"]] = record
    set_lead_interaction_state(shared_state=shared_state, state=state)
    logger.log(
        "plan_approval_requested",
        task_id=record["task_id"],
        task_type=record["task_type"],
        requested_by=record["requested_by"],
        transport=record["transport"],
        insert_task_count=len(record["proposed_task_ids"]),
        add_dependency_count=len(record["proposed_dependency_ids"]),
    )
    return record


def list_plan_approval_requests(
    shared_state: SharedState,
    status: str = "",
) -> List[Dict[str, Any]]:
    state = get_lead_interaction_state(shared_state)
    requests = state.get("plan_approval_requests", {})
    if not isinstance(requests, Mapping):
        return []
    items = [dict(item) for item in requests.values() if isinstance(item, Mapping)]
    if status:
        items = [item for item in items if str(item.get("status", "") or "") == status]
    items.sort(key=lambda item: (str(item.get("requested_at", "") or ""), str(item.get("task_id", "") or "")))
    return items


def describe_plan_approval_request(record: Mapping[str, Any]) -> List[str]:
    if not isinstance(record, Mapping):
        return ["invalid_request"]
    lines = [
        (
            f"task_id={str(record.get('task_id', '') or '')} "
            f"task_type={str(record.get('task_type', '') or '')} "
            f"requested_by={str(record.get('requested_by', '') or '')} "
            f"transport={str(record.get('transport', '') or '')} "
            f"status={str(record.get('status', '') or '')}"
        )
    ]
    result = record.get("result", {})
    state_updates = record.get("state_updates", {})
    if isinstance(result, Mapping):
        lines.append(
            "result_keys=" + (",".join(sorted(str(key) for key in result.keys())) or "none")
        )
    if isinstance(state_updates, Mapping):
        lines.append(
            "state_update_keys=" + (",".join(sorted(str(key) for key in state_updates.keys())) or "none")
        )
    proposed_task_ids = record.get("proposed_task_ids", [])
    if isinstance(proposed_task_ids, list):
        lines.append("proposed_task_ids=" + (",".join(str(item) for item in proposed_task_ids if str(item)) or "none"))
    proposed_dependency_ids = record.get("proposed_dependency_ids", [])
    if isinstance(proposed_dependency_ids, list):
        lines.append(
            "proposed_dependency_ids="
            + (",".join(str(item) for item in proposed_dependency_ids if str(item)) or "none")
        )
    proposed_tasks_preview = record.get("proposed_tasks_preview", [])
    if isinstance(proposed_tasks_preview, list) and proposed_tasks_preview:
        preview_lines = []
        for item in proposed_tasks_preview:
            if not isinstance(item, Mapping):
                continue
            task_id = str(item.get("task_id", "") or "")
            task_type = str(item.get("task_type", "") or "")
            title = str(item.get("title", "") or "")
            agent_types = (
                ",".join(str(agent_type) for agent_type in item.get("allowed_agent_types", []) if str(agent_type))
                if isinstance(item.get("allowed_agent_types", []), list)
                else ""
            )
            dependencies = (
                ",".join(str(dep_id) for dep_id in item.get("dependencies", []) if str(dep_id))
                if isinstance(item.get("dependencies", []), list)
                else ""
            )
            preview_lines.append(
                f"{task_id}[{task_type or 'unknown'}]"
                + (f" title={title}" if title else "")
                + (f" agents={agent_types}" if agent_types else "")
                + (f" deps={dependencies}" if dependencies else "")
            )
        lines.append("task_preview=" + ("; ".join(preview_lines) if preview_lines else "none"))
    proposed_dependencies_preview = record.get("proposed_dependencies_preview", [])
    if isinstance(proposed_dependencies_preview, list) and proposed_dependencies_preview:
        preview_lines = []
        for item in proposed_dependencies_preview:
            if not isinstance(item, Mapping):
                continue
            task_id = str(item.get("task_id", "") or "")
            dependency_id = str(item.get("dependency_id", "") or "")
            if task_id and dependency_id:
                preview_lines.append(f"{task_id}+={dependency_id}")
        lines.append("dependency_preview=" + ("; ".join(preview_lines) if preview_lines else "none"))
    return lines


def update_plan_approval_request(
    shared_state: SharedState,
    task_id: str,
    *,
    status: str,
    decision_source: str,
    applied_task_ids: List[str] | None = None,
    applied_dependency_ids: List[str] | None = None,
) -> Dict[str, Any]:
    normalized_task_id = str(task_id or "")
    if not normalized_task_id:
        return {}
    state = get_lead_interaction_state(shared_state)
    requests = state.setdefault("plan_approval_requests", {})
    raw_record = requests.get(normalized_task_id, {})
    if not isinstance(raw_record, MutableMapping):
        raw_record = {}
    record = dict(raw_record)
    if not record:
        return {}
    record["status"] = str(status or PLAN_APPROVAL_STATUS_PENDING)
    record["decision_at"] = utc_now()
    record["decision_source"] = str(decision_source or "")
    if applied_task_ids is not None:
        record["applied_task_ids"] = [str(item) for item in applied_task_ids if str(item)]
    if applied_dependency_ids is not None:
        record["applied_dependency_ids"] = [str(item) for item in applied_dependency_ids if str(item)]
    requests[normalized_task_id] = record
    set_lead_interaction_state(shared_state=shared_state, state=state)
    return record
