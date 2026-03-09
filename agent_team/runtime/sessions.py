from __future__ import annotations

import json
from typing import Any, Dict, Mapping, Optional, Sequence

from ..core import AgentProfile, SharedState, utc_now


AGENT_SESSION_REGISTRY_KEY = "agent_session_registry"


def _default_agent_session_entry(
    *,
    agent_name: str,
    agent_type: str,
    requested_mode: str,
    host_session: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    now = utc_now()
    host_payload = dict(host_session or {})
    return {
        "agent_name": agent_name,
        "agent_type": agent_type,
        "registered_at": now,
        "last_updated_at": now,
        "requested_teammate_mode": requested_mode,
        "host_kind": str(host_payload.get("host_kind", "") or ""),
        "session_transport": str(host_payload.get("session_transport", "") or ""),
        "effective_target_dir": str(host_payload.get("effective_target_dir", "") or ""),
        "workspace_isolated": bool(host_payload.get("workspace_isolated", False)),
        "auto_context_enabled": bool(host_payload.get("auto_context_enabled", False)),
        "host_session": host_payload,
        "current_transport": "thread" if requested_mode != "tmux" else "",
        "last_transport": "",
        "last_task_id": "",
        "last_task_type": "",
        "last_status": "",
        "last_fallback_reason": "",
        "tmux_preferred_session_name": "",
        "tmux_session_name": "",
        "tmux_session_status": "",
        "reuse_authorized": False,
        "reused_existing": False,
        "retained_for_reuse": False,
        "tasks_started": 0,
        "tasks_completed": 0,
        "tasks_failed": 0,
        "external_runs": 0,
        "fallback_runs": 0,
    }


def load_agent_session_registry(shared_state: SharedState) -> Dict[str, Dict[str, Any]]:
    raw = shared_state.get(AGENT_SESSION_REGISTRY_KEY, {})
    if not isinstance(raw, dict):
        return {}
    return json.loads(json.dumps(raw, ensure_ascii=False))


def save_agent_session_registry(shared_state: SharedState, registry: Dict[str, Dict[str, Any]]) -> None:
    shared_state.set(AGENT_SESSION_REGISTRY_KEY, registry)


def initialize_agent_session_registry(
    *,
    shared_state: SharedState,
    profiles: Sequence[AgentProfile],
    host_sessions: Mapping[str, Mapping[str, Any]],
    requested_mode: str,
) -> Dict[str, Dict[str, Any]]:
    registry: Dict[str, Dict[str, Any]] = {}
    for profile in profiles:
        host_session = host_sessions.get(profile.name, {})
        registry[profile.name] = _default_agent_session_entry(
            agent_name=profile.name,
            agent_type=profile.agent_type,
            requested_mode=requested_mode,
            host_session=host_session if isinstance(host_session, Mapping) else {},
        )
    save_agent_session_registry(shared_state, registry)
    return registry


def update_agent_session_registry(
    *,
    shared_state: SharedState,
    agent_name: str,
    agent_type: str = "",
    requested_mode: str = "",
    host_session: Optional[Mapping[str, Any]] = None,
    task_id: str = "",
    task_type: str = "",
    status: str = "",
    transport: str = "",
    current_transport: str = "",
    fallback_reason: Optional[str] = None,
    tmux_preferred_session_name: str = "",
    tmux_session_name: str = "",
    tmux_session_status: str = "",
    reuse_authorized: Optional[bool] = None,
    reused_existing: Optional[bool] = None,
    retained_for_reuse: Optional[bool] = None,
    task_started_delta: int = 0,
    task_completed_delta: int = 0,
    task_failed_delta: int = 0,
    external_run_delta: int = 0,
    fallback_run_delta: int = 0,
) -> Dict[str, Any]:
    registry = load_agent_session_registry(shared_state)
    entry = registry.get(agent_name)
    if not isinstance(entry, dict):
        entry = _default_agent_session_entry(
            agent_name=agent_name,
            agent_type=agent_type or "general",
            requested_mode=requested_mode or "",
            host_session=host_session or {},
        )
    if agent_type:
        entry["agent_type"] = agent_type
    if requested_mode:
        entry["requested_teammate_mode"] = requested_mode
    if host_session is not None:
        host_payload = dict(host_session)
        entry["host_session"] = host_payload
        entry["host_kind"] = str(host_payload.get("host_kind", "") or "")
        entry["session_transport"] = str(host_payload.get("session_transport", "") or "")
        entry["effective_target_dir"] = str(host_payload.get("effective_target_dir", "") or "")
        entry["workspace_isolated"] = bool(host_payload.get("workspace_isolated", False))
        entry["auto_context_enabled"] = bool(host_payload.get("auto_context_enabled", False))
    if task_id:
        entry["last_task_id"] = task_id
    if task_type:
        entry["last_task_type"] = task_type
    if status:
        entry["last_status"] = status
    if transport:
        entry["last_transport"] = transport
        entry["current_transport"] = current_transport or transport
    elif current_transport:
        entry["current_transport"] = current_transport
    if fallback_reason is not None:
        entry["last_fallback_reason"] = str(fallback_reason)
    if tmux_preferred_session_name:
        entry["tmux_preferred_session_name"] = tmux_preferred_session_name
    if tmux_session_name:
        entry["tmux_session_name"] = tmux_session_name
    if tmux_session_status:
        entry["tmux_session_status"] = tmux_session_status
    if reuse_authorized is not None:
        entry["reuse_authorized"] = bool(reuse_authorized)
    if reused_existing is not None:
        entry["reused_existing"] = bool(reused_existing)
    if retained_for_reuse is not None:
        entry["retained_for_reuse"] = bool(retained_for_reuse)
    entry["tasks_started"] = int(entry.get("tasks_started", 0)) + max(0, int(task_started_delta))
    entry["tasks_completed"] = int(entry.get("tasks_completed", 0)) + max(0, int(task_completed_delta))
    entry["tasks_failed"] = int(entry.get("tasks_failed", 0)) + max(0, int(task_failed_delta))
    entry["external_runs"] = int(entry.get("external_runs", 0)) + max(0, int(external_run_delta))
    entry["fallback_runs"] = int(entry.get("fallback_runs", 0)) + max(0, int(fallback_run_delta))
    entry["last_updated_at"] = utc_now()
    registry[agent_name] = entry
    save_agent_session_registry(shared_state, registry)
    return dict(entry)
