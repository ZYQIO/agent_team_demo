from __future__ import annotations

import json
import threading
import uuid
from typing import Any, Dict, List, Mapping, Optional

from ..config import RuntimeConfig
from ..core import AgentProfile, Message, SharedState, Task, utc_now
from ..host import build_host_enforcement_snapshot


TEAMMATE_SESSIONS_KEY = "teammate_sessions"
TEAMMATE_SESSIONS_FILENAME = "teammate_sessions.json"
SESSION_BOUNDARY_FILENAME = "session_boundaries.json"
SESSION_HISTORY_LIMIT = 12
SESSION_TELEMETRY_SUBJECT = "session_telemetry"


def teammate_transport_for_profile(profile: AgentProfile, runtime_config: RuntimeConfig) -> str:
    if runtime_config.teammate_mode == "host":
        return "host"
    if runtime_config.teammate_mode == "tmux" and profile.agent_type == "analyst":
        return "tmux"
    if runtime_config.teammate_mode == "subprocess" and profile.agent_type == "analyst":
        return "subprocess"
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
        "transport_session_name": str(entry.get("transport_session_name", "") or ""),
        "transport_backend": str(entry.get("transport_backend", "") or ""),
        "workspace_root": str(entry.get("workspace_root", "") or ""),
        "workspace_workdir": str(entry.get("workspace_workdir", "") or ""),
        "workspace_home_dir": str(entry.get("workspace_home_dir", "") or ""),
        "workspace_target_dir": str(entry.get("workspace_target_dir", "") or ""),
        "workspace_tmp_dir": str(entry.get("workspace_tmp_dir", "") or ""),
        "workspace_scope": str(entry.get("workspace_scope", "") or ""),
        "workspace_isolation_active": bool(entry.get("workspace_isolation_active", False)),
        "transport_reuse_count": max(0, int(entry.get("transport_reuse_count", 0) or 0)),
        "reuse_authorized": bool(entry.get("reuse_authorized", False)),
        "retained_for_reuse": bool(entry.get("retained_for_reuse", False)),
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


def apply_session_telemetry_event(
    session_entry: Optional[Mapping[str, Any]],
    telemetry: Mapping[str, Any],
) -> Dict[str, Any]:
    agent_name = str(telemetry.get("agent", "") or "")
    normalized = _normalize_session_entry(agent_name or "unknown", session_entry or {})
    if agent_name:
        normalized["agent"] = agent_name
    agent_type = str(telemetry.get("agent_type", "") or "")
    if agent_type:
        normalized["agent_type"] = agent_type
    raw_skills = telemetry.get("skills", [])
    if isinstance(raw_skills, list) and raw_skills:
        normalized["skills"] = sorted({str(skill) for skill in raw_skills if str(skill)})
    event_type = str(telemetry.get("event_type", "") or "")
    transport = str(telemetry.get("transport", "") or "")
    if transport:
        normalized["transport"] = transport
    transport_backend = str(telemetry.get("transport_backend", "") or "")
    if transport_backend:
        normalized["transport_backend"] = transport_backend
    if event_type == "status":
        status = str(telemetry.get("status", "") or "")
        if status:
            normalized["status"] = status
        normalized["current_task_id"] = str(telemetry.get("current_task_id", "") or "")
        normalized["current_task_type"] = str(telemetry.get("current_task_type", "") or "")
    elif event_type == "message_seen":
        normalized["messages_seen"] = int(normalized.get("messages_seen", 0) or 0) + 1
        recent_messages = list(normalized.get("recent_messages", []))
        recent_messages.append(
            {
                "from_agent": str(telemetry.get("from_agent", "") or ""),
                "subject": str(telemetry.get("subject", "") or ""),
                "task_id": str(telemetry.get("task_id", "") or ""),
                "recorded_at": utc_now(),
            }
        )
        normalized["recent_messages"] = _limit_list(recent_messages)
    elif event_type == "bind_task":
        normalized["status"] = "running"
        normalized["current_task_id"] = str(telemetry.get("task_id", "") or "")
        normalized["current_task_type"] = str(telemetry.get("task_type", "") or "")
        normalized["tasks_started"] = int(normalized.get("tasks_started", 0) or 0) + 1
        normalized["context_preparations"] = int(normalized.get("context_preparations", 0) or 0) + 1
        visible_keys = telemetry.get("visible_shared_state_keys", [])
        if isinstance(visible_keys, list):
            normalized["last_visible_shared_state_keys"] = [str(item) for item in visible_keys]
        normalized["last_visible_shared_state_key_count"] = max(
            0,
            int(
                telemetry.get(
                    "visible_shared_state_key_count",
                    len(normalized.get("last_visible_shared_state_keys", [])),
                )
                or 0
            ),
        )
        task_history = list(normalized.get("task_history", []))
        task_history.append(
            {
                "task_id": str(telemetry.get("task_id", "") or ""),
                "task_type": str(telemetry.get("task_type", "") or ""),
                "status": "started",
                "transport": str(normalized.get("transport", "") or ""),
                "visible_shared_state_key_count": int(normalized.get("last_visible_shared_state_key_count", 0) or 0),
                "recorded_at": utc_now(),
            }
        )
        normalized["task_history"] = _limit_list(task_history)
    elif event_type == "task_result":
        normalized["current_task_id"] = ""
        normalized["current_task_type"] = ""
        success = bool(telemetry.get("success", False))
        if success:
            normalized["tasks_completed"] = int(normalized.get("tasks_completed", 0) or 0) + 1
        else:
            normalized["tasks_failed"] = int(normalized.get("tasks_failed", 0) or 0) + 1
        result_status = str(telemetry.get("status", "") or ("ready" if success else "error"))
        normalized["status"] = result_status
        task_history = list(normalized.get("task_history", []))
        task_history.append(
            {
                "task_id": str(telemetry.get("task_id", "") or ""),
                "task_type": str(telemetry.get("task_type", "") or ""),
                "status": "completed" if success else "failed",
                "transport": str(normalized.get("transport", "") or ""),
                "visible_shared_state_key_count": int(normalized.get("last_visible_shared_state_key_count", 0) or 0),
                "recorded_at": utc_now(),
            }
        )
        normalized["task_history"] = _limit_list(task_history)
    elif event_type == "provider_reply":
        provider_memory = list(normalized.get("provider_memory", []))
        provider_memory.append(
            {
                "topic": str(telemetry.get("topic", "") or ""),
                "reply": str(telemetry.get("reply", "") or ""),
                "recorded_at": utc_now(),
            }
        )
        memory_turns = max(1, int(telemetry.get("memory_turns", SESSION_HISTORY_LIMIT) or 1))
        normalized["provider_memory"] = _limit_list(provider_memory, limit=memory_turns)
        normalized["provider_replies"] = int(normalized.get("provider_replies", 0) or 0) + 1
    normalized["last_active_at"] = utc_now()
    return normalized


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

    def apply_telemetry(self, telemetry: Mapping[str, Any]) -> Dict[str, Any]:
        with self._lock:
            agent_name = str(telemetry.get("agent", "") or "")
            if not agent_name:
                raise ValueError("telemetry is missing agent")
            entry = self._sessions.get(agent_name, {})
            self._sessions[agent_name] = apply_session_telemetry_event(entry, telemetry)
            self._flush_locked()
            return _clone(self._sessions[agent_name])

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

    def record_boundary(
        self,
        agent_name: str,
        transport: str = "",
        transport_session_name: str = "",
        transport_backend: str = "",
        workspace_root: str = "",
        workspace_workdir: str = "",
        workspace_home_dir: str = "",
        workspace_target_dir: str = "",
        workspace_tmp_dir: str = "",
        workspace_scope: str = "",
        workspace_isolation_active: bool = False,
        retained_for_reuse: bool = False,
        reuse_authorized: bool = False,
        transport_reuse_count: Optional[int] = None,
    ) -> Dict[str, Any]:
        with self._lock:
            name = str(agent_name)
            entry = self._sessions.get(name)
            if entry is None:
                entry = _normalize_session_entry(name, {})
                self._sessions[name] = entry
            if transport:
                entry["transport"] = str(transport)
            if transport_session_name:
                entry["transport_session_name"] = str(transport_session_name)
            if transport_backend:
                entry["transport_backend"] = str(transport_backend)
            if workspace_root:
                entry["workspace_root"] = str(workspace_root)
            if workspace_workdir:
                entry["workspace_workdir"] = str(workspace_workdir)
            if workspace_home_dir:
                entry["workspace_home_dir"] = str(workspace_home_dir)
            if workspace_target_dir:
                entry["workspace_target_dir"] = str(workspace_target_dir)
            if workspace_tmp_dir:
                entry["workspace_tmp_dir"] = str(workspace_tmp_dir)
            if workspace_scope:
                entry["workspace_scope"] = str(workspace_scope)
            entry["workspace_isolation_active"] = bool(workspace_isolation_active)
            entry["retained_for_reuse"] = bool(retained_for_reuse)
            entry["reuse_authorized"] = bool(reuse_authorized)
            if transport_reuse_count is not None:
                entry["transport_reuse_count"] = max(0, int(transport_reuse_count))
            entry["last_active_at"] = utc_now()
            self._flush_locked()
            return _clone(entry)

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
    host_enforcement = build_host_enforcement_snapshot(shared_state=shared_state)
    host = host_enforcement.get("host", {})
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
    host_native_session_active = bool(host_enforcement.get("host_native_session_active", False))
    host_native_workspace_active = bool(host_enforcement.get("host_native_workspace_active", False))
    host_session_enforcement = str(host_enforcement.get("session_enforcement", "") or "runtime_managed")
    host_workspace_enforcement = str(host_enforcement.get("workspace_enforcement", "") or "runtime_managed")
    for session in teammate_sessions.get("sessions", []):
        if not isinstance(session, Mapping):
            continue
        transport = str(session.get("transport", "") or "unknown")
        transport_backend = str(session.get("transport_backend", "") or "")
        notes: List[str] = []
        workspace_isolation_active = bool(session.get("workspace_isolation_active", False))
        if (
            host_native_session_active
            and transport == "host"
            and transport_backend in {"", "host_native"}
        ):
            boundary_mode = "host_native_session"
            boundary_strength = "strong"
            isolation_source = "host"
            notes.append("session_isolation_backed_by_host_transport")
        elif transport == "host" and transport_backend == "external_process":
            boundary_mode = "worker_subprocess_session"
            boundary_strength = "medium"
            isolation_source = "transport"
            notes.append("session_isolation_backed_by_host_external_process")
        elif transport == "tmux" or transport.startswith("tmux"):
            boundary_mode = "tmux_worker_session"
            boundary_strength = "medium"
            isolation_source = "transport"
            if transport == "tmux":
                notes.append("session_isolation_backed_by_tmux_process")
            else:
                notes.append("session_isolation_backed_by_tmux_fallback_transport")
        elif transport == "host" and transport_backend == "inprocess_thread":
            boundary_mode = "runtime_emulated_session"
            boundary_strength = "emulated"
            isolation_source = "runtime"
            notes.append("host_transport_requested_but_task_ran_inline")
        elif workspace_isolation_active or transport == "subprocess":
            boundary_mode = "worker_subprocess_session"
            boundary_strength = "medium"
            isolation_source = "transport"
            notes.append("session_isolation_backed_by_worker_subprocess")
        else:
            boundary_mode = "runtime_emulated_session"
            boundary_strength = "emulated"
            isolation_source = "runtime"
            notes.append("session_isolation_backed_by_shared_runtime")
        if not host_workspace_isolation:
            notes.append("workspace_isolation_unavailable")
        if not host_independent_sessions:
            notes.append("host_independent_sessions_unavailable")
        if host_independent_sessions and not host_native_session_active:
            notes.append("host_independent_sessions_advertised_only")
        if host_workspace_isolation and not host_native_workspace_active:
            notes.append("host_workspace_isolation_advertised_only")
        if workspace_isolation_active:
            notes.append("session_workspace_scoped_tmpdir")
        if str(session.get("workspace_workdir", "") or ""):
            notes.append("session_workspace_scoped_workdir")
        if str(session.get("workspace_home_dir", "") or ""):
            notes.append("session_workspace_scoped_home")
        if str(session.get("workspace_target_dir", "") or ""):
            notes.append("session_workspace_scoped_target_dir")
        record = {
            "agent": str(session.get("agent", "") or ""),
            "session_id": str(session.get("session_id", "") or ""),
            "transport": transport,
            "transport_session_name": str(session.get("transport_session_name", "") or ""),
            "transport_backend": transport_backend,
            "boundary_mode": boundary_mode,
            "boundary_strength": boundary_strength,
            "isolation_source": isolation_source,
            "host_kind": str(host.get("kind", "") or ""),
            "host_session_transport": host_session_transport,
            "host_independent_sessions": host_independent_sessions,
            "host_workspace_isolation": host_workspace_isolation,
            "host_session_enforcement": host_session_enforcement,
            "host_workspace_enforcement": host_workspace_enforcement,
            "host_native_session_active": host_native_session_active,
            "host_native_workspace_active": host_native_workspace_active,
            "workspace_root": str(session.get("workspace_root", "") or ""),
            "workspace_workdir": str(session.get("workspace_workdir", "") or ""),
            "workspace_home_dir": str(session.get("workspace_home_dir", "") or ""),
            "workspace_target_dir": str(session.get("workspace_target_dir", "") or ""),
            "workspace_tmp_dir": str(session.get("workspace_tmp_dir", "") or ""),
            "workspace_scope": str(session.get("workspace_scope", "") or ""),
            "workspace_isolation_active": workspace_isolation_active,
            "transport_reuse_count": int(session.get("transport_reuse_count", 0) or 0),
            "reuse_authorized": bool(session.get("reuse_authorized", False)),
            "retained_for_reuse": bool(session.get("retained_for_reuse", False)),
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
        "host_runtime_enforcement": {
            "requested_teammate_mode": str(host_enforcement.get("requested_teammate_mode", "") or "in-process"),
            "session_enforcement": host_session_enforcement,
            "workspace_enforcement": host_workspace_enforcement,
            "host_native_session_active": host_native_session_active,
            "host_native_workspace_active": host_native_workspace_active,
            "effective_boundary_source": str(host_enforcement.get("effective_boundary_source", "") or "runtime"),
            "effective_boundary_strength": str(host_enforcement.get("effective_boundary_strength", "") or "emulated"),
            "notes": list(host_enforcement.get("notes", [])) if isinstance(host_enforcement.get("notes", []), list) else [],
        },
        "session_count": len(session_boundaries),
        "boundary_mode_counts": boundary_mode_counts,
        "boundary_strength_counts": boundary_strength_counts,
        "sessions": session_boundaries,
    }
