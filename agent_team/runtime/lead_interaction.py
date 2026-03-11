from __future__ import annotations

from typing import Any, Dict, List, Mapping, MutableMapping

from ..core import SharedState, utc_now
from .task_mutations import proposed_task_mutation_summary


LEAD_INTERACTION_STATE_KEY = "lead_interaction"
PLAN_APPROVAL_STATUS_PENDING = "pending"
PLAN_APPROVAL_STATUS_APPLIED = "applied"
PLAN_APPROVAL_STATUS_REJECTED = "rejected"


def _empty_state() -> Dict[str, Any]:
    return {
        "updated_at": "",
        "plan_approval_requests": {},
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
    state["updated_at"] = str(value.get("updated_at", "") or "")
    return state


def get_lead_interaction_state(shared_state: SharedState) -> Dict[str, Any]:
    return _normalized_state(shared_state.get(LEAD_INTERACTION_STATE_KEY, {}))


def set_lead_interaction_state(shared_state: SharedState, state: Mapping[str, Any]) -> Dict[str, Any]:
    normalized = _normalized_state(state)
    normalized["updated_at"] = utc_now()
    shared_state.set(LEAD_INTERACTION_STATE_KEY, normalized)
    return normalized


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
