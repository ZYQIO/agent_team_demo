from __future__ import annotations

import json
import threading
import uuid
from typing import Any, Dict, List, Mapping, Optional

from ..config import RuntimeConfig
from ..core import AgentProfile, Message, SharedState, Task, utc_now


TEAMMATE_SESSIONS_KEY = "teammate_sessions"
TEAMMATE_SESSIONS_FILENAME = "teammate_sessions.json"
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
    if isinstance(raw_sessions, Mapping):
        for agent_name, entry in sorted(raw_sessions.items()):
            if not isinstance(entry, Mapping):
                continue
            normalized = _normalize_session_entry(str(agent_name), entry)
            status = str(normalized.get("status", "") or "unknown")
            transport = str(normalized.get("transport", "") or "unknown")
            status_counts[status] = status_counts.get(status, 0) + 1
            transport_counts[transport] = transport_counts.get(transport, 0) + 1
            sessions.append(normalized)
    return {
        "generated_at": utc_now(),
        "session_count": len(sessions),
        "status_counts": status_counts,
        "transport_counts": transport_counts,
        "sessions": sessions,
    }
