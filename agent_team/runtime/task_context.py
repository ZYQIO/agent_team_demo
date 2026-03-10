from __future__ import annotations

import dataclasses
import json
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Set

from ..core import SharedState, Task


COMMON_VISIBLE_STATE_KEYS: Set[str] = {
    "lead_name",
    "team",
    "team_profiles",
    "workflow",
    "workflow_lead_task_order",
    "workflow_report_task_ids",
    "workflow_runtime_metadata",
    "runtime_config",
    "agent_team_config",
    "policies",
    "host",
}


TASK_TYPE_VISIBLE_STATE_KEYS: Dict[str, Set[str]] = {
    "discover_markdown": set(),
    "heading_audit": {"markdown_inventory"},
    "length_audit": {"markdown_inventory"},
    "dynamic_planning": {"heading_issues", "length_issues"},
    "heading_structure_followup": {"markdown_inventory"},
    "length_risk_followup": {"markdown_inventory"},
    "peer_challenge": {
        "team_profiles",
        "heading_issues",
        "length_issues",
        "peer_challenge",
        "lead_adjudication",
    },
    "lead_adjudication": {"peer_challenge"},
    "evidence_pack": {
        "peer_challenge",
        "lead_adjudication",
        "evidence_pack",
        "team_profiles",
    },
    "lead_re_adjudication": {"lead_adjudication", "evidence_pack"},
    "llm_synthesis": {
        "heading_issues",
        "length_issues",
        "dynamic_plan",
        "heading_followup",
        "length_followup",
        "peer_challenge",
        "lead_adjudication",
        "evidence_pack",
        "lead_re_adjudication",
        "repository_inventory",
        "repository_large_files",
        "repository_extension_summary",
        "repo_dynamic_plan",
        "repo_extension_hotspots",
        "repo_directory_hotspots",
        "llm_synthesis",
    },
    "recommendation_pack": {
        "markdown_inventory",
        "heading_issues",
        "length_issues",
        "dynamic_plan",
        "heading_followup",
        "length_followup",
        "peer_challenge",
        "lead_adjudication",
        "evidence_pack",
        "lead_re_adjudication",
        "llm_synthesis",
        "team_progress",
    },
    "discover_repository": set(),
    "extension_audit": {"repository_inventory"},
    "large_file_audit": {"repository_inventory"},
    "repo_dynamic_planning": {
        "repository_inventory",
        "repository_extension_summary",
        "repository_large_files",
    },
    "extension_hotspot_followup": {"repository_inventory"},
    "directory_hotspot_followup": {"repository_inventory"},
    "repo_recommendation_pack": {
        "repository_inventory",
        "repository_extension_summary",
        "repository_large_files",
        "repo_dynamic_plan",
        "repo_extension_hotspots",
        "repo_directory_hotspots",
        "peer_challenge",
        "lead_adjudication",
        "evidence_pack",
        "lead_re_adjudication",
        "llm_synthesis",
        "team_progress",
    },
}


TASK_TYPE_VISIBLE_TASK_RESULT_IDS: Dict[str, Set[str]] = {
    "llm_synthesis": {
        "dynamic_planning",
        "heading_audit",
        "heading_structure_followup",
        "lead_adjudication",
        "lead_re_adjudication",
        "length_audit",
        "length_risk_followup",
        "peer_challenge",
        "evidence_pack",
        "extension_audit",
        "extension_hotspot_followup",
        "large_file_audit",
        "repo_dynamic_planning",
        "directory_hotspot_followup",
    },
    "recommendation_pack": {
        "heading_audit",
        "length_audit",
    },
    "repo_recommendation_pack": {
        "large_file_audit",
    },
}


def visible_state_keys_for_task(task_type: str) -> List[str]:
    scoped = set(COMMON_VISIBLE_STATE_KEYS)
    scoped.update(TASK_TYPE_VISIBLE_STATE_KEYS.get(str(task_type or ""), set()))
    return sorted(scoped)


def visible_task_result_ids_for_task(task_type: str) -> List[str]:
    return sorted(TASK_TYPE_VISIBLE_TASK_RESULT_IDS.get(str(task_type or ""), set()))


@dataclasses.dataclass
class ScopedSharedState:
    _underlying: SharedState
    _visible_keys: Set[str]
    _written_keys: Set[str] = dataclasses.field(default_factory=set)
    _buffered_updates: Dict[str, Any] = dataclasses.field(default_factory=dict)
    _write_through: bool = True

    def get(self, key: str, default: Any = None) -> Any:
        normalized = str(key)
        if normalized in self._buffered_updates:
            return self._buffered_updates.get(normalized, default)
        if normalized in self._visible_keys or normalized in self._written_keys:
            return self._underlying.get(normalized, default)
        return default

    def set(self, key: str, value: Any) -> None:
        normalized = str(key)
        if self._write_through:
            self._underlying.set(normalized, value)
        else:
            self._buffered_updates[normalized] = value
        self._written_keys.add(normalized)

    def snapshot(self) -> Dict[str, Any]:
        underlying_snapshot = self._underlying.snapshot()
        allowed_keys = self._visible_keys | self._written_keys
        visible_snapshot = {
            key: value
            for key, value in underlying_snapshot.items()
            if key in allowed_keys
        }
        if self._buffered_updates:
            visible_snapshot.update(self._buffered_updates)
        return json.loads(json.dumps(visible_snapshot, ensure_ascii=False))

    def buffered_updates(self) -> Dict[str, Any]:
        return json.loads(json.dumps(self._buffered_updates, ensure_ascii=False))


def build_task_context_snapshot(
    context: Any,
    task: Task,
    profile: Optional[Any] = None,
) -> Dict[str, Any]:
    active_profile = profile or context.profile
    state_snapshot = context.shared_state.snapshot()
    visible_keys = visible_state_keys_for_task(task.task_type)
    visible_key_set = set(visible_keys)
    visible_state = {
        key: state_snapshot[key]
        for key in visible_keys
        if key in state_snapshot
    }
    visible_task_result_ids = visible_task_result_ids_for_task(task.task_type)
    visible_task_results = {}
    for result_task_id in visible_task_result_ids:
        result = context.board.get_task_result(result_task_id)
        if result is not None:
            visible_task_results[result_task_id] = result
    omitted_keys = sorted(
        key for key in state_snapshot.keys()
        if key not in visible_key_set
    )
    dependency_results = {
        dep_id: context.board.get_task_result(dep_id)
        for dep_id in task.dependencies
    }
    board_snapshot = context.board.snapshot()
    task_status_counts = _task_status_counts(board_snapshot.get("tasks", []))
    return {
        "task_id": task.task_id,
        "task_type": task.task_type,
        "task_title": task.title,
        "agent": str(getattr(active_profile, "name", "") or ""),
        "agent_type": str(getattr(active_profile, "agent_type", "") or ""),
        "goal": str(context.goal),
        "dependencies": list(task.dependencies),
        "dependency_results": dependency_results,
        "visible_shared_state_keys": visible_keys,
        "visible_shared_state": visible_state,
        "visible_task_result_ids": visible_task_result_ids,
        "visible_task_results": visible_task_results,
        "visible_task_result_count": len(visible_task_results),
        "omitted_shared_state_keys": omitted_keys,
        "visible_shared_state_key_count": len(visible_state),
        "omitted_shared_state_key_count": len(omitted_keys),
        "task_status_counts": task_status_counts,
        "scope": "task_scoped_shared_state_view",
    }


def _task_status_counts(tasks: Iterable[Any]) -> Dict[str, int]:
    counts = {
        "pending": 0,
        "blocked": 0,
        "in_progress": 0,
        "completed": 0,
        "failed": 0,
        "other": 0,
    }
    for item in tasks:
        if not isinstance(item, Mapping):
            counts["other"] += 1
            continue
        status = str(item.get("status", "other") or "other")
        if status not in counts:
            counts["other"] += 1
        else:
            counts[status] += 1
    return counts
