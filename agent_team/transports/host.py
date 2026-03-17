from __future__ import annotations

import json
import pathlib
import subprocess
import sys
import threading
import time
import traceback
from typing import Any, Dict, Mapping, Optional, Sequence

from ..config import ModelConfig, RuntimeConfig
from ..core import AgentProfile, FileLockRegistry, Mailbox, SharedState, task_from_dict
from ..models import build_provider
from ..runtime.lead_interaction import plan_approval_required, queue_plan_approval_request
from ..runtime.session_state import SESSION_TELEMETRY_SUBJECT
from ..runtime.task_context import ScopedSharedState, build_task_context_snapshot
from ..runtime.task_mutations import apply_task_mutation_payload
from . import tmux as tmux_transport
from .inprocess import (
    HOST_SESSION_ASSIGNED_TASK_TYPES,
    HOST_SESSION_WORKER_PAYLOAD_TASK_TYPES,
    SESSION_CONTROL_SUBJECT,
    SESSION_TASK_ASSIGNMENT_SUBJECT,
    SESSION_TASK_RESULT_SUBJECT,
    InProcessTeammateAgent,
)

HOST_WORKER_THREADS_ATTR = "_host_worker_threads"
HOST_WORKER_RUNTIME_ATTR = "_host_worker_runtime"
HOST_EXTERNAL_WORKER_NAMES_ATTR = "_host_external_worker_names"
HOST_ASSIGNED_TASK_LOCKS_ATTR = "_host_assigned_task_locks"
HOST_WORKER_PAYLOAD_DIRNAME = "_host_session_workers"
HOST_SESSION_BACKEND_EXTERNAL_PROCESS = "external_process"
HOST_SESSION_BACKEND_CODEX_EXEC = "codex_exec"
HOST_SESSION_TASK_RESULT_SCHEMA_FILENAME = "codex_session_result.schema.json"


class _NullLogger:
    def log(self, _event: str, **_fields: Any) -> None:
        return


def _normalize_host_kind(value: str) -> str:
    return str(value or "generic-cli").strip().lower()


def host_session_backend_metadata(host_kind: str = "") -> Dict[str, Any]:
    normalized_kind = _normalize_host_kind(host_kind)
    if normalized_kind == "codex":
        return {
            "backend": HOST_SESSION_BACKEND_CODEX_EXEC,
            "source": "host",
            "host_managed": True,
            "session_isolation_active": True,
            "workspace_isolation_active": False,
        }
    return {
        "backend": HOST_SESSION_BACKEND_EXTERNAL_PROCESS,
        "source": "transport",
        "host_managed": False,
        "session_isolation_active": True,
        "workspace_isolation_active": False,
    }


class _StaticTaskBoard:
    def __init__(self, task_results: Optional[Mapping[str, Any]] = None) -> None:
        self._task_results: Dict[str, Any] = {}
        for task_id, result in dict(task_results or {}).items():
            self._task_results[str(task_id)] = result

    def apply_task_context(self, task_context: Mapping[str, Any]) -> None:
        dependency_results = task_context.get("dependency_results", {})
        if isinstance(dependency_results, Mapping):
            for task_id, result in dependency_results.items():
                self._task_results[str(task_id)] = result
        visible_task_results = task_context.get("visible_task_results", {})
        if isinstance(visible_task_results, Mapping):
            for task_id, result in visible_task_results.items():
                self._task_results[str(task_id)] = result
        visible_state = task_context.get("visible_shared_state", {})
        if isinstance(visible_state, Mapping):
            for key, value in visible_state.items():
                if isinstance(value, Mapping):
                    self._task_results.setdefault(str(key), dict(value))

    def get_task_result(self, task_id: str) -> Optional[Dict[str, Any]]:
        result = self._task_results.get(str(task_id))
        if isinstance(result, dict):
            return dict(result)
        return result

    def snapshot(self) -> Dict[str, Any]:
        return {"tasks": []}

    def all_terminal(self) -> bool:
        return False

    def claim_next(self, agent_name: str, agent_skills: set[str], agent_type: str) -> None:
        del agent_name, agent_skills, agent_type
        return None

    def defer(self, task_id: str, owner: str, reason: str) -> None:
        del task_id, owner, reason

    def complete(self, task_id: str, owner: str, result: Dict[str, Any]) -> None:
        del task_id, owner, result

    def fail(self, task_id: str, owner: str, error: str) -> None:
        del task_id, owner, error


class _HostSessionWorkerProcess:
    worker_backend = HOST_SESSION_BACKEND_EXTERNAL_PROCESS

    def __init__(
        self,
        profile_name: str,
        process: subprocess.Popen[str],
        payload_path: pathlib.Path,
    ) -> None:
        self.profile_name = str(profile_name or "")
        self.process = process
        self.payload_path = pathlib.Path(payload_path)
        self._lock = threading.Lock()
        self._assigned_task_id = ""
        self._stopping = False

    def is_alive(self) -> bool:
        return self.process.poll() is None

    def can_accept_assigned_task(self) -> bool:
        with self._lock:
            return self.is_alive() and (not self._stopping) and (not self._assigned_task_id)

    def reserve_assigned_task(self, task_id: str) -> bool:
        normalized_task_id = str(task_id or "")
        if not normalized_task_id:
            return False
        with self._lock:
            if not self.is_alive() or self._stopping or self._assigned_task_id:
                return False
            self._assigned_task_id = normalized_task_id
        return True

    def release_assigned_task(self, task_id: str = "") -> None:
        normalized_task_id = str(task_id or "")
        with self._lock:
            if normalized_task_id and self._assigned_task_id and self._assigned_task_id != normalized_task_id:
                return
            self._assigned_task_id = ""

    def stop(
        self,
        mailbox: Mailbox,
        lead_name: str,
        logger: Any,
        timeout_sec: float = 5.0,
    ) -> None:
        with self._lock:
            if self._stopping:
                return
            self._stopping = True
        if self.is_alive():
            mailbox.send(
                sender=str(lead_name or "lead"),
                recipient=self.profile_name,
                subject=SESSION_CONTROL_SUBJECT,
                body=json.dumps({"command": "stop"}, ensure_ascii=False),
            )
            logger.log(
                "host_session_worker_stop_requested",
                worker=self.profile_name,
                session_worker_backend=self.worker_backend,
                pid=int(self.process.pid or 0),
            )
            try:
                self.process.wait(timeout=max(0.1, float(timeout_sec)))
            except subprocess.TimeoutExpired:
                self.process.terminate()
                logger.log(
                    "host_session_worker_force_terminated",
                    worker=self.profile_name,
                    session_worker_backend=self.worker_backend,
                    pid=int(self.process.pid or 0),
                )
                try:
                    self.process.wait(timeout=2.0)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait(timeout=2.0)
        try:
            self.payload_path.unlink()
        except OSError:
            pass


def _normalize_task_outcome(raw_outcome: Any) -> Dict[str, Any]:
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


def _merge_task_context_shared_state(
    original_shared_state: Any,
    task_context: Mapping[str, Any],
) -> Any:
    visible_state = task_context.get("visible_shared_state", {})
    if not isinstance(visible_state, Mapping) or not visible_state:
        return original_shared_state
    if not isinstance(original_shared_state, SharedState):
        return original_shared_state
    merged_state = SharedState()
    try:
        baseline = original_shared_state.snapshot()
    except AttributeError:
        baseline = {}
    if isinstance(baseline, Mapping):
        for key, value in baseline.items():
            merged_state.set(str(key), value)
    for key, value in visible_state.items():
        merged_state.set(str(key), value)
    return merged_state


def _run_host_assigned_worker_payload_task(
    context: Any,
    task: Any,
    task_context: Mapping[str, Any],
) -> Dict[str, Any]:
    board_task_ids = task_context.get("board_task_ids", [])
    if not isinstance(board_task_ids, list):
        board_task_ids = []
    model_config: Dict[str, Any] = {}
    agent_team_config = context.shared_state.get("agent_team_config", {})
    if isinstance(agent_team_config, Mapping):
        raw_model_config = agent_team_config.get("model", {})
        if isinstance(raw_model_config, Mapping):
            model_config = dict(raw_model_config)
    payload = {
        "task_type": task.task_type,
        "task_payload": task.payload,
        "goal": context.goal,
        "target_dir": str(pathlib.Path(context.target_dir).resolve()),
        "output_dir": str(pathlib.Path(context.output_dir).resolve()),
        "task_context": dict(task_context),
        "board_snapshot": {
            "tasks": [
                {"task_id": str(task_id)}
                for task_id in board_task_ids
                if str(task_id)
            ]
        },
        "runtime_config": context.runtime_config.to_dict(),
        "model_config": model_config,
    }
    worker_payload = tmux_transport.run_tmux_worker_payload(payload)
    if not isinstance(worker_payload, dict):
        raise RuntimeError(f"invalid host assigned worker payload for task_type={task.task_type}")
    return worker_payload


def _write_host_session_task_result(result_path: pathlib.Path, payload: Mapping[str, Any]) -> None:
    result_path.parent.mkdir(parents=True, exist_ok=True)
    result_path.write_text(
        json.dumps(dict(payload), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _build_host_session_task_result_schema(schema_path: pathlib.Path) -> pathlib.Path:
    if schema_path.exists():
        return schema_path
    schema_path.parent.mkdir(parents=True, exist_ok=True)
    schema_path.write_text(
        json.dumps(
            {
                "type": "object",
                "properties": {
                    "status": {"type": "string", "enum": ["ok", "error"]},
                    "result_path": {"type": "string"},
                    "error": {"type": "string"},
                },
                "required": ["status"],
                "additionalProperties": False,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return schema_path


def _parse_codex_exec_output(stdout: str) -> Dict[str, str]:
    thread_id = ""
    agent_message = ""
    for raw_line in str(stdout or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue
        if str(payload.get("type", "")) == "thread.started":
            thread_id = str(payload.get("thread_id", "") or thread_id)
            continue
        if str(payload.get("type", "")) != "item.completed":
            continue
        item = payload.get("item", {})
        if not isinstance(item, dict):
            continue
        if str(item.get("type", "")) == "agent_message":
            agent_message = str(item.get("text", "") or agent_message)
    return {
        "thread_id": thread_id,
        "agent_message": agent_message,
    }


def _parse_json_object_text(raw_text: str) -> Dict[str, Any]:
    text = str(raw_text or "").strip()
    if not text:
        return {}
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return {}
    if not isinstance(payload, dict):
        return {}
    return payload


def _build_host_session_task_payload(
    lead_context: Any,
    profile: AgentProfile,
    assignment_payload: Mapping[str, Any],
    result_path: pathlib.Path,
) -> Dict[str, Any]:
    payload = _build_host_worker_payload(lead_context=lead_context, profile=profile)
    payload["contract"] = "host_session_task"
    payload["contract_version"] = 1
    payload["assignment"] = dict(assignment_payload)
    payload["result_path"] = str(result_path)
    return payload


def _codex_session_prompt(
    runtime_script: pathlib.Path,
    payload_path: pathlib.Path,
    result_path: pathlib.Path,
    profile_name: str,
) -> str:
    command = (
        f'& "{sys.executable}" "{runtime_script}" '
        f'--host-session-task-file "{payload_path}"'
    )
    return (
        f'You are the persistent Codex teammate session for agent "{profile_name}".\n'
        "Run the exact PowerShell command below and wait for it to finish.\n"
        "Do not change the command.\n"
        f"{command}\n\n"
        f'After the command finishes, read the JSON file at "{result_path}".\n'
        "Reply with exactly one JSON object and nothing else.\n"
        f'If the file exists, reply with {json.dumps({"status": "ok", "result_path": str(result_path)}, ensure_ascii=False)}.\n'
        'If the command fails or the file is missing, reply with {"status":"error","error":"brief reason"}.\n'
    )


class _CodexHostSessionWorker(threading.Thread):
    worker_backend = HOST_SESSION_BACKEND_CODEX_EXEC

    def __init__(
        self,
        lead_context: Any,
        profile: AgentProfile,
    ) -> None:
        super().__init__(name=f"host-codex-{profile.name}", daemon=True)
        self._lead_context = lead_context
        self._profile = profile
        self.profile_name = str(profile.name or "")
        self._stop_event = threading.Event()
        self._lock = threading.Lock()
        self._assigned_task_id = ""
        self._assigned_task_active = False
        session_registry = getattr(lead_context, "session_registry", None)
        existing_session = (
            session_registry.session_for(self.profile_name)
            if session_registry is not None
            else {}
        )
        self._session_id = str(existing_session.get("session_id", "") or "")

    def is_alive(self) -> bool:
        return super().is_alive() and (not self._stop_event.is_set())

    def can_accept_assigned_task(self) -> bool:
        with self._lock:
            return (not self._stop_event.is_set()) and (not self._assigned_task_active) and (not self._assigned_task_id)

    def reserve_assigned_task(self, task_id: str) -> bool:
        normalized_task_id = str(task_id or "")
        if not normalized_task_id:
            return False
        with self._lock:
            if self._stop_event.is_set() or self._assigned_task_active or self._assigned_task_id:
                return False
            self._assigned_task_id = normalized_task_id
        return True

    def release_assigned_task(self, task_id: str = "") -> None:
        normalized_task_id = str(task_id or "")
        with self._lock:
            if normalized_task_id and self._assigned_task_id and self._assigned_task_id != normalized_task_id:
                return
            if not self._assigned_task_active:
                self._assigned_task_id = ""

    def stop(
        self,
        mailbox: Mailbox,
        lead_name: str,
        logger: Any,
        timeout_sec: float = 5.0,
    ) -> None:
        del mailbox, lead_name
        self._stop_event.set()
        logger.log(
            "host_session_worker_stop_requested",
            worker=self.profile_name,
            session_worker_backend=self.worker_backend,
        )
        self.join(timeout=max(0.1, float(timeout_sec)))

    def _activate_assigned_task(self, task_id: str) -> bool:
        normalized_task_id = str(task_id or "")
        if not normalized_task_id:
            return False
        with self._lock:
            if self._assigned_task_active:
                return False
            if self._assigned_task_id and self._assigned_task_id != normalized_task_id:
                return False
            self._assigned_task_active = True
            self._assigned_task_id = normalized_task_id
        return True

    def _finish_assigned_task(self, task_id: str = "") -> None:
        normalized_task_id = str(task_id or "")
        with self._lock:
            if normalized_task_id and self._assigned_task_id and self._assigned_task_id != normalized_task_id:
                return
            self._assigned_task_active = False
            self._assigned_task_id = ""

    def _send_session_telemetry(
        self,
        event_type: str,
        task_id: str = "",
        task_type: str = "",
        success: Optional[bool] = None,
        status: str = "",
        task_context: Optional[Mapping[str, Any]] = None,
    ) -> None:
        payload: Dict[str, Any] = {
            "agent": self.profile_name,
            "agent_type": self._profile.agent_type,
            "skills": sorted(self._profile.skills),
            "event_type": event_type,
            "transport": "host",
            "transport_backend": self.worker_backend,
            "session_id": self._session_id,
            "transport_session_name": f"codex:{self.profile_name}",
            "task_id": str(task_id or ""),
            "task_type": str(task_type or ""),
        }
        if status:
            payload["status"] = str(status)
            payload["current_task_id"] = str(task_id or "")
            payload["current_task_type"] = str(task_type or "")
        if success is not None:
            payload["success"] = bool(success)
            payload["status"] = "ready" if success else "error"
        if isinstance(task_context, Mapping):
            payload["visible_shared_state_keys"] = list(task_context.get("visible_shared_state_keys", []))
            payload["visible_shared_state_key_count"] = int(
                task_context.get(
                    "visible_shared_state_key_count",
                    len(payload["visible_shared_state_keys"]),
                )
                or 0
            )
        self._lead_context.mailbox.send(
            sender=self.profile_name,
            recipient=str(self._lead_context.profile.name or "lead"),
            subject=SESSION_TELEMETRY_SUBJECT,
            body=json.dumps(payload, ensure_ascii=False),
            task_id=str(task_id or ""),
        )

    def _run_codex_task(
        self,
        task_id: str,
        assignment_payload: Mapping[str, Any],
    ) -> Dict[str, Any]:
        worker_dir = pathlib.Path(self._lead_context.output_dir) / HOST_WORKER_PAYLOAD_DIRNAME / self.profile_name
        worker_dir.mkdir(parents=True, exist_ok=True)
        result_path = worker_dir / f"{task_id}.result.json"
        prompt_path = worker_dir / f"{task_id}.payload.json"
        last_message_path = worker_dir / f"{task_id}.message.txt"
        schema_path = _build_host_session_task_result_schema(
            worker_dir / HOST_SESSION_TASK_RESULT_SCHEMA_FILENAME
        )
        runtime_script = pathlib.Path(str(self._lead_context.runtime_script or "")).resolve()
        if not runtime_script.exists():
            raise RuntimeError(f"host runtime_script not found: {runtime_script}")
        task_payload = _build_host_session_task_payload(
            lead_context=self._lead_context,
            profile=self._profile,
            assignment_payload=assignment_payload,
            result_path=result_path,
        )
        prompt_path.write_text(json.dumps(task_payload, ensure_ascii=False), encoding="utf-8")
        if last_message_path.exists():
            last_message_path.unlink()
        if result_path.exists():
            result_path.unlink()
        prompt = _codex_session_prompt(
            runtime_script=runtime_script,
            payload_path=prompt_path,
            result_path=result_path,
            profile_name=self.profile_name,
        )
        command = ["codex", "exec"]
        if self._session_id:
            command.append("resume")
        command.extend(
            [
                "--json",
                "--skip-git-repo-check",
                "--dangerously-bypass-approvals-and-sandbox",
                "--output-schema",
                str(schema_path),
                "--output-last-message",
                str(last_message_path),
            ]
        )
        if self._session_id:
            command.append(self._session_id)
        command.append(prompt)
        completed = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
            cwd=str(pathlib.Path(self._lead_context.target_dir).resolve()),
            timeout=max(60, int(getattr(self._lead_context.runtime_config, "provider_timeout_sec", 60) or 60) * 12),
        )
        parsed_output = _parse_codex_exec_output(completed.stdout)
        thread_id = str(parsed_output.get("thread_id", "") or "")
        if thread_id:
            self._session_id = thread_id
        if last_message_path.exists():
            response_payload = _parse_json_object_text(last_message_path.read_text(encoding="utf-8"))
        else:
            response_payload = _parse_json_object_text(parsed_output.get("agent_message", ""))
        if completed.returncode != 0:
            error = response_payload.get("error", "") if isinstance(response_payload, Mapping) else ""
            raise RuntimeError(
                error
                or f"codex exec failed for {self.profile_name} task {task_id}: {completed.stderr.strip() or completed.stdout.strip()}"
            )
        if str(response_payload.get("status", "") or "") != "ok":
            error = str(response_payload.get("error", "") or "").strip()
            raise RuntimeError(error or f"codex session did not acknowledge task {task_id}")
        resolved_result_path = pathlib.Path(
            str(response_payload.get("result_path", "") or result_path)
        ).resolve()
        if not resolved_result_path.exists():
            raise RuntimeError(f"codex session result file missing: {resolved_result_path}")
        result_payload = json.loads(resolved_result_path.read_text(encoding="utf-8"))
        if not isinstance(result_payload, dict):
            raise RuntimeError(f"invalid codex session result payload for {task_id}")
        return result_payload

    def _handle_assignment(self, message: Any) -> None:
        try:
            payload = json.loads(message.body)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"invalid assignment payload: {exc}") from exc
        if not isinstance(payload, dict):
            raise RuntimeError("invalid assignment payload: missing object")
        task_payload = payload.get("task", {})
        if not isinstance(task_payload, dict):
            raise RuntimeError("invalid assignment payload: missing task")
        task = task_from_dict(task_payload)
        task_context = payload.get("task_context", {})
        if not isinstance(task_context, dict):
            task_context = {}
        self._send_session_telemetry(
            event_type="bind_task",
            task_id=task.task_id,
            task_type=task.task_type,
            task_context=task_context,
        )
        result_payload = self._run_codex_task(task_id=task.task_id, assignment_payload=payload)
        self._send_session_telemetry(
            event_type="status",
            task_id=task.task_id,
            task_type=task.task_type,
            status="running",
        )
        success = bool(result_payload.get("success", False))
        outbound_payload = {
            "contract": "session_task_result",
            "contract_version": 1,
            "transport": "host",
            "execution_mode": "session_thread",
            "task_id": task.task_id,
            "task_type": task.task_type,
            "worker": self.profile_name,
            "success": success,
            "result": result_payload.get("result", {}) if success else {},
            "error": "" if success else str(result_payload.get("error", "") or "unknown worker error"),
            "state_updates": result_payload.get("state_updates", {}) if success else {},
            "task_mutations": result_payload.get("task_mutations", {}) if success else {},
        }
        self._lead_context.mailbox.send(
            sender=self.profile_name,
            recipient=str(self._lead_context.profile.name or "lead"),
            subject=SESSION_TASK_RESULT_SUBJECT,
            body=json.dumps(outbound_payload, ensure_ascii=False),
            task_id=task.task_id,
        )
        self._send_session_telemetry(
            event_type="task_result",
            task_id=task.task_id,
            task_type=task.task_type,
            success=success,
        )

    def run(self) -> None:
        while not self._stop_event.is_set():
            messages = self._lead_context.mailbox.pull_matching(
                self.profile_name,
                lambda message: message.subject in {SESSION_CONTROL_SUBJECT, SESSION_TASK_ASSIGNMENT_SUBJECT},
            )
            handled_any = False
            for message in messages:
                handled_any = True
                if message.subject == SESSION_CONTROL_SUBJECT:
                    self._stop_event.set()
                    continue
                task_id = str(message.task_id or "")
                if not self._activate_assigned_task(task_id):
                    continue
                try:
                    self._handle_assignment(message)
                except Exception as exc:
                    self._lead_context.mailbox.send(
                        sender=self.profile_name,
                        recipient=str(self._lead_context.profile.name or "lead"),
                        subject=SESSION_TASK_RESULT_SUBJECT,
                        body=json.dumps(
                            {
                                "contract": "session_task_result",
                                "contract_version": 1,
                                "transport": "host",
                                "execution_mode": "session_thread",
                                "task_id": task_id,
                                "task_type": "",
                                "worker": self.profile_name,
                                "success": False,
                                "result": {},
                                "error": f"{type(exc).__name__}: {exc}",
                                "state_updates": {},
                                "task_mutations": {},
                            },
                            ensure_ascii=False,
                        ),
                        task_id=task_id,
                    )
                    self._send_session_telemetry(
                        event_type="task_result",
                        task_id=task_id,
                        task_type="",
                        success=False,
                    )
                    self._lead_context.logger.log(
                        "host_session_worker_task_failed",
                        worker=self.profile_name,
                        task_id=task_id,
                        session_worker_backend=self.worker_backend,
                        error=f"{type(exc).__name__}: {exc}",
                    )
                finally:
                    self._finish_assigned_task(task_id)
            if not handled_any:
                time.sleep(0.05)


def _host_transport_identity(lead_context: Any, profile: AgentProfile) -> Dict[str, Any]:
    host_metadata = lead_context.shared_state.get("host", {})
    if not isinstance(host_metadata, Mapping):
        host_metadata = {}
    host_enforcement = lead_context.shared_state.get("host_runtime_enforcement", {})
    if not isinstance(host_enforcement, Mapping):
        host_enforcement = {}
    existing_session = (
        lead_context.session_registry.session_for(profile.name)
        if lead_context.session_registry is not None
        else {}
    )
    session_id = str(existing_session.get("session_id", "") or profile.name)
    host_kind = str(host_metadata.get("kind", "host") or "host")
    session_transport = str(host_metadata.get("session_transport", "host") or "host")
    transport_session_name = f"{host_kind}:{profile.name}"
    worker_backend = _host_worker_backend(
        lead_context=lead_context,
        worker_name=profile.name,
    )
    host_native_backend_active = worker_backend in {"", "host_native"}
    workspace_isolation_active = bool(
        host_enforcement.get("host_native_workspace_active", False)
    ) and host_native_backend_active
    workspace_scope = ""
    workspace_root = ""
    workspace_workdir = ""
    workspace_home_dir = ""
    workspace_target_dir = ""
    workspace_tmp_dir = ""
    if workspace_isolation_active:
        workspace_scope = "host_native_workspace"
        workspace_root = f"host://{host_kind}/sessions/{session_id}"
        workspace_workdir = f"{workspace_root}/workdir"
        workspace_home_dir = f"{workspace_root}/home"
        workspace_target_dir = f"{workspace_root}/target"
        workspace_tmp_dir = f"{workspace_root}/tmp"
    return {
        "host_kind": host_kind,
        "session_transport": session_transport,
        "transport_session_name": transport_session_name,
        "workspace_scope": workspace_scope,
        "workspace_root": workspace_root,
        "workspace_workdir": workspace_workdir,
        "workspace_home_dir": workspace_home_dir,
        "workspace_target_dir": workspace_target_dir,
        "workspace_tmp_dir": workspace_tmp_dir,
        "workspace_isolation_active": workspace_isolation_active,
    }


def _host_worker_thread(lead_context: Any, profile_name: str) -> Any:
    raw_workers = getattr(lead_context, HOST_WORKER_THREADS_ATTR, {})
    if not isinstance(raw_workers, dict):
        return None
    worker = raw_workers.get(str(profile_name))
    if worker is None:
        return None
    if not hasattr(worker, "reserve_assigned_task") or not hasattr(worker, "can_accept_assigned_task"):
        return None
    return worker


def _host_worker_runtime(lead_context: Any) -> Dict[str, Any]:
    raw_runtime = getattr(lead_context, HOST_WORKER_RUNTIME_ATTR, {})
    if not isinstance(raw_runtime, dict):
        return {}
    return dict(raw_runtime)


def _external_worker_names(lead_context: Any) -> set[str]:
    raw_names = getattr(lead_context, HOST_EXTERNAL_WORKER_NAMES_ATTR, set())
    if isinstance(raw_names, set):
        return {str(name) for name in raw_names if str(name)}
    if isinstance(raw_names, (list, tuple)):
        return {str(name) for name in raw_names if str(name)}
    return set()


def _record_host_boundary(
    lead_context: Any,
    profile: AgentProfile,
    transport_identity: Dict[str, Any],
) -> None:
    if lead_context.session_registry is None:
        return
    lead_context.session_registry.record_boundary(
        agent_name=profile.name,
        transport="host",
        transport_session_name=str(transport_identity.get("transport_session_name", "") or ""),
        transport_backend=_host_worker_backend(
            lead_context=lead_context,
            worker_name=profile.name,
        ),
        workspace_root=str(transport_identity.get("workspace_root", "") or ""),
        workspace_workdir=str(transport_identity.get("workspace_workdir", "") or ""),
        workspace_home_dir=str(transport_identity.get("workspace_home_dir", "") or ""),
        workspace_target_dir=str(transport_identity.get("workspace_target_dir", "") or ""),
        workspace_tmp_dir=str(transport_identity.get("workspace_tmp_dir", "") or ""),
        workspace_scope=str(transport_identity.get("workspace_scope", "") or ""),
        workspace_isolation_active=bool(transport_identity.get("workspace_isolation_active", False)),
    )


def _host_completion_identity(lead_context: Any, worker_name: str) -> Dict[str, Any]:
    return _host_transport_identity(
        lead_context=lead_context,
        profile=AgentProfile(name=str(worker_name), skills=set(), agent_type="general"),
    )


def _host_worker_backend(lead_context: Any, worker_name: str) -> str:
    worker = _host_worker_thread(lead_context=lead_context, profile_name=worker_name)
    return str(getattr(worker, "worker_backend", "inprocess_thread") or "inprocess_thread")


def _record_external_mail_sent_if_needed(lead_context: Any, message: Any) -> None:
    if str(message.sender or "") not in _external_worker_names(lead_context=lead_context):
        return
    if str(message.recipient or "") != str(lead_context.profile.name or ""):
        return
    lead_context.logger.log(
        "mail_sent",
        **message.to_dict(),
        delivery_mode="external_host_worker",
    )


def configure_host_session_workers(
    lead_context: Any,
    workflow_pack: str,
    model_config: ModelConfig,
) -> None:
    setattr(
        lead_context,
        HOST_WORKER_RUNTIME_ATTR,
        {
            "workflow_pack": str(workflow_pack or "markdown-audit"),
            "runtime_script": str(lead_context.runtime_script or ""),
            "model_config": model_config.to_dict(),
        },
    )
    setattr(lead_context, HOST_WORKER_THREADS_ATTR, {})
    setattr(lead_context, HOST_EXTERNAL_WORKER_NAMES_ATTR, set())
    setattr(lead_context, HOST_ASSIGNED_TASK_LOCKS_ATTR, {})


def _host_assigned_task_locks(lead_context: Any) -> Dict[str, Dict[str, Any]]:
    raw_locks = getattr(lead_context, HOST_ASSIGNED_TASK_LOCKS_ATTR, None)
    if not isinstance(raw_locks, dict):
        raw_locks = {}
        setattr(lead_context, HOST_ASSIGNED_TASK_LOCKS_ATTR, raw_locks)
    return raw_locks


def _remember_assigned_task_lock(
    lead_context: Any,
    task_id: str,
    owner: str,
    lock_paths: Sequence[str],
) -> None:
    normalized_task_id = str(task_id or "")
    normalized_owner = str(owner or "")
    normalized_paths = [str(pathlib.Path(path).resolve()) for path in lock_paths if str(path)]
    if not normalized_task_id or not normalized_owner or not normalized_paths:
        return
    _host_assigned_task_locks(lead_context=lead_context)[normalized_task_id] = {
        "owner": normalized_owner,
        "paths": normalized_paths,
    }


def _release_assigned_task_lock(lead_context: Any, task_id: str) -> None:
    normalized_task_id = str(task_id or "")
    if not normalized_task_id:
        return
    raw_lock = _host_assigned_task_locks(lead_context=lead_context).pop(normalized_task_id, None)
    if not isinstance(raw_lock, dict):
        return
    owner = str(raw_lock.get("owner", "") or "")
    raw_paths = raw_lock.get("paths", [])
    if not owner or not isinstance(raw_paths, list):
        return
    lock_paths = [str(pathlib.Path(path).resolve()) for path in raw_paths if str(path)]
    if lock_paths:
        lead_context.file_locks.release(owner, lock_paths)


def _apply_host_task_mutations(
    lead_context: Any,
    worker: str,
    task_type: str,
    result: Any,
    state_updates: Any,
    task_mutations: Any,
) -> Dict[str, Any]:
    applied = apply_task_mutation_payload(
        board=lead_context.board,
        shared_state=lead_context.shared_state,
        task_type=task_type,
        updated_by=worker,
        result=result,
        state_updates=state_updates,
        task_mutations=task_mutations,
    )
    return dict(applied.get("result", {}))


def _build_host_worker_payload(
    lead_context: Any,
    profile: AgentProfile,
) -> Dict[str, Any]:
    runtime_meta = _host_worker_runtime(lead_context=lead_context)
    state_snapshot = lead_context.shared_state.snapshot()
    team_profiles = state_snapshot.get("team_profiles", [])
    participant_names = [str(lead_context.profile.name or "lead")]
    if isinstance(team_profiles, list):
        for item in team_profiles:
            if isinstance(item, Mapping):
                name = str(item.get("name", "") or "")
                if name:
                    participant_names.append(name)
    session_state = (
        lead_context.session_registry.session_for(profile.name)
        if lead_context.session_registry is not None
        else {}
    )
    return {
        "contract": "host_session_worker_launch",
        "contract_version": 1,
        "profile": profile.to_dict(),
        "goal": str(lead_context.goal),
        "target_dir": str(lead_context.target_dir),
        "output_dir": str(lead_context.output_dir),
        "runtime_script": str(runtime_meta.get("runtime_script", "") or ""),
        "workflow_pack": str(runtime_meta.get("workflow_pack", "") or "markdown-audit"),
        "runtime_config": lead_context.runtime_config.to_dict(),
        "model_config": dict(runtime_meta.get("model_config", {})),
        "participants": participant_names,
        "mailbox_storage_dir": str(lead_context.mailbox.storage_dir or ""),
        "shared_state": state_snapshot,
        "session_state": session_state,
    }


def _host_kind(lead_context: Any) -> str:
    host_metadata = lead_context.shared_state.get("host", {})
    if not isinstance(host_metadata, Mapping):
        return "generic-cli"
    return _normalize_host_kind(str(host_metadata.get("kind", "") or "generic-cli"))


def _spawn_external_process_host_session_worker(
    lead_context: Any,
    profile: AgentProfile,
) -> _HostSessionWorkerProcess:
    runtime_meta = _host_worker_runtime(lead_context=lead_context)
    runtime_script = pathlib.Path(str(runtime_meta.get("runtime_script", "") or "")).resolve()
    if not runtime_script.exists():
        raise RuntimeError(f"host runtime_script not found: {runtime_script}")
    worker_dir = pathlib.Path(lead_context.output_dir) / HOST_WORKER_PAYLOAD_DIRNAME
    worker_dir.mkdir(parents=True, exist_ok=True)
    payload_path = worker_dir / f"{profile.name}.json"
    payload_path.write_text(
        json.dumps(_build_host_worker_payload(lead_context=lead_context, profile=profile), ensure_ascii=False),
        encoding="utf-8",
    )
    command = [
        sys.executable,
        str(runtime_script),
        "--host-session-worker-file",
        str(payload_path),
    ]
    process = subprocess.Popen(
        command,
        cwd=str(lead_context.output_dir),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    lead_context.logger.log(
        "host_session_worker_started",
        worker=profile.name,
        session_worker_backend=HOST_SESSION_BACKEND_EXTERNAL_PROCESS,
        pid=int(process.pid or 0),
        workflow_pack=str(runtime_meta.get("workflow_pack", "") or "markdown-audit"),
    )
    return _HostSessionWorkerProcess(
        profile_name=profile.name,
        process=process,
        payload_path=payload_path,
    )


def _spawn_codex_host_session_worker(
    lead_context: Any,
    profile: AgentProfile,
) -> _CodexHostSessionWorker:
    runtime_meta = _host_worker_runtime(lead_context=lead_context)
    runtime_script = pathlib.Path(str(runtime_meta.get("runtime_script", "") or "")).resolve()
    if not runtime_script.exists():
        raise RuntimeError(f"host runtime_script not found: {runtime_script}")
    worker = _CodexHostSessionWorker(lead_context=lead_context, profile=profile)
    worker.start()
    lead_context.logger.log(
        "host_session_worker_started",
        worker=profile.name,
        session_worker_backend=HOST_SESSION_BACKEND_CODEX_EXEC,
        workflow_pack=str(runtime_meta.get("workflow_pack", "") or "markdown-audit"),
    )
    return worker


def _spawn_host_session_worker(
    lead_context: Any,
    profile: AgentProfile,
) -> Any:
    if _host_kind(lead_context=lead_context) == "codex":
        return _spawn_codex_host_session_worker(lead_context=lead_context, profile=profile)
    return _spawn_external_process_host_session_worker(lead_context=lead_context, profile=profile)


def ensure_host_session_workers(
    lead_context: Any,
    teammate_profiles: Sequence[AgentProfile],
) -> None:
    existing_workers = getattr(lead_context, HOST_WORKER_THREADS_ATTR, {})
    if not isinstance(existing_workers, dict):
        existing_workers = {}
    active_workers: Dict[str, Any] = {}
    external_names: set[str] = set()
    for name, worker in existing_workers.items():
        if hasattr(worker, "is_alive") and not worker.is_alive():
            lead_context.logger.log(
                "host_session_worker_exited",
                worker=str(name),
                session_worker_backend=str(getattr(worker, "worker_backend", "unknown") or "unknown"),
                exit_code=int(worker.process.poll() or 0) if hasattr(worker, "process") else "",
            )
            try:
                worker.payload_path.unlink()
            except OSError:
                pass
            continue
        active_workers[str(name)] = worker
        if str(getattr(worker, "worker_backend", "") or "") in {
            HOST_SESSION_BACKEND_EXTERNAL_PROCESS,
            HOST_SESSION_BACKEND_CODEX_EXEC,
        }:
            external_names.add(str(name))

    for profile in teammate_profiles:
        if str(profile.name) in active_workers:
            continue
        worker = _spawn_host_session_worker(lead_context=lead_context, profile=profile)
        active_workers[profile.name] = worker
        external_names.add(profile.name)

    setattr(lead_context, HOST_WORKER_THREADS_ATTR, active_workers)
    setattr(lead_context, HOST_EXTERNAL_WORKER_NAMES_ATTR, external_names)


def stop_host_session_workers(lead_context: Any) -> None:
    raw_workers = getattr(lead_context, HOST_WORKER_THREADS_ATTR, {})
    if not isinstance(raw_workers, dict):
        return
    for worker in raw_workers.values():
        if hasattr(worker, "stop"):
            worker.stop(
                mailbox=lead_context.mailbox,
                lead_name=str(lead_context.profile.name or "lead"),
                logger=lead_context.logger,
            )


def run_host_session_worker_entrypoint(payload_file: pathlib.Path) -> int:
    payload = json.loads(pathlib.Path(payload_file).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"invalid host session worker payload: {payload_file}")

    from ..runtime.engine import AgentContext, get_lead_name, profile_has_skill
    from ..workflows import build_workflow_handlers

    runtime_payload = payload.get("runtime_config", {})
    runtime_config = RuntimeConfig(
        **{
            key: value
            for key, value in dict(runtime_payload if isinstance(runtime_payload, dict) else {}).items()
            if key in RuntimeConfig.__annotations__
        }
    )
    model_payload = payload.get("model_config", {})
    if not isinstance(model_payload, dict):
        model_payload = {}
    provider, _ = build_provider(
        provider_name=str(model_payload.get("provider_name", "heuristic") or "heuristic"),
        model=str(model_payload.get("model", "heuristic-v1") or "heuristic-v1"),
        openai_api_key_env=str(model_payload.get("openai_api_key_env", "OPENAI_API_KEY") or "OPENAI_API_KEY"),
        openai_base_url=str(model_payload.get("openai_base_url", "https://api.openai.com/v1") or "https://api.openai.com/v1"),
        require_llm=bool(model_payload.get("require_llm", False)),
        timeout_sec=int(model_payload.get("timeout_sec", 60) or 60),
    )
    profile_payload = payload.get("profile", {})
    profile = AgentProfile(
        name=str(profile_payload.get("name", "") or ""),
        skills={str(skill) for skill in profile_payload.get("skills", [])},
        agent_type=str(profile_payload.get("agent_type", "general") or "general"),
    )
    shared_state = SharedState()
    state_payload = payload.get("shared_state", {})
    if isinstance(state_payload, dict):
        for key, value in state_payload.items():
            shared_state.set(str(key), value)
    logger = _NullLogger()
    mailbox = Mailbox(
        participants=[str(item) for item in payload.get("participants", [])],
        logger=logger,
        storage_dir=pathlib.Path(str(payload.get("mailbox_storage_dir", "") or "")).resolve(),
        clear_storage=False,
    )
    board = _StaticTaskBoard()
    worker_context = AgentContext(
        profile=profile,
        target_dir=pathlib.Path(str(payload.get("target_dir", ".") or ".")).resolve(),
        output_dir=pathlib.Path(str(payload.get("output_dir", ".") or ".")).resolve(),
        goal=str(payload.get("goal", "") or ""),
        provider=provider,
        runtime_config=runtime_config,
        board=board,
        mailbox=mailbox.transport_view(),
        file_locks=FileLockRegistry(logger=logger),
        shared_state=shared_state,
        logger=logger,
        runtime_script=pathlib.Path(str(payload.get("runtime_script", "") or "")).resolve(),
        session_state=dict(payload.get("session_state", {})) if isinstance(payload.get("session_state", {}), dict) else {},
        session_registry=None,
    )
    stop_event = threading.Event()
    worker = InProcessTeammateAgent(
        context=worker_context,
        stop_event=stop_event,
        claim_tasks=False,
        handlers=build_workflow_handlers(str(payload.get("workflow_pack", "markdown-audit") or "markdown-audit")),
        get_lead_name_fn=get_lead_name,
        profile_has_skill_fn=profile_has_skill,
        traceback_module=traceback,
    )
    worker.start()
    worker.join()
    return 0


def run_host_session_task_entrypoint(payload_file: pathlib.Path) -> int:
    payload = json.loads(pathlib.Path(payload_file).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"invalid host session task payload: {payload_file}")

    from ..runtime.engine import AgentContext
    from ..workflows import build_workflow_handlers

    runtime_payload = payload.get("runtime_config", {})
    runtime_config = RuntimeConfig(
        **{
            key: value
            for key, value in dict(runtime_payload if isinstance(runtime_payload, dict) else {}).items()
            if key in RuntimeConfig.__annotations__
        }
    )
    model_payload = payload.get("model_config", {})
    if not isinstance(model_payload, dict):
        model_payload = {}
    provider, _ = build_provider(
        provider_name=str(model_payload.get("provider_name", "heuristic") or "heuristic"),
        model=str(model_payload.get("model", "heuristic-v1") or "heuristic-v1"),
        openai_api_key_env=str(model_payload.get("openai_api_key_env", "OPENAI_API_KEY") or "OPENAI_API_KEY"),
        openai_base_url=str(model_payload.get("openai_base_url", "https://api.openai.com/v1") or "https://api.openai.com/v1"),
        require_llm=bool(model_payload.get("require_llm", False)),
        timeout_sec=int(model_payload.get("timeout_sec", 60) or 60),
    )
    profile_payload = payload.get("profile", {})
    profile = AgentProfile(
        name=str(profile_payload.get("name", "") or ""),
        skills={str(skill) for skill in profile_payload.get("skills", [])},
        agent_type=str(profile_payload.get("agent_type", "general") or "general"),
    )
    shared_state = SharedState()
    state_payload = payload.get("shared_state", {})
    if isinstance(state_payload, dict):
        for key, value in state_payload.items():
            shared_state.set(str(key), value)
    logger = _NullLogger()
    mailbox = Mailbox(
        participants=[str(item) for item in payload.get("participants", [])],
        logger=logger,
        storage_dir=pathlib.Path(str(payload.get("mailbox_storage_dir", "") or "")).resolve(),
        clear_storage=False,
    )
    assignment = payload.get("assignment", {})
    if not isinstance(assignment, dict):
        raise ValueError(f"invalid host session task assignment: {payload_file}")
    task_payload = assignment.get("task", {})
    if not isinstance(task_payload, dict):
        raise ValueError(f"host session task assignment missing task: {payload_file}")
    task = task_from_dict(task_payload)
    if not task.task_id:
        raise ValueError(f"host session task assignment missing task id: {payload_file}")
    task_context = assignment.get("task_context", {})
    if not isinstance(task_context, dict):
        task_context = {}
    result_path = pathlib.Path(str(payload.get("result_path", "") or "")).resolve()
    board = _StaticTaskBoard()
    if hasattr(board, "apply_task_context"):
        board.apply_task_context(task_context)
    worker_context = AgentContext(
        profile=profile,
        target_dir=pathlib.Path(str(payload.get("target_dir", ".") or ".")).resolve(),
        output_dir=pathlib.Path(str(payload.get("output_dir", ".") or ".")).resolve(),
        goal=str(payload.get("goal", "") or ""),
        provider=provider,
        runtime_config=runtime_config,
        board=board,
        mailbox=mailbox.transport_view(),
        file_locks=FileLockRegistry(logger=logger),
        shared_state=shared_state,
        logger=logger,
        runtime_script=pathlib.Path(str(payload.get("runtime_script", "") or "")).resolve(),
        task_context=dict(task_context),
        session_state=dict(payload.get("session_state", {})) if isinstance(payload.get("session_state", {}), dict) else {},
        session_registry=None,
    )
    handlers = build_workflow_handlers(str(payload.get("workflow_pack", "markdown-audit") or "markdown-audit"))
    handler = handlers.get(task.task_type)
    if handler is None:
        _write_host_session_task_result(
            result_path,
            {
                "success": False,
                "result": {},
                "state_updates": {},
                "task_mutations": {},
                "error": f"no handler registered for task_type={task.task_type}",
            },
        )
        return 0

    original_shared_state = worker_context.shared_state
    shared_state_underlying = _merge_task_context_shared_state(
        original_shared_state=original_shared_state,
        task_context=task_context,
    )
    scoped_shared_state = ScopedSharedState(
        _underlying=shared_state_underlying,
        _visible_keys=set(task_context.get("visible_shared_state_keys", [])),
        _write_through=False,
    )
    worker_context.shared_state = scoped_shared_state
    try:
        if task.task_type in HOST_SESSION_WORKER_PAYLOAD_TASK_TYPES:
            task_outcome = _normalize_task_outcome(
                _run_host_assigned_worker_payload_task(
                    context=worker_context,
                    task=task,
                    task_context=task_context,
                )
            )
            state_updates = task_outcome.get("state_updates", {})
            task_mutations = task_outcome.get("task_mutations", {})
        else:
            task_outcome = _normalize_task_outcome(handler(worker_context, task))
            state_updates = scoped_shared_state.buffered_updates()
            task_mutations = task_outcome.get("task_mutations", {})
        _write_host_session_task_result(
            result_path,
            {
                "success": True,
                "result": task_outcome.get("result", {}),
                "state_updates": state_updates,
                "task_mutations": task_mutations,
                "error": "",
            },
        )
    except Exception as exc:
        _write_host_session_task_result(
            result_path,
            {
                "success": False,
                "result": {},
                "state_updates": {},
                "task_mutations": {},
                "error": f"{type(exc).__name__}: {exc}",
            },
        )
    finally:
        worker_context.shared_state = original_shared_state
        worker_context.task_context = {}
    return 0


def apply_host_session_telemetry_message(lead_context: Any, message: Any) -> bool:
    _record_external_mail_sent_if_needed(lead_context=lead_context, message=message)
    try:
        payload = json.loads(message.body)
    except json.JSONDecodeError:
        lead_context.logger.log(
            "host_session_telemetry_invalid",
            worker=message.sender,
            task_id=message.task_id,
            error="invalid_json",
        )
        return False
    if not isinstance(payload, dict):
        lead_context.logger.log(
            "host_session_telemetry_invalid",
            worker=message.sender,
            task_id=message.task_id,
            error="invalid_payload",
        )
        return False
    if lead_context.session_registry is None:
        lead_context.logger.log(
            "host_session_telemetry_skipped",
            worker=str(payload.get("agent", "") or message.sender or ""),
            task_id=str(payload.get("task_id", "") or message.task_id or ""),
            reason="missing_session_registry",
        )
        return False
    try:
        applied_session = lead_context.session_registry.apply_telemetry(payload)
    except Exception as exc:
        lead_context.logger.log(
            "host_session_telemetry_invalid",
            worker=str(payload.get("agent", "") or message.sender or ""),
            task_id=str(payload.get("task_id", "") or message.task_id or ""),
            error=f"{type(exc).__name__}: {exc}",
        )
        return False
    lead_context.logger.log(
        "host_session_telemetry_received",
        worker=str(applied_session.get("agent", "") or message.sender or ""),
        task_id=str(payload.get("task_id", "") or message.task_id or ""),
        event_type=str(payload.get("event_type", "") or ""),
        transport=str(applied_session.get("transport", "") or ""),
        status=str(applied_session.get("status", "") or ""),
        session_worker_backend=_host_worker_backend(
            lead_context=lead_context,
            worker_name=str(applied_session.get("agent", "") or message.sender or ""),
        ),
    )
    return True


def apply_host_session_telemetry_messages(lead_context: Any) -> int:
    telemetry_messages = lead_context.mailbox.pull_matching(
        lead_context.profile.name,
        lambda message: message.subject == SESSION_TELEMETRY_SUBJECT,
    )
    applied = 0
    for message in telemetry_messages:
        applied += 1 if apply_host_session_telemetry_message(lead_context=lead_context, message=message) else 0
    return applied


def apply_host_session_result_message(lead_context: Any, message: Any) -> bool:
    _record_external_mail_sent_if_needed(lead_context=lead_context, message=message)
    try:
        payload = json.loads(message.body)
    except json.JSONDecodeError:
        lead_context.logger.log(
            "host_worker_result_invalid",
            task_id=message.task_id,
            worker=message.sender,
            error="invalid_json",
        )
        return False
    if not isinstance(payload, dict):
        lead_context.logger.log(
            "host_worker_result_invalid",
            task_id=message.task_id,
            worker=message.sender,
            error="invalid_payload",
        )
        return False
    worker = str(payload.get("worker", "") or message.sender or "")
    task_id = str(payload.get("task_id", "") or message.task_id or "")
    task_type = str(payload.get("task_type", "") or "")
    if not worker or not task_id:
        lead_context.logger.log(
            "host_worker_result_invalid",
            task_id=task_id or message.task_id,
            worker=worker or message.sender,
            error="missing_worker_or_task_id",
        )
        return False
    success = bool(payload.get("success", False))
    result = payload.get("result", {})
    if not isinstance(result, dict):
        result = {"raw_result": result}
    state_updates = payload.get("state_updates", {})
    if not isinstance(state_updates, dict):
        state_updates = {}
    task_mutations = payload.get("task_mutations", {})
    if not isinstance(task_mutations, dict):
        task_mutations = {}
    error = str(payload.get("error", "") or "")
    transport_identity = _host_completion_identity(
        lead_context=lead_context,
        worker_name=worker,
    )
    session_worker_backend = _host_worker_backend(
        lead_context=lead_context,
        worker_name=worker,
    )
    lead_context.logger.log(
        "host_worker_result_received",
        worker=worker,
        task_id=task_id,
        task_type=task_type,
        success=success,
        state_update_keys=sorted(state_updates.keys()) if success else [],
        insert_task_count=len(task_mutations.get("insert_tasks", [])) if success else 0,
        add_dependency_count=len(task_mutations.get("add_dependencies", [])) if success else 0,
        completion_subject=SESSION_TASK_RESULT_SUBJECT,
        session_worker_backend=session_worker_backend,
    )
    try:
        if success:
            if plan_approval_required(
                shared_state=lead_context.shared_state,
                requested_by=worker,
                task_mutations=task_mutations,
            ):
                request = queue_plan_approval_request(
                    shared_state=lead_context.shared_state,
                    logger=lead_context.logger,
                    requested_by=worker,
                    task_id=task_id,
                    task_type=task_type,
                    transport="host",
                    result=result,
                    state_updates=state_updates,
                    task_mutations=task_mutations,
                )
                normalized_result = dict(result)
                normalized_result["approval_required"] = True
                normalized_result["mutations_applied"] = False
                normalized_result["proposed_task_ids"] = list(request.get("proposed_task_ids", []))
                normalized_result["proposed_dependency_ids"] = list(request.get("proposed_dependency_ids", []))
                lead_context.mailbox.send(
                    sender=worker,
                    recipient=lead_context.profile.name,
                    subject="plan_approval_requested",
                    body=json.dumps(
                        {
                            "task_id": task_id,
                            "task_type": task_type,
                            "requested_by": worker,
                            "transport": "host",
                            "proposed_task_ids": list(request.get("proposed_task_ids", [])),
                            "proposed_dependency_ids": list(request.get("proposed_dependency_ids", [])),
                        },
                        ensure_ascii=False,
                    ),
                    task_id=task_id,
                )
            else:
                normalized_result = _apply_host_task_mutations(
                    lead_context=lead_context,
                    worker=worker,
                    task_type=task_type,
                    result=result,
                    state_updates=state_updates,
                    task_mutations=task_mutations,
                )
            lead_context.board.complete(task_id=task_id, owner=worker, result=normalized_result)
            lead_context.mailbox.send(
                sender=worker,
                recipient=lead_context.profile.name,
                subject="task_completed",
                body=f"{task_id} done",
                task_id=task_id,
            )
            lead_context.logger.log(
                "host_worker_task_completed",
                worker=worker,
                task_id=task_id,
                task_type=task_type,
                host_kind=str(transport_identity.get("host_kind", "") or ""),
                host_session_transport=str(transport_identity.get("session_transport", "") or ""),
                transport_session_name=str(transport_identity.get("transport_session_name", "") or ""),
                execution_mode="session_thread",
                completion_contract="mailbox_message",
                completion_subject=SESSION_TASK_RESULT_SUBJECT,
                state_update_keys=sorted(state_updates.keys()),
                insert_task_count=len(task_mutations.get("insert_tasks", [])),
                add_dependency_count=len(task_mutations.get("add_dependencies", [])),
                approval_required=bool(normalized_result.get("approval_required", False)),
                session_worker_backend=session_worker_backend,
            )
        else:
            resolved_error = error or "unknown worker error"
            lead_context.board.fail(task_id=task_id, owner=worker, error=resolved_error)
            lead_context.mailbox.send(
                sender=worker,
                recipient=lead_context.profile.name,
                subject="task_failed",
                body=resolved_error,
                task_id=task_id,
            )
            lead_context.logger.log(
                "host_worker_task_failed",
                worker=worker,
                task_id=task_id,
                task_type=task_type,
                error=resolved_error,
                host_kind=str(transport_identity.get("host_kind", "") or ""),
                host_session_transport=str(transport_identity.get("session_transport", "") or ""),
                transport_session_name=str(transport_identity.get("transport_session_name", "") or ""),
                execution_mode="session_thread",
                completion_contract="mailbox_message",
                completion_subject=SESSION_TASK_RESULT_SUBJECT,
                session_worker_backend=session_worker_backend,
            )
    except Exception as exc:
        lead_context.logger.log(
            "host_worker_result_apply_failed",
            worker=worker,
            task_id=task_id,
            task_type=task_type,
            success=success,
            error=f"{type(exc).__name__}: {exc}",
            session_worker_backend=session_worker_backend,
        )
        return False
    finally:
        _release_assigned_task_lock(lead_context=lead_context, task_id=task_id)
        session_worker = _host_worker_thread(lead_context=lead_context, profile_name=worker)
        if session_worker is not None and hasattr(session_worker, "release_assigned_task"):
            session_worker.release_assigned_task(task_id)
    return True


def apply_host_session_result_messages(lead_context: Any) -> int:
    result_messages = lead_context.mailbox.pull_matching(
        lead_context.profile.name,
        lambda message: message.subject == SESSION_TASK_RESULT_SUBJECT,
    )
    applied = 0
    for message in result_messages:
        applied += 1 if apply_host_session_result_message(lead_context=lead_context, message=message) else 0
    return applied


def run_host_teammate_task_once(
    lead_context: Any,
    teammate_profiles: Sequence[AgentProfile],
    handlers: Mapping[str, Any],
) -> bool:
    apply_host_session_telemetry_messages(lead_context=lead_context)
    ran_any = bool(apply_host_session_result_messages(lead_context=lead_context))
    if not teammate_profiles:
        return ran_any
    rr_index = int(lead_context.shared_state.get("_host_rr_index", 0))
    rr_index = rr_index % len(teammate_profiles)
    ordered_profiles = list(teammate_profiles[rr_index:]) + list(teammate_profiles[:rr_index])

    for offset, profile in enumerate(ordered_profiles):
        session_worker = _host_worker_thread(lead_context=lead_context, profile_name=profile.name)
        if session_worker is not None and not session_worker.can_accept_assigned_task():
            continue
        task = lead_context.board.claim_next(
            agent_name=profile.name,
            agent_skills=profile.skills,
            agent_type=profile.agent_type,
        )
        if task is None:
            continue
        next_index = (rr_index + offset + 1) % len(teammate_profiles)
        lead_context.shared_state.set("_host_rr_index", next_index)
        handler = handlers.get(task.task_type)

        if handler is None:
            transport_identity = _host_transport_identity(lead_context=lead_context, profile=profile)
            error = f"no handler registered for task_type={task.task_type}"
            lead_context.board.fail(task_id=task.task_id, owner=profile.name, error=error)
            lead_context.mailbox.send(
                sender=profile.name,
                recipient=lead_context.profile.name,
                subject="task_failed",
                body=error,
                task_id=task.task_id,
            )
            lead_context.logger.log(
                "host_worker_task_failed",
                worker=profile.name,
                task_id=task.task_id,
                task_type=task.task_type,
                error=error,
                host_kind=str(transport_identity.get("host_kind", "") or ""),
                host_session_transport=str(transport_identity.get("session_transport", "") or ""),
                transport_session_name=str(transport_identity.get("transport_session_name", "") or ""),
            )
            return True

        if task.task_type in HOST_SESSION_ASSIGNED_TASK_TYPES:
            ensure_host_session_workers(
                lead_context=lead_context,
                teammate_profiles=teammate_profiles,
            )
            transport_identity = _host_transport_identity(lead_context=lead_context, profile=profile)
            session_worker = _host_worker_thread(lead_context=lead_context, profile_name=profile.name)
            if session_worker is None:
                error = f"no external host session worker available for {profile.name}"
                lead_context.board.fail(task_id=task.task_id, owner=profile.name, error=error)
                lead_context.logger.log(
                    "host_worker_task_failed",
                    worker=profile.name,
                    task_id=task.task_id,
                    task_type=task.task_type,
                    error=error,
                    host_kind=str(transport_identity.get("host_kind", "") or ""),
                    host_session_transport=str(transport_identity.get("session_transport", "") or ""),
                    transport_session_name=str(transport_identity.get("transport_session_name", "") or ""),
                    execution_mode="session_thread",
                    completion_contract="mailbox_message",
                    completion_subject=SESSION_TASK_RESULT_SUBJECT,
                    session_worker_backend="external_process",
                )
                return True
            _record_host_boundary(
                lead_context=lead_context,
                profile=profile,
                transport_identity=transport_identity,
            )
            if not session_worker.reserve_assigned_task(task.task_id):
                lead_context.board.defer(
                    task_id=task.task_id,
                    owner=profile.name,
                    reason="host worker session busy",
                )
                return True
            lock_paths = [str(pathlib.Path(path).resolve()) for path in task.locked_paths]
            if lock_paths and not lead_context.file_locks.acquire(profile.name, lock_paths):
                session_worker.release_assigned_task(task.task_id)
                lead_context.board.defer(
                    task_id=task.task_id,
                    owner=profile.name,
                    reason="file lock unavailable",
                )
                return True
            _remember_assigned_task_lock(
                lead_context=lead_context,
                task_id=task.task_id,
                owner=profile.name,
                lock_paths=lock_paths,
            )
            task_context = build_task_context_snapshot(
                context=lead_context,
                task=task,
                profile=profile,
            )
            lead_context.logger.log(
                "task_context_prepared",
                agent=profile.name,
                task_id=task.task_id,
                task_type=task.task_type,
                scope=str(task_context.get("scope", "")),
                visible_shared_state_keys=list(task_context.get("visible_shared_state_keys", [])),
                visible_shared_state_key_count=int(task_context.get("visible_shared_state_key_count", 0)),
                omitted_shared_state_key_count=int(task_context.get("omitted_shared_state_key_count", 0)),
                dependency_task_ids=list(task_context.get("dependencies", [])),
                transport="host",
            )
            assignment_payload = {
                "contract": "session_task_assignment",
                "contract_version": 1,
                "transport": "host",
                "execution_mode": "session_thread",
                "task": task.to_dict(),
                "task_context": task_context,
            }
            try:
                lead_context.mailbox.send(
                    sender=lead_context.profile.name,
                    recipient=profile.name,
                    subject=SESSION_TASK_ASSIGNMENT_SUBJECT,
                    body=json.dumps(assignment_payload, ensure_ascii=False),
                    task_id=task.task_id,
                )
            except Exception:
                _release_assigned_task_lock(lead_context=lead_context, task_id=task.task_id)
                session_worker.release_assigned_task(task.task_id)
                raise
            lead_context.logger.log(
                "host_worker_task_dispatched",
                worker=profile.name,
                task_id=task.task_id,
                task_type=task.task_type,
                host_kind=str(transport_identity.get("host_kind", "") or ""),
                host_session_transport=str(transport_identity.get("session_transport", "") or ""),
                transport_session_name=str(transport_identity.get("transport_session_name", "") or ""),
                execution_mode="session_thread",
                dispatch_contract="mailbox_message",
                dispatch_subject=SESSION_TASK_ASSIGNMENT_SUBJECT,
                session_worker_backend=_host_worker_backend(
                    lead_context=lead_context,
                    worker_name=profile.name,
                ),
            )
            return True

        lock_paths = [str(pathlib.Path(path).resolve()) for path in task.locked_paths]
        if lock_paths and not lead_context.file_locks.acquire(profile.name, lock_paths):
            lead_context.board.defer(task_id=task.task_id, owner=profile.name, reason="file lock unavailable")
            return True

        transport_identity = _host_transport_identity(lead_context=lead_context, profile=profile)
        lead_context.logger.log(
            "task_started",
            task_id=task.task_id,
            agent=profile.name,
            task_type=task.task_type,
            teammate_mode=lead_context.runtime_config.teammate_mode,
        )
        lead_context.mailbox.send(
            sender=profile.name,
            recipient=lead_context.profile.name,
            subject="task_started",
            body=f"{profile.name} started {task.task_id}",
            task_id=task.task_id,
        )
        task_context = build_task_context_snapshot(
            context=lead_context,
            task=task,
            profile=profile,
        )
        session_state: Dict[str, Any] = {}
        if lead_context.session_registry is not None:
            session_state = lead_context.session_registry.bind_task(
                agent_name=profile.name,
                task=task,
                transport="host",
                task_context=task_context,
            )
            _record_host_boundary(
                lead_context=lead_context,
                profile=profile,
                transport_identity=transport_identity,
            )
            session_state = lead_context.session_registry.session_for(profile.name)

        worker_context = lead_context.__class__(
            profile=profile,
            target_dir=lead_context.target_dir,
            output_dir=lead_context.output_dir,
            goal=lead_context.goal,
            provider=lead_context.provider,
            runtime_config=lead_context.runtime_config,
            board=lead_context.board,
            mailbox=lead_context.mailbox.transport_view(),
            file_locks=lead_context.file_locks,
            shared_state=lead_context.shared_state,
            logger=lead_context.logger,
            runtime_script=lead_context.runtime_script,
            task_context=task_context,
            session_state=session_state,
            session_registry=lead_context.session_registry,
        )
        original_shared_state = worker_context.shared_state
        scoped_shared_state = ScopedSharedState(
            _underlying=original_shared_state,
            _visible_keys=set(task_context.get("visible_shared_state_keys", [])),
        )
        worker_context.shared_state = scoped_shared_state
        lead_context.logger.log(
            "host_worker_task_dispatched",
            worker=profile.name,
            task_id=task.task_id,
            task_type=task.task_type,
            host_kind=str(transport_identity.get("host_kind", "") or ""),
            host_session_transport=str(transport_identity.get("session_transport", "") or ""),
            transport_session_name=str(transport_identity.get("transport_session_name", "") or ""),
            execution_mode="inline",
            dispatch_contract="inline_call",
            session_worker_backend=_host_worker_backend(
                lead_context=lead_context,
                worker_name=profile.name,
            ),
        )
        lead_context.logger.log(
            "task_context_prepared",
            agent=profile.name,
            task_id=task.task_id,
            task_type=task.task_type,
            scope=str(task_context.get("scope", "")),
            visible_shared_state_keys=list(task_context.get("visible_shared_state_keys", [])),
            visible_shared_state_key_count=int(task_context.get("visible_shared_state_key_count", 0)),
            omitted_shared_state_key_count=int(task_context.get("omitted_shared_state_key_count", 0)),
            dependency_task_ids=list(task_context.get("dependencies", [])),
            transport="host",
        )
        try:
            result = handler(worker_context, task)
            if lead_context.session_registry is not None:
                lead_context.session_registry.record_task_result(
                    agent_name=profile.name,
                    task=task,
                    transport="host",
                    success=True,
                    status="ready",
                )
            lead_context.board.complete(task_id=task.task_id, owner=profile.name, result=result)
            lead_context.mailbox.send(
                sender=profile.name,
                recipient=lead_context.profile.name,
                subject="task_completed",
                body=f"{task.task_id} done",
                task_id=task.task_id,
            )
            lead_context.logger.log(
                "host_worker_task_completed",
                worker=profile.name,
                task_id=task.task_id,
                task_type=task.task_type,
                host_kind=str(transport_identity.get("host_kind", "") or ""),
                host_session_transport=str(transport_identity.get("session_transport", "") or ""),
                transport_session_name=str(transport_identity.get("transport_session_name", "") or ""),
                session_worker_backend=_host_worker_backend(
                    lead_context=lead_context,
                    worker_name=profile.name,
                ),
            )
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            if lead_context.session_registry is not None:
                lead_context.session_registry.record_task_result(
                    agent_name=profile.name,
                    task=task,
                    transport="host",
                    success=False,
                    status="error",
                )
            lead_context.board.fail(task_id=task.task_id, owner=profile.name, error=error)
            lead_context.mailbox.send(
                sender=profile.name,
                recipient=lead_context.profile.name,
                subject="task_failed",
                body=error,
                task_id=task.task_id,
            )
            lead_context.logger.log(
                "host_worker_task_failed",
                worker=profile.name,
                task_id=task.task_id,
                task_type=task.task_type,
                error=error,
                host_kind=str(transport_identity.get("host_kind", "") or ""),
                host_session_transport=str(transport_identity.get("session_transport", "") or ""),
                transport_session_name=str(transport_identity.get("transport_session_name", "") or ""),
                traceback=traceback.format_exc(),
                session_worker_backend=_host_worker_backend(
                    lead_context=lead_context,
                    worker_name=profile.name,
                ),
            )
        finally:
            worker_context.shared_state = original_shared_state
            worker_context.task_context = {}
            if lock_paths:
                lead_context.file_locks.release(profile.name, lock_paths)
        return True
    return ran_any
