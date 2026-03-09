from __future__ import annotations

import json
import threading
import uuid
from typing import Any, Dict, List, Mapping, Optional

from ..config import RuntimeConfig
from ..core import AgentProfile, Message, SharedState, Task, utc_now


TEAMMATE_SESSIONS_KEY = "teammate_sessions"
TEAMMATE_SESSIONS_FILENAME = "teammate_sessions.json"
SESSION_BOUNDARY_FILENAME = "session_boundaries.json"
SESSION_HISTORY_LIMIT = 12


def teammate_transport_for_profile(profile: AgentProfile, runtime_config: RuntimeConfig) -> str:
    if runtime_config.teammate_mode == "tmux" and profile.agent_type == "analyst":
        return "tmux"
    return "in-process"


def _clone(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False))


def _limit_list(items: List[Dict[str, Any]], limit: int = SESSION_HISTORY_LIMIT) -> List[Dict[str, Any]]:
    normalized_limit = max(1, int(limit))
    return items[-normalized_limit:]


def _normalize_session_entry(agent_name: str, entry: Mapping[str, Any]) -> Dict[str, Any]:
    now = utc_now()
    provider_memory = entry.get("provider_memory", [])
    if not isinstance(provider_memory, list):
        provider_memory = []
    task_history = entry.get("task_history", [])
    if not isinstance(task_history, list):
        task_history = []
    recent_messages = entry.get("recent_messages", [])
    if not isinstance(recent_messages, list):
        recent_messages = []
    visible_keys = entry.get("last_visible_shared_state_keys", [])
    if not isinstance(visible_keys, list):
        visible_keys = []
    return {
        "session_id": str(entry.get("session_id", "") or uuid.uuid4()),
        "agent": str(entry.get("agent", agent_name) or agent_name),
        "agent_type": str(entry.get("agent_type", "general") or "general"),
        "skills": sorted({str(skill) for skill in entry.get("skills", [])}),
        "transport": str(entry.get("transport", "") or ""),
        "status": str(entry.get("status", "created") or "created"),
        "started_at": str(entry.get("started_at", now) or now),
        "last_active_at": str(entry.get("last_active_at", now) or now),
        "current_task_id": str(entry.get("current_task_id", "") or ""),
        "current_task_type": str(entry.get("current_task_type", "") or ""),
        "run_activations": max(0, int(entry.get("run_activations", 0) or 0)),
        "initialization_count": max(0, int(entry.get("initialization_count", 0) or 0)),
        "resume_count": max(0, int(entry.get("resume_count", 0) or 0)),
        "last_initialized_at": str(entry.get("last_initialized_at", "") or ""),
        "last_resume_at": str(entry.get("last_resume_at", "") or ""),
        "last_resume_from": str(entry.get("last_resume_from", "") or ""),
        "tasks_started": max(0, int(entry.get("tasks_started", 0) or 0)),
        "tasks_completed": max(0, int(entry.get("tasks_completed", 0) or 0)),
        "tasks_failed": max(0, int(entry.get("tasks_failed", 0) or 0)),
        "messages_seen": max(0, int(entry.get("messages_seen", 0) or 0)),
        "provider_replies": max(0, int(entry.get("provider_replies", 0) or 0)),
        "context_preparations": max(0, int(entry.get("context_preparations", 0) or 0)),
        "last_visible_shared_state_keys": [str(item) for item in visible_keys],
        "last_visible_shared_state_key_count": max(
            0,
            int(entry.get("last_visible_shared_state_key_count", len(visible_keys)) or 0),
        ),
        "provider_memory": _limit_list(
            [
                {
                    "topic": str(item.get("topic", "") or ""),
                    "reply": str(item.get("reply", "") or ""),
                    "recorded_at": str(item.get("recorded_at", now) or now),
                }
                for item in provider_memory
                if isinstance(item, Mapping)
            ]
        ),
        "task_history": _limit_list(
            [
                {
                    "task_id": str(item.get("task_id", "") or ""),
                    "task_type": str(item.get("task_type", "") or ""),
                    "status": str(item.get("status", "") or ""),
                    "transport": str(item.get("transport", "") or ""),
                    "visible_shared_state_key_count": max(
                        0,
                        int(item.get("visible_shared_state_key_count", 0) or 0),
                    ),
                    "recorded_at": str(item.get("recorded_at", now) or now),
                }
                for item in task_history
                if isinstance(item, Mapping)
            ]
        ),
        "recent_messages": _limit_list(
            [
                {
                    "from_agent": str(item.get("from_agent", "") or ""),
                    "subject": str(item.get("subject", "") or ""),
                    "task_id": str(item.get("task_id", "") or ""),
                    "recorded_at": str(item.get("recorded_at", now) or now),
                }
                for item in recent_messages
                if isinstance(item, Mapping)
            ]
        ),
    }


class TeammateSessionRegistry:
    def __init__(
        self,
        shared_state: SharedState,
        initial_snapshot: Optional[Mapping[str, Any]] = None,
    ) -> None:
        self._shared_state = shared_state
        self._lock = threading.Lock()
        self._sessions: Dict[str, Dict[str, Any]] = {}
        raw_snapshot = initial_snapshot or shared_state.get(TEAMMATE_SESSIONS_KEY, {})
        if isinstance(raw_snapshot, Mapping):
            for agent_name, entry in raw_snapshot.items():
                if isinstance(entry, Mapping):
                    self._sessions[str(agent_name)] = _normalize_session_entry(str(agent_name), entry)
        self._flush_locked()

    def _flush_locked(self) -> None:
        self._shared_state.set(TEAMMATE_SESSIONS_KEY, _clone(self._sessions))

    def snapshot(self) -> Dict[str, Dict[str, Any]]:
        with self._lock:
            return _clone(self._sessions)

    def session_for(self, agent_name: str) -> Dict[str, Any]:
        with self._lock:
            entry = self._sessions.get(str(agent_name), {})
            return _clone(entry)

    def ensure_profile(
        self,
        profile: AgentProfile,
        transport: str,
        status: str = "created",
    ) -> Dict[str, Any]:
        with self._lock:
            name = str(profile.name)
            entry = self._sessions.get(name)
            if entry is None:
                entry = _normalize_session_entry(
                    name,
                    {
                        "agent": name,
                        "agent_type": profile.agent_type,
                        "skills": sorted(profile.skills),
                        "transport": transport,
                        "status": status,
                    },
                )
                self._sessions[name] = entry
            else:
                entry["agent_type"] = profile.agent_type
                entry["skills"] = sorted(profile.skills)
                if transport:
                    entry["transport"] = str(transport)
                if status:
                    entry["status"] = str(status)
                entry["last_active_at"] = utc_now()
            self._flush_locked()
            return _clone(entry)

    def activate_for_run(
        self,
        profile: AgentProfile,
        transport: str,
        resume_from: str = "",
    ) -> Dict[str, Any]:
        with self._lock:
            name = str(profile.name)
            entry = self._sessions.get(name)
            had_existing_session = entry is not None and bool(str(entry.get("session_id", "") or ""))
            if entry is None:
                entry = _normalize_session_entry(
                    name,
                    {
                        "agent": name,
                        "agent_type": profile.agent_type,
                        "skills": sorted(profile.skills),
                    },
                )
                self._sessions[name] = entry
            entry["agent_type"] = profile.agent_type
            entry["skills"] = sorted(profile.skills)
            entry["transport"] = str(transport)
            entry["run_activations"] = int(entry.get("run_activations", 0)) + 1
            entry["last_active_at"] = utc_now()
            if resume_from and had_existing_session:
                lifecycle_event = "resumed"
                entry["resume_count"] = int(entry.get("resume_count", 0)) + 1
                entry["last_resume_from"] = str(resume_from)
                entry["last_resume_at"] = utc_now()
                entry["status"] = "resumed"
            else:
                lifecycle_event = "initialized"
                entry["initialization_count"] = int(entry.get("initialization_count", 0)) + 1
                entry["last_initialized_at"] = utc_now()
                entry["status"] = "created"
            self._flush_locked()
            activated = _clone(entry)
            activated["lifecycle_event"] = lifecycle_event
            activated["resume_from"] = str(resume_from or "")
            return activated

    def record_status(
        self,
        agent_name: str,
        transport: str = "",
        status: str = "",
        current_task_id: str = "",
        current_task_type: str = "",
    ) -> Dict[str, Any]:
        with self._lock:
            name = str(agent_name)
            entry = self._sessions.get(name)
            if entry is None:
                entry = _normalize_session_entry(name, {})
                self._sessions[name] = entry
            if transport:
                entry["transport"] = str(transport)
            if status:
                entry["status"] = str(status)
            entry["current_task_id"] = str(current_task_id or "")
            entry["current_task_type"] = str(current_task_type or "")
            entry["last_active_at"] = utc_now()
            self._flush_locked()
            return _clone(entry)

    def record_message_seen(self, agent_name: str, message: Message) -> Dict[str, Any]:
        with self._lock:
            name = str(agent_name)
            entry = self._sessions.get(name)
            if entry is None:
                entry = _normalize_session_entry(name, {})
                self._sessions[name] = entry
            entry["messages_seen"] = int(entry.get("messages_seen", 0)) + 1
            recent_messages = list(entry.get("recent_messages", []))
            recent_messages.append(
                {
                    "from_agent": str(message.sender),
                    "subject": str(message.subject),
                    "task_id": str(message.task_id or ""),
                    "recorded_at": utc_now(),
                }
            )
            entry["recent_messages"] = _limit_list(recent_messages)
            entry["last_active_at"] = utc_now()
            self._flush_locked()
            return _clone(entry)

    def bind_task(
        self,
        agent_name: str,
        task: Task,
        transport: str,
        task_context: Optional[Mapping[str, Any]] = None,
    ) -> Dict[str, Any]:
        with self._lock:
            name = str(agent_name)
            entry = self._sessions.get(name)
            if entry is None:
                entry = _normalize_session_entry(name, {})
                self._sessions[name] = entry
            visible_keys = []
            visible_key_count = 0
            if isinstance(task_context, Mapping):
                raw_visible_keys = task_context.get("visible_shared_state_keys", [])
                if isinstance(raw_visible_keys, list):
                    visible_keys = [str(item) for item in raw_visible_keys]
                visible_key_count = max(
                    0,
                    int(task_context.get("visible_shared_state_key_count", len(visible_keys)) or 0),
                )
            entry["transport"] = str(transport)
            entry["status"] = "running"
            entry["current_task_id"] = str(task.task_id)
            entry["current_task_type"] = str(task.task_type)
            entry["tasks_started"] = int(entry.get("tasks_started", 0)) + 1
            entry["context_preparations"] = int(entry.get("context_preparations", 0)) + 1
            entry["last_visible_shared_state_keys"] = visible_keys
            entry["last_visible_shared_state_key_count"] = visible_key_count
            task_history = list(entry.get("task_history", []))
            task_history.append(
                {
                    "task_id": str(task.task_id),
                    "task_type": str(task.task_type),
                    "status": "started",
                    "transport": str(transport),
                    "visible_shared_state_key_count": visible_key_count,
                    "recorded_at": utc_now(),
                }
            )
            entry["task_history"] = _limit_list(task_history)
            entry["last_active_at"] = utc_now()
            self._flush_locked()
            return _clone(entry)

    def record_task_result(
        self,
        agent_name: str,
        task: Task,
        transport: str,
        success: bool,
        status: str = "",
    ) -> Dict[str, Any]:
        with self._lock:
            name = str(agent_name)
            entry = self._sessions.get(name)
            if entry is None:
                entry = _normalize_session_entry(name, {})
                self._sessions[name] = entry
            entry["transport"] = str(transport or entry.get("transport", ""))
            entry["current_task_id"] = ""
            entry["current_task_type"] = ""
            if success:
                entry["tasks_completed"] = int(entry.get("tasks_completed", 0)) + 1
            else:
                entry["tasks_failed"] = int(entry.get("tasks_failed", 0)) + 1
            entry["status"] = str(status or ("ready" if success else "error"))
            task_history = list(entry.get("task_history", []))
            task_history.append(
                {
                    "task_id": str(task.task_id),
                    "task_type": str(task.task_type),
                    "status": "completed" if success else "failed",
                    "transport": str(transport or entry.get("transport", "")),
                    "visible_shared_state_key_count": int(
                        entry.get("last_visible_shared_state_key_count", 0) or 0
                    ),
                    "recorded_at": utc_now(),
                }
            )
            entry["task_history"] = _limit_list(task_history)
            entry["last_active_at"] = utc_now()
            self._flush_locked()
            return _clone(entry)

    def record_provider_reply(
        self,
        agent_name: str,
        topic: str,
        reply: str,
        memory_turns: int,
    ) -> Dict[str, Any]:
        with self._lock:
            name = str(agent_name)
            entry = self._sessions.get(name)
            if entry is None:
                entry = _normalize_session_entry(name, {})
                self._sessions[name] = entry
            provider_memory = list(entry.get("provider_memory", []))
            provider_memory.append(
                {
                    "topic": str(topic),
                    "reply": str(reply),
                    "recorded_at": utc_now(),
                }
            )
            entry["provider_memory"] = _limit_list(provider_memory, limit=max(1, int(memory_turns)))
            entry["provider_replies"] = int(entry.get("provider_replies", 0)) + 1
            entry["last_active_at"] = utc_now()
            self._flush_locked()
            return _clone(entry)


def build_teammate_sessions_snapshot(shared_state: SharedState) -> Dict[str, Any]:
    raw_sessions = shared_state.snapshot().get(TEAMMATE_SESSIONS_KEY, {})
    sessions: List[Dict[str, Any]] = []
    status_counts: Dict[str, int] = {}
    transport_counts: Dict[str, int] = {}
    lifecycle_counts = {
        "run_activations": 0,
        "initializations": 0,
        "resumes": 0,
    }
    if isinstance(raw_sessions, Mapping):
        for agent_name, entry in sorted(raw_sessions.items()):
            if not isinstance(entry, Mapping):
                continue
            normalized = _normalize_session_entry(str(agent_name), entry)
            status = str(normalized.get("status", "") or "unknown")
            transport = str(normalized.get("transport", "") or "unknown")
            status_counts[status] = status_counts.get(status, 0) + 1
            transport_counts[transport] = transport_counts.get(transport, 0) + 1
            lifecycle_counts["run_activations"] += int(normalized.get("run_activations", 0) or 0)
            lifecycle_counts["initializations"] += int(normalized.get("initialization_count", 0) or 0)
            lifecycle_counts["resumes"] += int(normalized.get("resume_count", 0) or 0)
            sessions.append(normalized)
    return {
        "generated_at": utc_now(),
        "session_count": len(sessions),
        "status_counts": status_counts,
        "transport_counts": transport_counts,
        "lifecycle_counts": lifecycle_counts,
        "sessions": sessions,
    }


def build_session_boundary_snapshot(shared_state: SharedState) -> Dict[str, Any]:
    state_snapshot = shared_state.snapshot()
    host = state_snapshot.get("host", {})
    if not isinstance(host, Mapping):
        host = {}
    host_capabilities = host.get("capabilities", {})
    if not isinstance(host_capabilities, Mapping):
        host_capabilities = {}
    teammate_sessions = build_teammate_sessions_snapshot(shared_state=shared_state)
    session_boundaries: List[Dict[str, Any]] = []
    boundary_mode_counts: Dict[str, int] = {}
    boundary_strength_counts: Dict[str, int] = {}

    host_independent_sessions = bool(host_capabilities.get("independent_sessions", False))
    host_workspace_isolation = bool(host_capabilities.get("workspace_isolation", False))
    host_session_transport = str(host.get("session_transport", "") or "")
    for session in teammate_sessions.get("sessions", []):
        if not isinstance(session, Mapping):
            continue
        transport = str(session.get("transport", "") or "unknown")
        notes: List[str] = []
        if host_independent_sessions:
            boundary_mode = "host_native_session"
            boundary_strength = "strong"
            isolation_source = "host"
        elif transport == "tmux":
            boundary_mode = "tmux_worker_session"
            boundary_strength = "medium"
            isolation_source = "transport"
            notes.append("session_isolation_backed_by_tmux_process")
        else:
            boundary_mode = "runtime_emulated_session"
            boundary_strength = "emulated"
            isolation_source = "runtime"
            notes.append("session_isolation_backed_by_shared_runtime")
        if not host_workspace_isolation:
            notes.append("workspace_isolation_unavailable")
        if not host_independent_sessions:
            notes.append("host_independent_sessions_unavailable")
        record = {
            "agent": str(session.get("agent", "") or ""),
            "session_id": str(session.get("session_id", "") or ""),
            "transport": transport,
            "boundary_mode": boundary_mode,
            "boundary_strength": boundary_strength,
            "isolation_source": isolation_source,
            "host_kind": str(host.get("kind", "") or ""),
            "host_session_transport": host_session_transport,
            "host_independent_sessions": host_independent_sessions,
            "host_workspace_isolation": host_workspace_isolation,
            "status": str(session.get("status", "") or ""),
            "current_task_id": str(session.get("current_task_id", "") or ""),
            "current_task_type": str(session.get("current_task_type", "") or ""),
            "provider_memory_entries": len(session.get("provider_memory", [])),
            "notes": notes,
        }
        session_boundaries.append(record)
        boundary_mode_counts[boundary_mode] = boundary_mode_counts.get(boundary_mode, 0) + 1
        boundary_strength_counts[boundary_strength] = boundary_strength_counts.get(boundary_strength, 0) + 1

    return {
        "generated_at": utc_now(),
        "host": {
            "kind": str(host.get("kind", "") or ""),
            "session_transport": host_session_transport,
            "capabilities": dict(host_capabilities),
            "limits": list(host.get("limits", [])) if isinstance(host.get("limits", []), list) else [],
            "note": str(host.get("note", "") or ""),
        },
        "session_count": len(session_boundaries),
        "boundary_mode_counts": boundary_mode_counts,
        "boundary_strength_counts": boundary_strength_counts,
        "sessions": session_boundaries,
    }
