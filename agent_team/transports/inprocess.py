from __future__ import annotations

import json
import pathlib
import threading
import time
from types import ModuleType
from typing import Any, Callable, Dict, List, Mapping, Optional

from ..core import (
    HOOK_EVENT_TEAMMATE_IDLE,
    TEAMMATE_IDLE_HOOK_INTERVAL_SEC,
    Message,
    SharedState,
    Task,
    task_from_dict,
)
from ..runtime.session_state import SESSION_TELEMETRY_SUBJECT, apply_session_telemetry_event
from ..runtime.task_context import ScopedSharedState, build_task_context_snapshot
from ..runtime.lead_interaction import (
    LEAD_STATUS_REPLY_SUBJECT,
    LEAD_STATUS_REQUEST_SUBJECT,
    plan_approval_required,
    queue_plan_approval_request,
)
from ..runtime.task_mutations import apply_task_mutation_payload
from . import tmux as tmux_transport


SUBPROCESS_REVIEWER_TASK_TYPES = set(tmux_transport.SUBPROCESS_REVIEWER_TASK_TYPES)
MAILBOX_REVIEWER_TASK_TYPES = set(tmux_transport.MAILBOX_REVIEWER_TASK_TYPES)
TMUX_REVIEWER_TASK_TYPES = set(tmux_transport.TMUX_REVIEWER_EXTERNAL_TASK_TYPES)
HOST_SESSION_ASSIGNED_TASK_TYPES = (
    MAILBOX_REVIEWER_TASK_TYPES
    | SUBPROCESS_REVIEWER_TASK_TYPES
    | set(tmux_transport.TMUX_ANALYST_TASK_TYPES)
)
HOST_SESSION_MUTATION_TASK_TYPES = {"dynamic_planning", "repo_dynamic_planning"}
HOST_SESSION_WORKER_PAYLOAD_TASK_TYPES = (
    set(tmux_transport.TMUX_ANALYST_TASK_TYPES)
    | HOST_SESSION_MUTATION_TASK_TYPES
)
SESSION_TASK_ASSIGNMENT_SUBJECT = "session_task_assignment"
SESSION_TASK_RESULT_SUBJECT = "session_task_result"
SESSION_CONTROL_SUBJECT = "session_control"
AUTO_REPLY_SUBJECTS = {
    "peer_challenge_round1_request",
    "peer_challenge_round2_request",
    "peer_challenge_round3_request",
    "evidence_request",
    LEAD_STATUS_REQUEST_SUBJECT,
}


class InProcessTeammateAgent(threading.Thread):
    def __init__(
        self,
        context: Any,
        stop_event: threading.Event,
        handlers: Mapping[str, Callable[[Any, Task], Dict[str, Any]]],
        get_lead_name_fn: Callable[[Any], str],
        profile_has_skill_fn: Callable[[Any, str], bool],
        traceback_module: ModuleType,
        claim_tasks: bool = True,
    ) -> None:
        super().__init__(name=context.profile.name, daemon=True)
        self.context = context
        self.stop_event = stop_event
        self.claim_tasks = claim_tasks
        self._handlers = handlers
        self._get_lead_name_fn = get_lead_name_fn
        self._profile_has_skill_fn = profile_has_skill_fn
        self._traceback_module = traceback_module
        self._local_memory: List[Dict[str, str]] = []
        self._assigned_task_lock = threading.Lock()
        self._assigned_task_active = False
        self._assigned_task_id = ""
        self._assigned_task_contexts: Dict[str, Dict[str, Any]] = {}
        self._refresh_session_state()

    def can_accept_assigned_task(self) -> bool:
        if self.claim_tasks or self.stop_event.is_set():
            return False
        with self._assigned_task_lock:
            return (not self._assigned_task_active) and (not self._assigned_task_id)

    def reserve_assigned_task(self, task_id: str) -> bool:
        if self.claim_tasks or self.stop_event.is_set():
            return False
        normalized_task_id = str(task_id or "")
        if not normalized_task_id:
            return False
        with self._assigned_task_lock:
            if self._assigned_task_active or self._assigned_task_id:
                return False
            self._assigned_task_id = normalized_task_id
        return True

    def release_assigned_task(self, task_id: str = "") -> None:
        normalized_task_id = str(task_id or "")
        with self._assigned_task_lock:
            if normalized_task_id and self._assigned_task_id and self._assigned_task_id != normalized_task_id:
                return
            if not self._assigned_task_active:
                self._assigned_task_id = ""

    def _activate_assigned_task(self, task: Task) -> bool:
        normalized_task_id = str(task.task_id or "")
        if not normalized_task_id:
            return False
        with self._assigned_task_lock:
            if self._assigned_task_active:
                return False
            if self._assigned_task_id and self._assigned_task_id != normalized_task_id:
                return False
            self._assigned_task_active = True
            self._assigned_task_id = normalized_task_id
        return True

    def _finish_assigned_task(self, task_id: str = "") -> None:
        normalized_task_id = str(task_id or "")
        with self._assigned_task_lock:
            if normalized_task_id and self._assigned_task_id and self._assigned_task_id != normalized_task_id:
                return
            self._assigned_task_active = False
            self._assigned_task_id = ""

    def _assigned_task_from_message(self, message: Message) -> Optional[Task]:
        if message.subject != SESSION_TASK_ASSIGNMENT_SUBJECT:
            return None
        try:
            payload = json.loads(message.body)
        except json.JSONDecodeError:
            self.context.logger.log(
                "assigned_task_message_invalid",
                agent=self.context.profile.name,
                task_id=message.task_id,
                error="invalid_json",
            )
            self.release_assigned_task(message.task_id or "")
            return None
        task_payload = payload
        if isinstance(payload, dict) and isinstance(payload.get("task"), dict):
            task_payload = payload.get("task", {})
        if not isinstance(task_payload, dict):
            self.context.logger.log(
                "assigned_task_message_invalid",
                agent=self.context.profile.name,
                task_id=message.task_id,
                error="missing_task_payload",
            )
            self.release_assigned_task(message.task_id or "")
            return None
        assigned_task = task_from_dict(task_payload)
        if not assigned_task.task_id:
            self.context.logger.log(
                "assigned_task_message_invalid",
                agent=self.context.profile.name,
                task_id=message.task_id,
                error="empty_task_id",
            )
            self.release_assigned_task(message.task_id or "")
            return None
        task_context = payload.get("task_context", {}) if isinstance(payload, dict) else {}
        if isinstance(task_context, dict):
            self._assigned_task_contexts[assigned_task.task_id] = task_context
        self.context.logger.log(
            "assigned_task_message_received",
            agent=self.context.profile.name,
            task_id=assigned_task.task_id,
            task_type=assigned_task.task_type,
            sender=message.sender,
        )
        return assigned_task

    def _consume_assigned_task_context(self, task_id: str) -> Dict[str, Any]:
        if not task_id:
            return {}
        raw_task_context = self._assigned_task_contexts.pop(str(task_id), {})
        if not isinstance(raw_task_context, dict):
            return {}
        return raw_task_context

    def _merge_task_context_shared_state(
        self,
        original_shared_state: Any,
        task_context: Dict[str, Any],
    ) -> Any:
        visible_state = task_context.get("visible_shared_state", {})
        if not isinstance(visible_state, dict) or not visible_state:
            return original_shared_state
        if not isinstance(original_shared_state, SharedState):
            return original_shared_state
        merged_state = SharedState()
        try:
            baseline = original_shared_state.snapshot()
        except AttributeError:
            baseline = {}
        if isinstance(baseline, dict):
            for key, value in baseline.items():
                merged_state.set(str(key), value)
        for key, value in visible_state.items():
            merged_state.set(str(key), value)
        return merged_state

    def _handle_session_control_message(self, message: Message) -> None:
        if message.subject != SESSION_CONTROL_SUBJECT:
            return
        command = str(message.body or "").strip().lower()
        try:
            payload = json.loads(message.body)
        except json.JSONDecodeError:
            payload = {}
        if isinstance(payload, dict):
            command = str(payload.get("command", command) or command).strip().lower()
        if command == "stop":
            self.stop_event.set()
            self.context.logger.log(
                "session_control_received",
                agent=self.context.profile.name,
                task_id=message.task_id,
                command=command,
            )

    def _run_assigned_task(self, task: Task) -> None:
        try:
            self._run_task(task)
        finally:
            self._finish_assigned_task(task.task_id)

    def _uses_host_session_telemetry_contract(self) -> bool:
        return (not self.claim_tasks) and self.context.runtime_config.teammate_mode == "host"

    def _uses_host_session_result_contract(self, task: Task, task_transport: str) -> bool:
        return (
            (not self.claim_tasks)
            and task_transport == "host"
            and task.task_type in HOST_SESSION_ASSIGNED_TASK_TYPES
        )

    def _publish_assigned_task_result(
        self,
        task: Task,
        success: bool,
        result: Any = None,
        error: str = "",
        state_updates: Optional[Dict[str, Any]] = None,
        task_mutations: Optional[Dict[str, Any]] = None,
    ) -> None:
        normalized_state_updates = state_updates if isinstance(state_updates, dict) else {}
        normalized_task_mutations = task_mutations if isinstance(task_mutations, dict) else {}
        normalized_result = result if isinstance(result, dict) else {"raw_result": result}
        payload = {
            "contract": "session_task_result",
            "contract_version": 1,
            "transport": "host",
            "execution_mode": "session_thread",
            "task_id": task.task_id,
            "task_type": task.task_type,
            "worker": self.context.profile.name,
            "success": bool(success),
            "result": normalized_result if success else {},
            "error": "" if success else str(error or "unknown worker error"),
            "state_updates": normalized_state_updates if success else {},
            "task_mutations": normalized_task_mutations if success else {},
        }
        self.context.mailbox.send(
            sender=self.context.profile.name,
            recipient=self._get_lead_name_fn(self.context),
            subject=SESSION_TASK_RESULT_SUBJECT,
            body=json.dumps(payload, ensure_ascii=False),
            task_id=task.task_id,
        )
        self.context.logger.log(
            "assigned_task_result_published",
            agent=self.context.profile.name,
            task_id=task.task_id,
            task_type=task.task_type,
            success=bool(success),
            state_update_keys=sorted(normalized_state_updates.keys()) if success else [],
            insert_task_count=len(normalized_task_mutations.get("insert_tasks", [])) if success else 0,
            add_dependency_count=len(normalized_task_mutations.get("add_dependencies", [])) if success else 0,
        )

    def _refresh_session_state(self) -> None:
        if self._uses_host_session_telemetry_contract():
            current_state = self.context.session_state if isinstance(self.context.session_state, dict) else {}
        else:
            if self.context.session_registry is None:
                return
            self.context.session_state = self.context.session_registry.session_for(self.context.profile.name)
            current_state = self.context.session_state
        raw_memory = current_state.get("provider_memory", [])
        if not isinstance(raw_memory, list):
            self._local_memory = []
            return
        self._local_memory = [
            {
                "topic": str(item.get("topic", "") or ""),
                "reply": str(item.get("reply", "") or ""),
            }
            for item in raw_memory
            if isinstance(item, dict)
        ]

    def _reply_with_provider(
        self,
        topic: str,
        prompt: str,
        fallback_reply: str,
    ) -> str:
        if not self.context.runtime_config.teammate_provider_replies:
            return fallback_reply

        self._refresh_session_state()
        memory_turns = max(1, int(self.context.runtime_config.teammate_memory_turns))
        recent_memory = self._local_memory[-memory_turns:]
        memory_text = "\n".join(
            [f"- [{item.get('topic', 'unknown')}] {item.get('reply', '')[:180]}" for item in recent_memory]
        )
        if not memory_text:
            memory_text = "- none"

        system_prompt = (
            "You are a teammate analyst in a multi-agent workflow. "
            "Return one concise paragraph with concrete, testable recommendations."
        )
        user_prompt = (
            f"Agent: {self.context.profile.name}\n"
            f"Agent type: {self.context.profile.agent_type}\n"
            f"Topic: {topic}\n"
            "Recent local memory:\n"
            f"{memory_text}\n\n"
            "Task prompt:\n"
            f"{prompt}\n"
            "Output style: concise, specific, and directly actionable."
        )
        try:
            generated = self.context.provider.complete(system_prompt=system_prompt, user_prompt=user_prompt).strip()
            if not generated:
                return fallback_reply
            if self.context.session_registry is not None or self._uses_host_session_telemetry_contract():
                self._record_session_provider_reply(topic=topic, reply=generated, memory_turns=memory_turns)
            else:
                self._local_memory.append({"topic": topic, "reply": generated})
                self._local_memory = self._local_memory[-memory_turns:]
            self.context.logger.log(
                "teammate_provider_reply_generated",
                agent=self.context.profile.name,
                topic=topic,
                provider=self.context.provider.metadata.provider,
                model=self.context.provider.metadata.model,
            )
            self.context.logger.log(
                "teammate_session_memory_updated",
                agent=self.context.profile.name,
                topic=topic,
                memory_turns=memory_turns,
                cached_replies=len(self._local_memory),
            )
            return generated
        except Exception as exc:
            self.context.logger.log(
                "teammate_provider_reply_fallback",
                agent=self.context.profile.name,
                topic=topic,
                error=f"{type(exc).__name__}: {exc}",
            )
            return fallback_reply

    def _build_session_telemetry(self, event_type: str, **fields: Any) -> Dict[str, Any]:
        telemetry = {
            "contract": "session_telemetry",
            "contract_version": 1,
            "transport": "host",
            "agent": self.context.profile.name,
            "agent_type": self.context.profile.agent_type,
            "skills": sorted(self.context.profile.skills),
            "event_type": str(event_type or ""),
        }
        telemetry.update(fields)
        return telemetry

    def _apply_local_session_telemetry(self, event_type: str, **fields: Any) -> Dict[str, Any]:
        telemetry = self._build_session_telemetry(event_type, **fields)
        self.context.session_state = apply_session_telemetry_event(
            self.context.session_state if isinstance(self.context.session_state, dict) else {},
            telemetry,
        )
        self._refresh_session_state()
        return telemetry

    def _publish_session_telemetry(self, telemetry: Mapping[str, Any]) -> None:
        self.context.mailbox.send(
            sender=self.context.profile.name,
            recipient=self._get_lead_name_fn(self.context),
            subject=SESSION_TELEMETRY_SUBJECT,
            body=json.dumps(dict(telemetry), ensure_ascii=False),
            task_id=str(telemetry.get("task_id", "") or None) or None,
        )
        self.context.logger.log(
            "session_telemetry_published",
            agent=self.context.profile.name,
            event_type=str(telemetry.get("event_type", "") or ""),
            task_id=str(telemetry.get("task_id", "") or ""),
        )

    def _record_session_status(
        self,
        transport: str,
        status: str,
        current_task_id: str = "",
        current_task_type: str = "",
    ) -> None:
        if self._uses_host_session_telemetry_contract():
            telemetry = self._apply_local_session_telemetry(
                "status",
                transport=transport,
                status=status,
                current_task_id=current_task_id,
                current_task_type=current_task_type,
            )
            self._publish_session_telemetry(telemetry)
            return
        if self.context.session_registry is not None:
            self.context.session_state = self.context.session_registry.record_status(
                agent_name=self.context.profile.name,
                transport=transport,
                status=status,
                current_task_id=current_task_id,
                current_task_type=current_task_type,
            )

    def _record_session_message_seen(self, message: Message) -> None:
        if self._uses_host_session_telemetry_contract():
            telemetry = self._apply_local_session_telemetry(
                "message_seen",
                from_agent=message.sender,
                subject=message.subject,
                task_id=str(message.task_id or ""),
            )
            self._publish_session_telemetry(telemetry)
            return
        if self.context.session_registry is not None:
            self.context.session_state = self.context.session_registry.record_message_seen(
                agent_name=self.context.profile.name,
                message=message,
            )

    def _build_lead_status_reply_payload(self) -> Dict[str, Any]:
        self._refresh_session_state()
        session_state = self.context.session_state if isinstance(self.context.session_state, dict) else {}
        task_history = session_state.get("task_history", [])
        last_task = task_history[-1] if isinstance(task_history, list) and task_history else {}
        payload = {
            "agent": self.context.profile.name,
            "agent_type": self.context.profile.agent_type,
            "transport": str(session_state.get("transport", "") or "in-process"),
            "status": str(session_state.get("status", "") or "ready"),
            "session_id": str(session_state.get("session_id", "") or ""),
            "current_task_id": str(session_state.get("current_task_id", "") or ""),
            "current_task_type": str(session_state.get("current_task_type", "") or ""),
            "last_task_id": str(last_task.get("task_id", "") or ""),
            "last_task_type": str(last_task.get("task_type", "") or ""),
            "last_task_status": str(last_task.get("status", "") or ""),
            "messages_seen": int(session_state.get("messages_seen", 0) or 0),
            "tasks_completed": int(session_state.get("tasks_completed", 0) or 0),
            "tasks_failed": int(session_state.get("tasks_failed", 0) or 0),
        }
        current_task = payload["current_task_id"] or "none"
        last_task_summary = (
            f"{payload['last_task_id']}({payload['last_task_status'] or 'unknown'})"
            if payload["last_task_id"]
            else "none"
        )
        payload["summary"] = (
            f"{self.context.profile.name} status={payload['status']} "
            f"current_task={current_task} "
            f"last_task={last_task_summary} "
            f"transport={payload['transport']}"
        )
        return payload

    def _auto_reply_lead_status_request(self, message: Message) -> None:
        payload = self._build_lead_status_reply_payload()
        self.context.mailbox.send(
            sender=self.context.profile.name,
            recipient=message.sender,
            subject=LEAD_STATUS_REPLY_SUBJECT,
            body=json.dumps(payload, ensure_ascii=False),
            task_id=str(message.task_id or "") or None,
        )
        self.context.logger.log(
            "lead_status_reply_sent",
            agent=self.context.profile.name,
            recipient=message.sender,
            summary=str(payload.get("summary", "") or ""),
        )

    def _bind_session_task(self, task: Task, transport: str, task_context: Dict[str, Any]) -> None:
        if self._uses_host_session_telemetry_contract():
            telemetry = self._apply_local_session_telemetry(
                "bind_task",
                task_id=task.task_id,
                task_type=task.task_type,
                transport=transport,
                visible_shared_state_keys=list(task_context.get("visible_shared_state_keys", [])),
                visible_shared_state_key_count=int(task_context.get("visible_shared_state_key_count", 0)),
            )
            self._publish_session_telemetry(telemetry)
            return
        if self.context.session_registry is not None:
            self.context.session_state = self.context.session_registry.bind_task(
                agent_name=self.context.profile.name,
                task=task,
                transport=transport,
                task_context=task_context,
            )

    def _record_session_task_result(
        self,
        task: Task,
        transport: str,
        success: bool,
        status: str,
    ) -> None:
        if self._uses_host_session_telemetry_contract():
            telemetry = self._apply_local_session_telemetry(
                "task_result",
                task_id=task.task_id,
                task_type=task.task_type,
                transport=transport,
                success=bool(success),
                status=status,
            )
            self._publish_session_telemetry(telemetry)
            return
        if self.context.session_registry is not None:
            self.context.session_state = self.context.session_registry.record_task_result(
                agent_name=self.context.profile.name,
                task=task,
                transport=transport,
                success=success,
                status=status,
            )

    def _record_session_provider_reply(self, topic: str, reply: str, memory_turns: int) -> None:
        if self._uses_host_session_telemetry_contract():
            telemetry = self._apply_local_session_telemetry(
                "provider_reply",
                topic=topic,
                reply=reply,
                memory_turns=memory_turns,
            )
            self._publish_session_telemetry(telemetry)
            return
        if self.context.session_registry is not None:
            self.context.session_state = self.context.session_registry.record_provider_reply(
                agent_name=self.context.profile.name,
                topic=topic,
                reply=reply,
                memory_turns=memory_turns,
            )
            self._refresh_session_state()

        self._refresh_session_state()
        memory_turns = max(1, int(self.context.runtime_config.teammate_memory_turns))
        recent_memory = self._local_memory[-memory_turns:]
        memory_text = "\n".join(
            [f"- [{item.get('topic', 'unknown')}] {item.get('reply', '')[:180]}" for item in recent_memory]
        )
        if not memory_text:
            memory_text = "- none"

        system_prompt = (
            "You are a teammate analyst in a multi-agent workflow. "
            "Return one concise paragraph with concrete, testable recommendations."
        )
        user_prompt = (
            f"Agent: {self.context.profile.name}\n"
            f"Agent type: {self.context.profile.agent_type}\n"
            f"Topic: {topic}\n"
            "Recent local memory:\n"
            f"{memory_text}\n\n"
            "Task prompt:\n"
            f"{prompt}\n"
            "Output style: concise, specific, and directly actionable."
        )
        try:
            generated = self.context.provider.complete(system_prompt=system_prompt, user_prompt=user_prompt).strip()
            if not generated:
                return fallback_reply
            if self.context.session_registry is not None or self._uses_host_session_telemetry_contract():
                self._record_session_provider_reply(topic=topic, reply=generated, memory_turns=memory_turns)
            else:
                self._local_memory.append({"topic": topic, "reply": generated})
                self._local_memory = self._local_memory[-memory_turns:]
            self.context.logger.log(
                "teammate_provider_reply_generated",
                agent=self.context.profile.name,
                topic=topic,
                provider=self.context.provider.metadata.provider,
                model=self.context.provider.metadata.model,
            )
            self.context.logger.log(
                "teammate_session_memory_updated",
                agent=self.context.profile.name,
                topic=topic,
                memory_turns=memory_turns,
                cached_replies=len(self._local_memory),
            )
            return generated
        except Exception as exc:
            self.context.logger.log(
                "teammate_provider_reply_fallback",
                agent=self.context.profile.name,
                topic=topic,
                error=f"{type(exc).__name__}: {exc}",
            )
            return fallback_reply

    def _task_transport(self, task: Task) -> str:
        if self.context.runtime_config.teammate_mode == "host":
            return "host"
        if (
            self.context.runtime_config.teammate_mode == "tmux"
            and self.context.profile.agent_type == "reviewer"
            and task.task_type in TMUX_REVIEWER_TASK_TYPES
            and getattr(self.context, "runtime_script", None) is not None
        ):
            return "tmux"
        if task.task_type in MAILBOX_REVIEWER_TASK_TYPES:
            return "in-process"
        if (
            self.context.runtime_config.teammate_mode == "subprocess"
            and self.context.profile.agent_type == "reviewer"
            and task.task_type in SUBPROCESS_REVIEWER_TASK_TYPES
            and getattr(self.context, "runtime_script", None) is not None
        ):
            return "subprocess"
        return "in-process"

    def _normalize_task_outcome(self, raw_outcome: Any) -> Dict[str, Any]:
        if isinstance(raw_outcome, dict) and (
            "result" in raw_outcome
            or "state_updates" in raw_outcome
            or "task_mutations" in raw_outcome
        ):
            result = raw_outcome.get("result", {})
            state_updates = raw_outcome.get("state_updates", {})
            task_mutations = raw_outcome.get("task_mutations", {})
        else:
            result = raw_outcome
            state_updates = {}
            task_mutations = {}
        return {
            "result": result if isinstance(result, dict) else {"raw_result": result},
            "state_updates": state_updates if isinstance(state_updates, dict) else {},
            "task_mutations": task_mutations if isinstance(task_mutations, dict) else {},
        }

    def _queue_plan_approval(
        self,
        task: Task,
        task_transport: str,
        task_outcome: Dict[str, Any],
    ) -> Dict[str, Any]:
        request = queue_plan_approval_request(
            shared_state=self.context.shared_state if isinstance(self.context.shared_state, SharedState) else self.context.shared_state._underlying,
            logger=self.context.logger,
            requested_by=self.context.profile.name,
            task_id=task.task_id,
            task_type=task.task_type,
            transport=task_transport,
            result=task_outcome.get("result", {}),
            state_updates=task_outcome.get("state_updates", {}),
            task_mutations=task_outcome.get("task_mutations", {}),
        )
        self.context.mailbox.send(
            sender=self.context.profile.name,
            recipient=self._get_lead_name_fn(self.context),
            subject="plan_approval_requested",
            body=json.dumps(
                {
                    "task_id": task.task_id,
                    "task_type": task.task_type,
                    "requested_by": self.context.profile.name,
                    "transport": task_transport,
                    "proposed_task_ids": list(request.get("proposed_task_ids", [])),
                    "proposed_dependency_ids": list(request.get("proposed_dependency_ids", [])),
                },
                ensure_ascii=False,
            ),
            task_id=task.task_id,
        )
        normalized_result = dict(task_outcome.get("result", {}))
        normalized_result["approval_required"] = True
        normalized_result["mutations_applied"] = False
        normalized_result["proposed_task_ids"] = list(request.get("proposed_task_ids", []))
        normalized_result["proposed_dependency_ids"] = list(request.get("proposed_dependency_ids", []))
        return normalized_result

    def _run_subprocess_worker_task(self, task: Task, task_context: Dict[str, Any]) -> Dict[str, Any]:
        runtime_script = getattr(self.context, "runtime_script", None)
        if runtime_script is None:
            raise RuntimeError("runtime_script unavailable for subprocess worker task")
        visible_shared_state = task_context.get("visible_shared_state", {})
        if not isinstance(visible_shared_state, dict):
            visible_shared_state = {}
        payload = {
            "task_type": task.task_type,
            "task_payload": task.payload,
            "target_dir": str(self.context.target_dir),
            "output_dir": str(self.context.output_dir),
            "goal": self.context.goal,
            "task_context": task_context,
            "shared_state": visible_shared_state,
            "board_snapshot": self.context.board.snapshot(),
            "runtime_config": self.context.runtime_config.to_dict(),
        }
        agent_team_config = visible_shared_state.get("agent_team_config", {})
        if isinstance(agent_team_config, dict):
            model_config = agent_team_config.get("model", {})
            if isinstance(model_config, dict) and model_config:
                payload["model_config"] = dict(model_config)
        if self.context.session_registry is not None:
            payload["session_state"] = dict(self.context.session_state)
        self.context.logger.log("subprocess_worker_task_dispatched", worker=self.context.profile.name, task_id=task.task_id, task_type=task.task_type)
        execution = tmux_transport.run_tmux_worker_task(
            runtime_script=pathlib.Path(runtime_script).resolve(),
            output_dir=self.context.output_dir,
            runtime_config=self.context.runtime_config,
            payload=payload,
            worker_name=self.context.profile.name,
            logger=self.context.logger,
            timeout_sec=int(self.context.runtime_config.tmux_worker_timeout_sec),
        )
        execution_diagnostics = execution.get("diagnostics", {})
        transport_used = str(execution.get("transport", "") or "subprocess")
        if self.context.session_registry is not None and isinstance(execution_diagnostics, dict):
            tmux_transport.record_worker_boundary_from_diagnostics(lead_context=self.context, worker_name=self.context.profile.name, transport=transport_used, execution_diagnostics=execution_diagnostics)
        if not execution.get("ok"):
            error = str(execution.get("error", "unknown worker error"))
            self.context.logger.log("subprocess_worker_task_failed", worker=self.context.profile.name, task_id=task.task_id, task_type=task.task_type, transport=transport_used, error=error)
            raise RuntimeError(error)
        worker_payload = execution.get("payload", {})
        if not isinstance(worker_payload, dict):
            worker_payload = {}
        self.context.logger.log("subprocess_worker_task_completed", worker=self.context.profile.name, task_id=task.task_id, task_type=task.task_type, transport=transport_used)
        return self._normalize_task_outcome(worker_payload)

    def _run_tmux_external_worker_task(
        self,
        task: Task,
        task_context: Dict[str, Any],
    ) -> Dict[str, Any]:
        runtime_script = getattr(self.context, "runtime_script", None)
        if runtime_script is None:
            raise RuntimeError("runtime_script unavailable for tmux worker task")
        self.context.logger.log(
            "tmux_worker_task_dispatched",
            worker=self.context.profile.name,
            task_id=task.task_id,
            task_type=task.task_type,
            execution_mode="reviewer_thread",
        )
        execution = tmux_transport.run_external_tmux_task(
            context=self.context,
            task=task,
            runtime_script=pathlib.Path(runtime_script).resolve(),
            task_context=task_context,
            record_boundary=self.context.session_registry is not None,
            timeout_sec=int(self.context.runtime_config.tmux_worker_timeout_sec),
        )
        transport_used = str(execution.get("transport", "") or "tmux")
        if not execution.get("ok"):
            error = str(execution.get("error", "unknown worker error"))
            self.context.logger.log(
                "tmux_worker_task_failed",
                worker=self.context.profile.name,
                task_id=task.task_id,
                task_type=task.task_type,
                transport=transport_used,
                error=error,
                execution_mode="reviewer_thread",
            )
            raise RuntimeError(error)
        worker_payload = execution.get("payload", {})
        if not isinstance(worker_payload, dict):
            worker_payload = {}
        self.context.logger.log(
            "tmux_worker_task_completed",
            worker=self.context.profile.name,
            task_id=task.task_id,
            task_type=task.task_type,
            transport=transport_used,
            execution_mode="reviewer_thread",
        )
        return self._normalize_task_outcome(worker_payload)

    def _run_host_assigned_worker_payload_task(self, task: Task, task_context: Dict[str, Any]) -> Dict[str, Any]:
        board_task_ids = task_context.get("board_task_ids", [])
        if not isinstance(board_task_ids, list):
            board_task_ids = []
        model_config: Dict[str, Any] = {}
        agent_team_config = self.context.shared_state.get("agent_team_config", {})
        if isinstance(agent_team_config, dict):
            raw_model_config = agent_team_config.get("model", {})
            if isinstance(raw_model_config, dict):
                model_config = dict(raw_model_config)
        payload = {
            "task_type": task.task_type,
            "task_payload": task.payload,
            "goal": self.context.goal,
            "target_dir": str(pathlib.Path(self.context.target_dir).resolve()),
            "output_dir": str(pathlib.Path(self.context.output_dir).resolve()),
            "task_context": task_context,
            "board_snapshot": {
                "tasks": [
                    {"task_id": str(task_id)}
                    for task_id in board_task_ids
                    if str(task_id)
                ]
            },
            "runtime_config": self.context.runtime_config.to_dict(),
            "model_config": model_config,
        }
        worker_payload = tmux_transport.run_tmux_worker_payload(payload)
        if not isinstance(worker_payload, dict):
            raise RuntimeError(f"invalid host assigned worker payload for task_type={task.task_type}")
        return worker_payload

    def _run_task(self, task: Task) -> None:
        lock_paths = [str(pathlib.Path(path).resolve()) for path in task.locked_paths]
        if lock_paths and not self.context.file_locks.acquire(self.context.profile.name, lock_paths):
            self.context.board.defer(task_id=task.task_id, owner=self.context.profile.name, reason="file lock unavailable")
            time.sleep(0.1)
            return
        self.context.logger.log("task_started", task_id=task.task_id, agent=self.context.profile.name, task_type=task.task_type)
        self.context.mailbox.send(sender=self.context.profile.name, recipient=self._get_lead_name_fn(self.context), subject="task_started", body=f"{self.context.profile.name} started {task.task_id}", task_id=task.task_id)
        handler = self._handlers.get(task.task_type)
        if handler is None:
            error = f"no handler registered for task_type={task.task_type}"
            self.context.board.fail(task_id=task.task_id, owner=self.context.profile.name, error=error)
            self.context.mailbox.send(sender=self.context.profile.name, recipient=self._get_lead_name_fn(self.context), subject="task_failed", body=error, task_id=task.task_id)
            if lock_paths:
                self.context.file_locks.release(self.context.profile.name, lock_paths)
            return
        original_shared_state = self.context.shared_state
        provided_task_context = self._consume_assigned_task_context(task.task_id)
        task_context = provided_task_context or build_task_context_snapshot(self.context, task)
        task_transport = self._task_transport(task)
        uses_host_session_result_contract = self._uses_host_session_result_contract(
            task=task,
            task_transport=task_transport,
        )
        shared_state_underlying = (
            self._merge_task_context_shared_state(
                original_shared_state=original_shared_state,
                task_context=task_context,
            )
            if provided_task_context
            else original_shared_state
        )
        if hasattr(self.context.board, "apply_task_context"):
            self.context.board.apply_task_context(task_context)
        scoped_shared_state = ScopedSharedState(
            _underlying=shared_state_underlying,
            _visible_keys=set(task_context.get("visible_shared_state_keys", [])),
            _write_through=not uses_host_session_result_contract,
        )
        if self.context.session_registry is not None or self._uses_host_session_telemetry_contract():
            self._bind_session_task(task=task, transport=task_transport, task_context=task_context)
        self.context.task_context = task_context
        self.context.shared_state = scoped_shared_state
        self.context.logger.log("task_context_prepared", agent=self.context.profile.name, task_id=task.task_id, task_type=task.task_type, scope=str(task_context.get("scope", "")), visible_shared_state_keys=list(task_context.get("visible_shared_state_keys", [])), visible_shared_state_key_count=int(task_context.get("visible_shared_state_key_count", 0)), omitted_shared_state_key_count=int(task_context.get("omitted_shared_state_key_count", 0)), dependency_task_ids=list(task_context.get("dependencies", [])), transport=task_transport)
        try:
            host_assigned_payload: Dict[str, Any] = {}
            if uses_host_session_result_contract and task.task_type in HOST_SESSION_WORKER_PAYLOAD_TASK_TYPES:
                host_assigned_payload = self._run_host_assigned_worker_payload_task(task=task, task_context=task_context)
                result = host_assigned_payload.get("result", {})
            else:
                if task_transport == "subprocess":
                    task_outcome = self._run_subprocess_worker_task(
                        task=task,
                        task_context=task_context,
                    )
                elif task_transport == "tmux":
                    task_outcome = self._run_tmux_external_worker_task(
                        task=task,
                        task_context=task_context,
                    )
                else:
                    task_outcome = self._normalize_task_outcome(handler(self.context, task))
            if task_transport not in {"subprocess", "tmux"} and task.task_type in HOST_SESSION_WORKER_PAYLOAD_TASK_TYPES and uses_host_session_result_contract:
                task_outcome = self._normalize_task_outcome(host_assigned_payload)
            if task_transport in {"subprocess", "tmux"}:
                result = dict(task_outcome.get("result", {}))
            else:
                result = dict(task_outcome.get("result", {}))
            if self.context.session_registry is not None or self._uses_host_session_telemetry_contract():
                self._record_session_task_result(task=task, transport=task_transport, success=True, status="ready")
            if uses_host_session_result_contract:
                result_state_updates = (
                    task_outcome.get("state_updates", {})
                    if task.task_type in HOST_SESSION_WORKER_PAYLOAD_TASK_TYPES
                    else scoped_shared_state.buffered_updates()
                )
                result_task_mutations = (
                    task_outcome.get("task_mutations", {})
                    if task.task_type in HOST_SESSION_WORKER_PAYLOAD_TASK_TYPES
                    else {}
                )
                self._publish_assigned_task_result(
                    task=task,
                    success=True,
                    result=result,
                    state_updates=result_state_updates,
                    task_mutations=result_task_mutations,
                )
            else:
                if plan_approval_required(
                    shared_state=original_shared_state,
                    requested_by=self.context.profile.name,
                    task_mutations=task_outcome.get("task_mutations", {}),
                ):
                    result = self._queue_plan_approval(
                        task=task,
                        task_transport=task_transport,
                        task_outcome=task_outcome,
                    )
                else:
                    applied = apply_task_mutation_payload(
                        board=self.context.board,
                        shared_state=original_shared_state,
                        task_type=task.task_type,
                        updated_by=self.context.profile.name,
                        result=task_outcome.get("result", {}),
                        state_updates=task_outcome.get("state_updates", {}),
                        task_mutations=task_outcome.get("task_mutations", {}),
                    )
                    result = applied.get("result", result)
                self.context.board.complete(task_id=task.task_id, owner=self.context.profile.name, result=result)
                self.context.mailbox.send(sender=self.context.profile.name, recipient=self._get_lead_name_fn(self.context), subject="task_completed", body=f"{task.task_id} done", task_id=task.task_id)
        except Exception as exc:  # pragma: no cover - defensive path
            error = f"{type(exc).__name__}: {exc}"
            if self.context.session_registry is not None or self._uses_host_session_telemetry_contract():
                self._record_session_task_result(task=task, transport=task_transport, success=False, status="error")
            if uses_host_session_result_contract:
                self._publish_assigned_task_result(
                    task=task,
                    success=False,
                    error=error,
                )
            else:
                self.context.board.fail(task_id=task.task_id, owner=self.context.profile.name, error=error)
                self.context.mailbox.send(sender=self.context.profile.name, recipient=self._get_lead_name_fn(self.context), subject="task_failed", body=error, task_id=task.task_id)
            self.context.logger.log("task_exception", task_id=task.task_id, agent=self.context.profile.name, traceback=self._traceback_module.format_exc())
        finally:
            self.context.shared_state = original_shared_state
            self.context.task_context = {}
            if lock_paths:
                self.context.file_locks.release(self.context.profile.name, lock_paths)

    def _auto_reply_peer_challenge(self, message: Message) -> None:
        question = message.body
        round_id = 1
        peer_name = ""
        peer_reply = ""
        try:
            parsed = json.loads(message.body)
            if isinstance(parsed, dict):
                question = str(parsed.get("question", message.body))
                round_id = int(parsed.get("round", 1))
                peer_name = str(parsed.get("peer_name", ""))
                peer_reply = str(parsed.get("peer_round1_reply", parsed.get("peer_round2_reply", "")))
        except json.JSONDecodeError:
            pass

        heading_issues = self.context.shared_state.get("heading_issues", [])
        length_issues = self.context.shared_state.get("length_issues", [])
        is_heading_specialist = self._profile_has_skill_fn(self.context.profile, "inventory")
        is_length_specialist = (
            self.context.profile.agent_type == "analyst" and not is_heading_specialist
        )
        if round_id == 1:
            if is_heading_specialist:
                reply = (
                    f"Concern on question '{question}': heading audit may miss files with non-standard markdown "
                    f"heading style. Suggest adding regex fallback and markdown lint rules. "
                    f"Current heading-gap files={len(heading_issues)}."
                )
            elif is_length_specialist:
                reply = (
                    f"Concern on question '{question}': line-count threshold is static and may over/under flag files. "
                    f"Suggest percentile-based threshold plus topic density score. "
                    f"Current long-file findings={len(length_issues)}."
                )
            else:
                reply = (
                    f"Concern on question '{question}': combine heading and length checks into a weighted quality score."
                )
            response_subject = "peer_challenge_round1_reply"
        else:
            if round_id == 2:
                if is_heading_specialist:
                    reply = (
                        f"Rebuttal to {peer_name}: static-threshold concern is valid, but complexity can be controlled by "
                        f"starting with two-tier thresholds. Improvement: use heading density as second signal. "
                        f"Peer said: {peer_reply[:220]}"
                    )
                elif is_length_specialist:
                    reply = (
                        f"Rebuttal to {peer_name}: heading-style concern is valid, but regex-only rules can create false "
                        f"positives. Improvement: combine parser-based checks with lint config baselines. "
                        f"Peer said: {peer_reply[:220]}"
                    )
                else:
                    reply = (
                        f"Rebuttal to {peer_name}: align both proposals into a single quality score with weighted signals."
                    )
                response_subject = "peer_challenge_round2_reply"
            else:
                if is_heading_specialist:
                    reply = (
                        f"Final proposal for '{question}': implement heading parser + lint fallback, "
                        f"acceptance check = 100% files with at least one heading, rollout in 2 phases. "
                        f"Resolved critique from {peer_name}: {peer_reply[:180]}"
                    )
                elif is_length_specialist:
                    reply = (
                        f"Final proposal for '{question}': switch to percentile thresholds (P85 line count) plus "
                        f"topic-density signal, acceptance check = <5% false positives in pilot. "
                        f"Resolved critique from {peer_name}: {peer_reply[:180]}"
                    )
                else:
                    reply = (
                        f"Final proposal for '{question}': combine both approaches into weighted scoring with CI gates."
                    )
                response_subject = "peer_challenge_round3_reply"

        provider_prompt = (
            f"Question: {question}\n"
            f"Round: {round_id}\n"
            f"Peer name: {peer_name or 'none'}\n"
            f"Peer context: {peer_reply[:260] if peer_reply else 'none'}\n"
            f"Current fallback proposal: {reply}"
        )
        reply = self._reply_with_provider(
            topic=f"peer_challenge_round{round_id}",
            prompt=provider_prompt,
            fallback_reply=reply,
        )

        self.context.mailbox.send(
            sender=self.context.profile.name,
            recipient=message.sender,
            subject=response_subject,
            body=reply,
            task_id=message.task_id,
        )
        self.context.logger.log(
            "peer_challenge_reply_sent",
            sender=self.context.profile.name,
            recipient=message.sender,
            task_id=message.task_id,
        )

    def _auto_reply_evidence_request(self, message: Message) -> None:
        question = message.body
        source_score = "unknown"
        focus_areas: List[str] = []
        peer_name = ""
        peer_objection = ""
        target_previous_reply = ""
        try:
            parsed = json.loads(message.body)
            if isinstance(parsed, dict):
                question = str(parsed.get("question", message.body))
                source_score = str(parsed.get("source_score", "unknown"))
                focus_areas = [str(x) for x in parsed.get("focus_areas", [])]
                peer_name = str(parsed.get("peer_name", ""))
                peer_objection = str(parsed.get("peer_objection", ""))
                target_previous_reply = str(parsed.get("target_previous_reply", ""))
        except json.JSONDecodeError:
            pass

        heading_issues = self.context.shared_state.get("heading_issues", [])
        length_issues = self.context.shared_state.get("length_issues", [])
        if not focus_areas:
            focus_areas = ["depth"]

        role_note = ""
        is_heading_specialist = self._profile_has_skill_fn(self.context.profile, "inventory")
        is_length_specialist = (
            self.context.profile.agent_type == "analyst" and not is_heading_specialist
        )
        if is_heading_specialist:
            role_note = (
                f"Domain: heading quality. Current heading issues={len(heading_issues)} "
                f"(source score={source_score})."
            )
        elif is_length_specialist:
            role_note = (
                f"Domain: file length governance. Current long files={len(length_issues)} "
                f"(source score={source_score})."
            )
        else:
            role_note = f"Domain: synthesis. Source score={source_score}."

        segments: List[str] = [f"Evidence response for question: {question}", role_note]
        if target_previous_reply:
            segments.append(f"Previous proposal context: {target_previous_reply[:200]}")
        if "coverage" in focus_areas:
            segments.append(
                "Coverage evidence: define explicit acceptance checks, sample size, and pass/fail threshold."
            )
        if "rebuttal" in focus_areas:
            segments.append(
                f"Rebuttal evidence: directly address objection from {peer_name or 'peer'}: "
                f"{peer_objection[:180]}"
            )
        if "depth" in focus_areas:
            segments.append(
                "Depth evidence: provide phased rollout timeline, monitoring KPIs, and rollback trigger."
            )
        if is_heading_specialist:
            segments.append(
                "Plan: parser+linter dual validation; KPI=100% files with top-level heading; rollback if lint noise >20%."
            )
        elif is_length_specialist:
            segments.append(
                "Plan: percentile threshold (P85) pilot; KPI=false positives <5%; rollback if >10%."
            )
        else:
            segments.append("Plan: combine both tracks into staged rollout with CI quality gates.")
        reply = " ".join(segments)
        provider_prompt = (
            f"Evidence question: {question}\n"
            f"Focus areas: {', '.join(focus_areas)}\n"
            f"Peer name: {peer_name or 'none'}\n"
            f"Peer objection: {peer_objection[:220] if peer_objection else 'none'}\n"
            f"Previous reply: {target_previous_reply[:220] if target_previous_reply else 'none'}\n"
            f"Current fallback proposal: {reply}"
        )
        reply = self._reply_with_provider(
            topic="evidence_reply",
            prompt=provider_prompt,
            fallback_reply=reply,
        )

        self.context.mailbox.send(
            sender=self.context.profile.name,
            recipient=message.sender,
            subject="evidence_reply",
            body=reply,
            task_id=message.task_id,
        )
        self.context.logger.log(
            "evidence_reply_sent",
            sender=self.context.profile.name,
            recipient=message.sender,
            task_id=message.task_id,
        )

    def run(self) -> None:
        session_transport = "in-process"
        if (
            not self.claim_tasks
            and (
                (
                    self.context.runtime_config.teammate_mode in {"tmux", "subprocess"}
                    and self.context.profile.agent_type == "analyst"
                )
                or self.context.runtime_config.teammate_mode == "host"
            )
        ):
            session_transport = str(self.context.session_state.get("transport", "") or "")
        if self.context.session_registry is not None or self._uses_host_session_telemetry_contract():
            self._record_session_status(
                transport=session_transport,
                status="ready",
            )
        self.context.logger.log(
            "teammate_session_started",
            agent=self.context.profile.name,
            transport=str(self.context.session_state.get("transport", "") or session_transport or "in-process"),
            session_id=str(self.context.session_state.get("session_id", "") or ""),
        )
        self.context.mailbox.send(
            sender=self.context.profile.name,
            recipient=self._get_lead_name_fn(self.context),
            subject="agent_ready",
            body=f"{self.context.profile.name} online with skills {sorted(self.context.profile.skills)}",
        )
        last_idle_hook_emit_ts = 0.0
        while not self.stop_event.is_set():
            if self.claim_tasks:
                messages = self.context.mailbox.pull(self.context.profile.name)
            else:
                messages = self.context.mailbox.pull_matching(
                    self.context.profile.name,
                    lambda message: (
                        message.subject in AUTO_REPLY_SUBJECTS
                        or message.subject == SESSION_TASK_ASSIGNMENT_SUBJECT
                        or message.subject == SESSION_CONTROL_SUBJECT
                    ),
                )
            assigned_task: Optional[Task] = None
            for message in messages:
                if self.context.session_registry is not None or self._uses_host_session_telemetry_contract():
                    self._record_session_message_seen(message)
                self.context.logger.log(
                    "agent_mail_seen",
                    agent=self.context.profile.name,
                    from_agent=message.sender,
                    subject=message.subject,
                )
                if message.subject in {
                    "peer_challenge_round1_request",
                    "peer_challenge_round2_request",
                    "peer_challenge_round3_request",
                }:
                    self._auto_reply_peer_challenge(message)
                if message.subject == "evidence_request":
                    self._auto_reply_evidence_request(message)
                if message.subject == LEAD_STATUS_REQUEST_SUBJECT:
                    self._auto_reply_lead_status_request(message)
                if message.subject == SESSION_TASK_ASSIGNMENT_SUBJECT:
                    assigned_task = self._assigned_task_from_message(message)
                if message.subject == SESSION_CONTROL_SUBJECT:
                    self._handle_session_control_message(message)

            if assigned_task is not None and self._activate_assigned_task(assigned_task):
                self._run_assigned_task(assigned_task)
                continue

            if self.claim_tasks:
                task = self.context.board.claim_next(
                    agent_name=self.context.profile.name,
                    agent_skills=self.context.profile.skills,
                    agent_type=self.context.profile.agent_type,
                )
                if task is not None:
                    self._run_task(task)
                    continue

            if self.context.board.all_terminal():
                break
            now = time.time()
            if now - last_idle_hook_emit_ts >= TEAMMATE_IDLE_HOOK_INTERVAL_SEC:
                self.context.logger.log(HOOK_EVENT_TEAMMATE_IDLE, agent=self.context.profile.name)
                last_idle_hook_emit_ts = now
            time.sleep(0.1)

        self.context.file_locks.release(self.context.profile.name)
        if self.context.session_registry is not None or self._uses_host_session_telemetry_contract():
            self._record_session_status(
                transport=session_transport,
                status="stopped",
            )
        self.context.logger.log(
            "teammate_session_stopped",
            agent=self.context.profile.name,
            transport=str(self.context.session_state.get("transport", "") or session_transport or "in-process"),
            session_id=str(self.context.session_state.get("session_id", "") or ""),
        )
        self.context.mailbox.send(
            sender=self.context.profile.name,
            recipient=self._get_lead_name_fn(self.context),
            subject="agent_stopped",
            body=f"{self.context.profile.name} stopped",
        )
