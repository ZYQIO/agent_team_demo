from __future__ import annotations

import json
import pathlib
import shlex
import shutil
import subprocess
import sys
import time
import uuid
from types import SimpleNamespace
from typing import Any, Callable, Collection, Dict, List, Optional, Sequence, Tuple

from ..config import RuntimeConfig
from ..core import AgentProfile, EventLogger, Message, Task, utc_now
from ..models import build_provider
from ..workflows.markdown_audit_analysis import handle_dynamic_planning as handle_markdown_dynamic_planning
from ..workflows.markdown_audit_reporting import (
    handle_llm_synthesis as handle_markdown_llm_synthesis,
    handle_recommendation_pack as handle_markdown_recommendation_pack,
)
from ..workflows.repo_audit_analysis import handle_repo_dynamic_planning
from ..workflows.repo_audit_reporting import (
    handle_llm_synthesis as handle_repo_llm_synthesis,
    handle_repo_recommendation_pack,
)
from ..workflows.shared_challenge import (
    handle_evidence_pack as handle_shared_evidence_pack,
    handle_peer_challenge as handle_shared_peer_challenge,
)


TMUX_ANALYST_TASK_TYPES = {
    "discover_markdown",
    "discover_repository",
    "heading_audit",
    "length_audit",
    "extension_audit",
    "large_file_audit",
    "heading_structure_followup",
    "length_risk_followup",
    "extension_hotspot_followup",
    "directory_hotspot_followup",
}
TMUX_REVIEWER_EXTERNAL_TASK_TYPES = {
    "dynamic_planning",
    "repo_dynamic_planning",
    "peer_challenge",
    "evidence_pack",
    "llm_synthesis",
    "recommendation_pack",
    "repo_recommendation_pack",
}
TMUX_EXTERNAL_TASK_TYPES = TMUX_ANALYST_TASK_TYPES | TMUX_REVIEWER_EXTERNAL_TASK_TYPES
TMUX_WORKER_DIAGNOSTICS_FILENAME = "tmux_worker_diagnostics.jsonl"
TMUX_WORKER_OUTPUT_PREVIEW_LIMIT = 240
TMUX_SESSION_POLL_INTERVAL_SEC = 0.1
TMUX_SESSION_SPAWN_MAX_ATTEMPTS = 3
TMUX_SESSION_LEASES_KEY = "tmux_session_leases"


class _WorkerNullLogger:
    def log(self, _event: str, **_fields: Any) -> None:
        return


class _WorkerMailboxNull:
    def send(
        self,
        sender: str,
        recipient: str,
        subject: str,
        body: str,
        task_id: Optional[str] = None,
    ) -> None:
        del sender, recipient, subject, body, task_id

    def broadcast(self, sender: str, subject: str, body: str) -> None:
        del sender, subject, body

    def pull(self, recipient: str) -> List[Message]:
        del recipient
        return []

    def pull_matching(
        self,
        recipient: str,
        matcher: Callable[[Message], bool],
    ) -> List[Message]:
        del recipient, matcher
        return []


class _WorkerMailboxBridge:
    def __init__(
        self,
        *,
        requests_dir: pathlib.Path,
        responses_dir: pathlib.Path,
        poll_interval_sec: float = 0.05,
        timeout_sec: float = 30.0,
    ) -> None:
        self._requests_dir = requests_dir
        self._responses_dir = responses_dir
        self._poll_interval_sec = poll_interval_sec
        self._timeout_sec = timeout_sec
        self._local_queues: Dict[str, List[Message]] = {}
        self._requests_dir.mkdir(parents=True, exist_ok=True)
        self._responses_dir.mkdir(parents=True, exist_ok=True)

    def _request(self, op: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        request_id = uuid.uuid4().hex
        request_path = self._requests_dir / f"{request_id}.json"
        response_path = self._responses_dir / f"{request_id}.json"
        request_path.write_text(
            json.dumps({"request_id": request_id, "op": op, "payload": payload}, ensure_ascii=False),
            encoding="utf-8",
        )
        deadline = time.time() + self._timeout_sec
        while time.time() < deadline:
            if response_path.exists():
                response = json.loads(response_path.read_text(encoding="utf-8"))
                response_path.unlink(missing_ok=True)
                if not isinstance(response, dict):
                    raise RuntimeError(f"mailbox bridge invalid response for op={op}")
                if not response.get("ok", False):
                    raise RuntimeError(str(response.get("error", f"mailbox bridge {op} failed")))
                payload = response.get("payload", {})
                if isinstance(payload, dict):
                    return payload
                return {"value": payload}
            time.sleep(self._poll_interval_sec)
        raise TimeoutError(f"mailbox bridge timeout for op={op}")

    def send(
        self,
        sender: str,
        recipient: str,
        subject: str,
        body: str,
        task_id: Optional[str] = None,
    ) -> None:
        self._request(
            "send",
            {
                "sender": sender,
                "recipient": recipient,
                "subject": subject,
                "body": body,
                "task_id": task_id,
            },
        )

    def broadcast(self, sender: str, subject: str, body: str) -> None:
        self._request(
            "broadcast",
            {
                "sender": sender,
                "subject": subject,
                "body": body,
            },
        )

    def _pull_remote(self, recipient: str) -> List[Message]:
        payload = self._request("pull", {"recipient": recipient})
        messages = payload.get("messages", [])
        if not isinstance(messages, list):
            return []
        result: List[Message] = []
        for item in messages:
            if isinstance(item, dict):
                result.append(
                    Message(
                        message_id=str(item.get("message_id", "")),
                        sent_at=str(item.get("sent_at", "")),
                        sender=str(item.get("sender", "")),
                        recipient=str(item.get("recipient", "")),
                        subject=str(item.get("subject", "")),
                        body=str(item.get("body", "")),
                        task_id=item.get("task_id"),
                    )
                )
        return result

    def pull(self, recipient: str) -> List[Message]:
        queued = list(self._local_queues.pop(recipient, []))
        queued.extend(self._pull_remote(recipient))
        return queued

    def pull_matching(
        self,
        recipient: str,
        matcher: Callable[[Message], bool],
    ) -> List[Message]:
        queue = list(self._local_queues.pop(recipient, []))
        queue.extend(self._pull_remote(recipient))
        matched: List[Message] = []
        rest: List[Message] = []
        for message in queue:
            if matcher(message):
                matched.append(message)
            else:
                rest.append(message)
        self._local_queues[recipient] = rest
        return matched


class _WorkerSharedState:
    def __init__(self, payload: Dict[str, Any]) -> None:
        self._original = json.loads(json.dumps(payload, ensure_ascii=False))
        self._data = json.loads(json.dumps(payload, ensure_ascii=False))

    def get(self, key: str, default: Any = None) -> Any:
        return self._data.get(key, default)

    def set(self, key: str, value: Any) -> None:
        self._data[key] = value

    def snapshot(self) -> Dict[str, Any]:
        return json.loads(json.dumps(self._data, ensure_ascii=False))

    def changed_values(self) -> Dict[str, Any]:
        changes: Dict[str, Any] = {}
        keys = set(self._original.keys()) | set(self._data.keys())
        for key in keys:
            if self._original.get(key) != self._data.get(key):
                changes[str(key)] = self._data.get(key)
        return changes


class _WorkerBoardView:
    def __init__(self, task_results: Dict[str, Any], task_ids: Sequence[str]) -> None:
        self._task_results = dict(task_results)
        self._task_ids = {str(task_id) for task_id in task_ids}
        self._inserted_tasks: List[Task] = []
        self._added_dependencies: List[Dict[str, str]] = []

    def get_task_result(self, task_id: str) -> Optional[Dict[str, Any]]:
        result = self._task_results.get(task_id)
        if isinstance(result, dict):
            return dict(result)
        return result

    def has_task(self, task_id: str) -> bool:
        if task_id in self._task_ids:
            return True
        return any(task.task_id == task_id for task in self._inserted_tasks)

    def add_tasks(self, tasks: Sequence[Task], inserted_by: str) -> List[str]:
        inserted_ids: List[str] = []
        for task in tasks:
            if self.has_task(task.task_id):
                continue
            inserted = Task(
                task_id=task.task_id,
                title=task.title,
                task_type=task.task_type,
                required_skills=set(task.required_skills),
                dependencies=list(task.dependencies),
                payload=dict(task.payload),
                locked_paths=list(task.locked_paths),
                allowed_agent_types=set(task.allowed_agent_types),
            )
            self._inserted_tasks.append(inserted)
            self._task_ids.add(inserted.task_id)
            inserted_ids.append(inserted.task_id)
        self._inserted_by = str(inserted_by)
        return inserted_ids

    def add_dependency(self, task_id: str, dependency_id: str, updated_by: str) -> bool:
        record = {
            "task_id": str(task_id),
            "dependency_id": str(dependency_id),
            "updated_by": str(updated_by),
        }
        if record in self._added_dependencies:
            return False
        if not self.has_task(record["task_id"]) or not self.has_task(record["dependency_id"]):
            return False
        self._added_dependencies.append(record)
        return True

    def mutations(self) -> Dict[str, Any]:
        return {
            "inserted_by": getattr(self, "_inserted_by", ""),
            "add_tasks": [task.to_dict() for task in self._inserted_tasks],
            "add_dependencies": list(self._added_dependencies),
        }


def _build_worker_context(payload: Dict[str, Any]) -> Any:
    runtime_payload = payload.get("runtime_config", {})
    if not isinstance(runtime_payload, dict):
        runtime_payload = {}
    runtime_config = RuntimeConfig(
        **{
            key: value
            for key, value in runtime_payload.items()
            if key in RuntimeConfig.__annotations__
        }
    )
    shared_state_payload = payload.get("shared_state", {})
    if not isinstance(shared_state_payload, dict):
        shared_state_payload = {}
    board_results = payload.get("task_results", {})
    if not isinstance(board_results, dict):
        board_results = {}
    task_ids = payload.get("task_ids", [])
    if not isinstance(task_ids, list):
        task_ids = []
    profile_payload = payload.get("profile", {})
    if not isinstance(profile_payload, dict):
        profile_payload = {}
    model_payload = payload.get("provider_config", {})
    if not isinstance(model_payload, dict):
        model_payload = {}
    mailbox_bridge_payload = payload.get("mailbox_bridge", {})
    if not isinstance(mailbox_bridge_payload, dict):
        mailbox_bridge_payload = {}
    provider, _ = build_provider(
        provider_name=str(model_payload.get("provider_name", model_payload.get("provider", "heuristic"))),
        model=str(model_payload.get("model", "heuristic-v1")),
        openai_api_key_env=str(model_payload.get("openai_api_key_env", "OPENAI_API_KEY")),
        openai_base_url=str(model_payload.get("openai_base_url", "https://api.openai.com/v1")),
        require_llm=bool(model_payload.get("require_llm", False)),
        timeout_sec=int(model_payload.get("timeout_sec", 60)),
    )
    mailbox: Any = _WorkerMailboxNull()
    requests_dir = str(mailbox_bridge_payload.get("requests_dir", "") or "")
    responses_dir = str(mailbox_bridge_payload.get("responses_dir", "") or "")
    if requests_dir and responses_dir:
        mailbox = _WorkerMailboxBridge(
            requests_dir=pathlib.Path(requests_dir),
            responses_dir=pathlib.Path(responses_dir),
        )
    return SimpleNamespace(
        profile=AgentProfile(
            name=str(profile_payload.get("name", "worker")),
            skills={str(skill) for skill in profile_payload.get("skills", [])},
            agent_type=str(profile_payload.get("agent_type", "general")),
        ),
        target_dir=pathlib.Path(str(payload.get("target_dir", "."))).absolute(),
        output_dir=pathlib.Path(str(payload.get("output_dir", "."))).absolute(),
        goal=str(payload.get("goal", "")),
        provider=provider,
        runtime_config=runtime_config,
        board=_WorkerBoardView(task_results=board_results, task_ids=task_ids),
        shared_state=_WorkerSharedState(shared_state_payload),
        mailbox=mailbox,
        logger=_WorkerNullLogger(),
    )


def _run_handler_backed_worker(payload: Dict[str, Any], handler: Callable[[Any, Task], Dict[str, Any]]) -> Dict[str, Any]:
    context = _build_worker_context(payload)
    task_payload = payload.get("task_payload", {})
    if not isinstance(task_payload, dict):
        task_payload = {}
    task = Task(
        task_id=str(payload.get("task_id", payload.get("task_type", "worker_task"))),
        title=str(payload.get("task_title", payload.get("task_type", "worker task"))),
        task_type=str(payload.get("task_type", "")),
        required_skills={str(skill) for skill in payload.get("task_required_skills", [])},
        dependencies=[str(dep) for dep in payload.get("task_dependencies", [])],
        payload=task_payload,
        locked_paths=[str(path) for path in payload.get("task_locked_paths", [])],
        allowed_agent_types={str(name) for name in payload.get("task_allowed_agent_types", [])},
    )
    result = handler(context, task)
    if not isinstance(result, dict):
        result = {"raw_result": result}
    return {
        "result": result,
        "state_updates": context.shared_state.changed_values(),
        "board_mutations": context.board.mutations(),
    }


def preferred_tmux_session_name(worker_name: str) -> str:
    return f"agent_{worker_name}"


def _load_tmux_session_leases(shared_state: Any) -> Dict[str, Dict[str, Any]]:
    raw = shared_state.get(TMUX_SESSION_LEASES_KEY, {})
    if not isinstance(raw, dict):
        return {}
    leases: Dict[str, Dict[str, Any]] = {}
    for worker_name, entry in raw.items():
        if isinstance(entry, dict):
            leases[str(worker_name)] = dict(entry)
    return leases


def _save_tmux_session_leases(shared_state: Any, leases: Dict[str, Dict[str, Any]]) -> None:
    shared_state.set(TMUX_SESSION_LEASES_KEY, dict(leases))


def _get_tmux_session_lease(shared_state: Any, worker_name: str) -> Dict[str, Any]:
    return dict(_load_tmux_session_leases(shared_state).get(str(worker_name), {}))


def _lease_allows_preferred_session_reuse(shared_state: Any, worker_name: str) -> bool:
    lease = _get_tmux_session_lease(shared_state=shared_state, worker_name=worker_name)
    return (
        str(lease.get("status", "")) in {"retained", "recovered_available"}
        and str(lease.get("session_name", "")) == preferred_tmux_session_name(worker_name)
    )


def _update_tmux_session_lease(
    lead_context: Any,
    worker_name: str,
    status: str,
    task_id: str = "",
    task_type: str = "",
    transport: str = "",
    cleanup_result: str = "",
    retained_for_reuse: bool = False,
    reused_existing: bool = False,
    reuse_authorized: bool = False,
    error: str = "",
    session_name: str = "",
    recovery_result: str = "",
) -> Dict[str, Any]:
    leases = _load_tmux_session_leases(lead_context.shared_state)
    current = dict(leases.get(worker_name, {}))
    next_session_name = (
        str(session_name or "")
        or str(current.get("session_name", ""))
        or preferred_tmux_session_name(worker_name)
    )
    reuse_count = int(current.get("reuse_count", 0) or 0)
    if reused_existing:
        reuse_count += 1
    retained_at = str(current.get("retained_at", "") or "")
    if retained_for_reuse:
        retained_at = utc_now()
    entry = {
        "worker": worker_name,
        "session_name": next_session_name,
        "status": status,
        "last_task_id": str(task_id or current.get("last_task_id", "")),
        "last_task_type": str(task_type or current.get("last_task_type", "")),
        "last_transport": str(transport or current.get("last_transport", "")),
        "last_cleanup_result": str(cleanup_result or current.get("last_cleanup_result", "")),
        "retained_for_reuse": bool(retained_for_reuse),
        "reuse_authorized": bool(reuse_authorized),
        "reused_existing": bool(reused_existing),
        "reuse_count": reuse_count,
        "retained_at": retained_at,
        "recovery_result": str(recovery_result or current.get("recovery_result", "")),
        "recovered_at": utc_now() if recovery_result else str(current.get("recovered_at", "")),
        "updated_at": utc_now(),
        "error": str(error or "")[:400],
    }
    leases[worker_name] = entry
    _save_tmux_session_leases(shared_state=lead_context.shared_state, leases=leases)
    lead_context.logger.log(
        "tmux_worker_session_lease_updated",
        worker=worker_name,
        session_name=next_session_name,
        status=status,
        task_id=str(entry.get("last_task_id", "")),
        task_type=str(entry.get("last_task_type", "")),
        transport=str(entry.get("last_transport", "")),
        cleanup_result=str(entry.get("last_cleanup_result", "")),
        retained_for_reuse=bool(retained_for_reuse),
        reuse_authorized=bool(reuse_authorized),
        reused_existing=bool(reused_existing),
        reuse_count=reuse_count,
        retained_at=retained_at,
        recovery_result=str(entry.get("recovery_result", "")),
        error=str(entry.get("error", "")),
    )
    return entry


def _tmux_session_exists(session_name: str) -> Dict[str, Any]:
    completed = subprocess.run(
        ["tmux", "has-session", "-t", session_name],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    stderr = str(completed.stderr or "").strip()
    if completed.returncode == 0:
        return {"exists": True, "error": ""}
    if _is_no_tmux_server_error(stderr) or _is_missing_tmux_session_error(stderr):
        return {"exists": False, "error": ""}
    return {"exists": False, "error": stderr[:400]}


def tmux_worker_diagnostics_file(output_dir: pathlib.Path) -> pathlib.Path:
    return output_dir / TMUX_WORKER_DIAGNOSTICS_FILENAME


def _output_preview(text: str, limit: int = TMUX_WORKER_OUTPUT_PREVIEW_LIMIT) -> str:
    compact = " ".join(str(text or "").split())
    return compact[:limit]


def _append_tmux_worker_diagnostics(output_dir: pathlib.Path, record: Dict[str, Any]) -> None:
    diagnostics_path = tmux_worker_diagnostics_file(output_dir)
    diagnostics_path.parent.mkdir(parents=True, exist_ok=True)
    with diagnostics_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")


def _attach_tmux_lifecycle(
    completed: subprocess.CompletedProcess[str],
    lifecycle: Dict[str, Any],
) -> subprocess.CompletedProcess[str]:
    completed.tmux_lifecycle = dict(lifecycle)
    return completed


def _extract_tmux_lifecycle(completed: subprocess.CompletedProcess[str]) -> Dict[str, Any]:
    lifecycle = getattr(completed, "tmux_lifecycle", {})
    if isinstance(lifecycle, dict):
        return dict(lifecycle)
    return {}


def _attach_transport_timeout(
    completed: subprocess.CompletedProcess[str],
    timeout_metadata: Dict[str, Any],
) -> subprocess.CompletedProcess[str]:
    completed.transport_timeout = dict(timeout_metadata)
    return completed


def _extract_transport_timeout(completed: subprocess.CompletedProcess[str]) -> Dict[str, Any]:
    timeout_metadata = getattr(completed, "transport_timeout", {})
    if isinstance(timeout_metadata, dict):
        return dict(timeout_metadata)
    return {}


def _normalize_timeout_output(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="ignore")
    return str(value)


def _completed_process_from_timeout(
    command: List[str],
    timeout_exc: subprocess.TimeoutExpired,
    transport: str,
    phase: str,
) -> subprocess.CompletedProcess[str]:
    stdout = _normalize_timeout_output(getattr(timeout_exc, "stdout", None))
    if not stdout:
        stdout = _normalize_timeout_output(getattr(timeout_exc, "output", None))
    stderr = _normalize_timeout_output(getattr(timeout_exc, "stderr", None))
    timeout_seconds = int(getattr(timeout_exc, "timeout", 0) or 0)
    if not stderr:
        stderr = f"{transport} worker timed out after {timeout_seconds}s"
    completed = subprocess.CompletedProcess(
        args=command,
        returncode=124,
        stdout=stdout,
        stderr=stderr,
    )
    return _attach_transport_timeout(
        completed,
        {
            "execution_timed_out": True,
            "timeout_transport": transport,
            "timeout_phase": phase,
            "timeout_message": stderr[:400],
        },
    )


def _is_duplicate_tmux_session_error(stderr: str) -> bool:
    message = str(stderr or "").casefold()
    return "duplicate session" in message or "session already exists" in message


def _is_no_tmux_server_error(message: str) -> bool:
    text = str(message or "").casefold()
    return "no server running" in text or "failed to connect to server" in text


def _is_missing_tmux_session_error(message: str) -> bool:
    text = str(message or "").casefold()
    return "can't find session" in text or "no such session" in text


def _build_tmux_ipc_paths(
    ipc_dir: pathlib.Path,
    session_name: str,
) -> Tuple[pathlib.Path, pathlib.Path, pathlib.Path]:
    return (
        ipc_dir / f"{session_name}.stdout.txt",
        ipc_dir / f"{session_name}.stderr.txt",
        ipc_dir / f"{session_name}.status.txt",
    )


def _build_tmux_session_name_candidates(
    session_prefix: str,
    max_attempts: int,
) -> List[str]:
    candidates = [session_prefix, session_prefix]
    while len(candidates) < max_attempts:
        candidates.append(f"{session_prefix}_{uuid.uuid4().hex[:8]}")
    return candidates[:max_attempts]


def _build_tmux_shell_command(
    command: List[str],
    stdout_file: pathlib.Path,
    stderr_file: pathlib.Path,
    status_file: pathlib.Path,
) -> str:
    return (
        f"{shlex.join(command)} > {shlex.quote(str(stdout_file))} "
        f"2> {shlex.quote(str(stderr_file))}; "
        f"echo $? > {shlex.quote(str(status_file))}"
    )


def _reuse_tmux_session(
    command: List[str],
    session_name: str,
    stdout_file: pathlib.Path,
    stderr_file: pathlib.Path,
    status_file: pathlib.Path,
) -> Dict[str, Any]:
    shell_cmd = _build_tmux_shell_command(
        command=command,
        stdout_file=stdout_file,
        stderr_file=stderr_file,
        status_file=status_file,
    )
    reused = subprocess.run(
        ["tmux", "respawn-pane", "-k", "-t", f"{session_name}:0.0", shell_cmd],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    stderr = (reused.stderr or "").strip()
    if reused.returncode == 0:
        return {
            "ok": True,
            "result": "respawned",
            "error": "",
            "completed": reused,
        }
    return {
        "ok": False,
        "result": "respawn_failed",
        "error": stderr[:400],
        "completed": reused,
    }


def _spawn_tmux_session(
    command: List[str],
    ipc_dir: pathlib.Path,
    session_prefix: str,
    preferred_session_found: bool = False,
    allow_existing_session_reuse: bool = False,
) -> Dict[str, Any]:
    attempts = 0
    retry_reason = ""
    last_error = ""
    last_spawn: Optional[subprocess.CompletedProcess[str]] = None
    session_name = ""
    stdout_file = ipc_dir / "stdout.txt"
    stderr_file = ipc_dir / "stderr.txt"
    status_file = ipc_dir / "status.txt"
    stale_cleanup_attempted = False
    stale_cleanup_session_name = ""
    stale_cleanup_result = ""
    stale_cleanup_error = ""
    stale_cleanup_retry_attempted = False
    stale_cleanup_retry_result = ""
    stale_cleanup_retry_error = ""
    stale_session_exists_after_cleanup = False
    stale_cleanup_verification_error = ""
    preferred_session_name = session_prefix
    preferred_session_retried = False
    preferred_session_reused = False
    session_name_strategy = ""
    preferred_session_reuse_attempted = False
    preferred_session_reuse_result = ""
    preferred_session_reuse_error = ""
    preferred_session_reused_existing = False
    candidate_names = _build_tmux_session_name_candidates(
        session_prefix=session_prefix,
        max_attempts=TMUX_SESSION_SPAWN_MAX_ATTEMPTS,
    )

    while attempts < TMUX_SESSION_SPAWN_MAX_ATTEMPTS:
        attempts += 1
        session_name = candidate_names[attempts - 1]
        session_name_strategy = "preferred" if session_name == preferred_session_name else "unique_suffix"
        stdout_file, stderr_file, status_file = _build_tmux_ipc_paths(ipc_dir=ipc_dir, session_name=session_name)
        shell_cmd = _build_tmux_shell_command(
            command=command,
            stdout_file=stdout_file,
            stderr_file=stderr_file,
            status_file=status_file,
        )
        last_spawn = subprocess.run(
            ["tmux", "new-session", "-d", "-s", session_name, shell_cmd],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        if last_spawn.returncode == 0:
            preferred_session_reused = session_name == preferred_session_name
            return {
                "ok": True,
                "session_name": session_name,
                "preferred_session_name": preferred_session_name,
                "session_name_strategy": session_name_strategy,
                "preferred_session_retried": preferred_session_retried,
                "preferred_session_reused": preferred_session_reused,
                "preferred_session_reuse_attempted": preferred_session_reuse_attempted,
                "preferred_session_reuse_result": preferred_session_reuse_result[:400],
                "preferred_session_reuse_error": preferred_session_reuse_error[:400],
                "preferred_session_reused_existing": preferred_session_reused_existing,
                "stdout_file": stdout_file,
                "stderr_file": stderr_file,
                "status_file": status_file,
                "spawn": last_spawn,
                "attempts": attempts,
                "retried": attempts > 1,
                "retry_reason": retry_reason[:400],
                "spawn_error": "",
                "stale_cleanup_attempted": stale_cleanup_attempted,
                "stale_cleanup_session_name": stale_cleanup_session_name,
                "stale_cleanup_result": stale_cleanup_result[:400],
                "stale_cleanup_error": stale_cleanup_error[:400],
                "stale_cleanup_retry_attempted": stale_cleanup_retry_attempted,
                "stale_cleanup_retry_result": stale_cleanup_retry_result[:400],
                "stale_cleanup_retry_error": stale_cleanup_retry_error[:400],
                "stale_session_exists_after_cleanup": stale_session_exists_after_cleanup,
                "stale_cleanup_verification_error": stale_cleanup_verification_error[:400],
            }

        last_error = (last_spawn.stderr or "").strip()
        if _is_duplicate_tmux_session_error(last_error) and attempts < TMUX_SESSION_SPAWN_MAX_ATTEMPTS:
            retry_reason = last_error
            if (
                allow_existing_session_reuse
                and session_name == preferred_session_name
                and preferred_session_found
            ):
                preferred_session_reuse_attempted = True
                reuse = _reuse_tmux_session(
                    command=command,
                    session_name=session_name,
                    stdout_file=stdout_file,
                    stderr_file=stderr_file,
                    status_file=status_file,
                )
                preferred_session_reuse_result = str(reuse["result"])
                preferred_session_reuse_error = str(reuse["error"])
                if reuse["ok"]:
                    preferred_session_reused = True
                    preferred_session_reused_existing = True
                    return {
                        "ok": True,
                        "session_name": session_name,
                        "preferred_session_name": preferred_session_name,
                        "session_name_strategy": "preferred_reused_existing",
                        "preferred_session_retried": preferred_session_retried,
                        "preferred_session_reused": preferred_session_reused,
                        "preferred_session_reuse_attempted": preferred_session_reuse_attempted,
                        "preferred_session_reuse_result": preferred_session_reuse_result[:400],
                        "preferred_session_reuse_error": preferred_session_reuse_error[:400],
                        "preferred_session_reused_existing": preferred_session_reused_existing,
                        "stdout_file": stdout_file,
                        "stderr_file": stderr_file,
                        "status_file": status_file,
                        "spawn": reuse["completed"],
                        "attempts": attempts,
                        "retried": False,
                        "retry_reason": retry_reason[:400],
                        "spawn_error": "",
                        "stale_cleanup_attempted": stale_cleanup_attempted,
                        "stale_cleanup_session_name": stale_cleanup_session_name,
                        "stale_cleanup_result": stale_cleanup_result[:400],
                        "stale_cleanup_error": stale_cleanup_error[:400],
                        "stale_cleanup_retry_attempted": stale_cleanup_retry_attempted,
                        "stale_cleanup_retry_result": stale_cleanup_retry_result[:400],
                        "stale_cleanup_retry_error": stale_cleanup_retry_error[:400],
                        "stale_session_exists_after_cleanup": stale_session_exists_after_cleanup,
                        "stale_cleanup_verification_error": stale_cleanup_verification_error[:400],
                    }
            if session_name == preferred_session_name:
                preferred_session_retried = True
            stale_cleanup = _cleanup_stale_tmux_session(session_name=session_name)
            stale_cleanup_attempted = bool(stale_cleanup["attempted"])
            stale_cleanup_session_name = str(stale_cleanup["session_name"])
            stale_cleanup_result = str(stale_cleanup["result"])
            stale_cleanup_error = str(stale_cleanup["error"])
            stale_cleanup_retry_attempted = bool(stale_cleanup["retry_attempted"])
            stale_cleanup_retry_result = str(stale_cleanup["retry_result"])
            stale_cleanup_retry_error = str(stale_cleanup["retry_error"])
            stale_session_exists_after_cleanup = bool(stale_cleanup["session_exists_after_cleanup"])
            stale_cleanup_verification_error = str(stale_cleanup["verification_error"])
            continue
        break

    return {
        "ok": False,
        "session_name": session_name,
        "preferred_session_name": preferred_session_name,
        "session_name_strategy": session_name_strategy,
        "preferred_session_retried": preferred_session_retried,
        "preferred_session_reused": preferred_session_reused,
        "preferred_session_reuse_attempted": preferred_session_reuse_attempted,
        "preferred_session_reuse_result": preferred_session_reuse_result[:400],
        "preferred_session_reuse_error": preferred_session_reuse_error[:400],
        "preferred_session_reused_existing": preferred_session_reused_existing,
        "stdout_file": stdout_file,
        "stderr_file": stderr_file,
        "status_file": status_file,
        "spawn": last_spawn,
        "attempts": attempts,
        "retried": attempts > 1,
        "retry_reason": retry_reason[:400],
        "spawn_error": last_error[:400],
        "stale_cleanup_attempted": stale_cleanup_attempted,
        "stale_cleanup_session_name": stale_cleanup_session_name,
        "stale_cleanup_result": stale_cleanup_result[:400],
        "stale_cleanup_error": stale_cleanup_error[:400],
        "stale_cleanup_retry_attempted": stale_cleanup_retry_attempted,
        "stale_cleanup_retry_result": stale_cleanup_retry_result[:400],
        "stale_cleanup_retry_error": stale_cleanup_retry_error[:400],
        "stale_session_exists_after_cleanup": stale_session_exists_after_cleanup,
        "stale_cleanup_verification_error": stale_cleanup_verification_error[:400],
    }


def _kill_tmux_session(session_name: str) -> Dict[str, Any]:
    kill = subprocess.run(
        ["tmux", "kill-session", "-t", session_name],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    stderr = (kill.stderr or "").strip()
    if kill.returncode == 0:
        result = "killed"
    elif "can't find session" in stderr.casefold():
        result = "already_exited"
    else:
        result = "failed"
    return {
        "result": result,
        "returncode": kill.returncode,
        "error": stderr[:400],
    }


def _list_tmux_sessions() -> Dict[str, Any]:
    listed = subprocess.run(
        ["tmux", "list-sessions", "-F", "#{session_name}"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    stderr = (listed.stderr or "").strip()
    stdout = (listed.stdout or "").strip()
    if listed.returncode == 0:
        sessions = [line.strip() for line in stdout.splitlines() if line.strip()]
        return {
            "sessions": sessions,
            "error": "",
            "server_running": True,
        }
    if _is_no_tmux_server_error(stderr or stdout):
        return {
            "sessions": [],
            "error": "",
            "server_running": False,
        }
    return {
        "sessions": [],
        "error": (stderr or stdout)[:400],
        "server_running": True,
    }


def _cleanup_tmux_session_with_recovery(session_name: str) -> Dict[str, Any]:
    cleanup = _kill_tmux_session(session_name)
    retry_attempted = False
    retry_result = ""
    retry_error = ""
    session_exists_after_cleanup = False
    verification_error = ""

    if cleanup["result"] not in {"killed", "already_exited"}:
        existence = subprocess.run(
            ["tmux", "has-session", "-t", session_name],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        stderr = (existence.stderr or "").strip()
        stdout = (existence.stdout or "").strip()
        message = stderr or stdout
        if existence.returncode == 0:
            session_exists_after_cleanup = True
        elif existence.returncode == 1 or "can't find session" in message.casefold():
            session_exists_after_cleanup = False
        else:
            verification_error = message[:400]

        if session_exists_after_cleanup:
            retry_attempted = True
            retry = _kill_tmux_session(session_name)
            retry_result = retry["result"]
            retry_error = retry["error"]
            if retry["result"] in {"killed", "already_exited"}:
                cleanup = {
                    "result": "recovered_after_retry",
                    "returncode": retry["returncode"],
                    "error": retry["error"],
                }
            else:
                cleanup = {
                    "result": "cleanup_retry_failed",
                    "returncode": retry["returncode"],
                    "error": retry["error"] or cleanup["error"],
                }

    return {
        "session_name": session_name,
        "result": cleanup["result"],
        "error": cleanup["error"],
        "retry_attempted": retry_attempted,
        "retry_result": retry_result,
        "retry_error": retry_error,
        "session_exists_after_cleanup": session_exists_after_cleanup,
        "verification_error": verification_error,
    }


def _cleanup_stale_tmux_session(session_name: str) -> Dict[str, Any]:
    cleanup = _cleanup_tmux_session_with_recovery(session_name)
    cleanup["attempted"] = True
    return cleanup


def _cleanup_active_tmux_session(session_name: str) -> Dict[str, Any]:
    cleanup = _cleanup_tmux_session_with_recovery(session_name)
    cleanup["attempted"] = True
    return cleanup


def _cleanup_orphan_tmux_sessions(session_prefix: str, keep_exact_session: bool = False) -> Dict[str, Any]:
    listed = _list_tmux_sessions()
    preferred_session_found = False
    matched = [
        name
        for name in listed["sessions"]
        if str(name).startswith(f"{session_prefix}_")
    ]
    for name in listed["sessions"]:
        if str(name) == session_prefix:
            preferred_session_found = True
            if not keep_exact_session:
                matched.append(name)
    cleaned = 0
    failed: List[str] = []

    for session_name in matched:
        cleanup = _cleanup_tmux_session_with_recovery(session_name)
        if cleanup["result"] in {"killed", "already_exited", "recovered_after_retry"}:
            cleaned += 1
        else:
            failed.append(session_name)

    return {
        "attempted": True,
        "server_running": bool(listed["server_running"]),
        "preferred_session_found": preferred_session_found,
        "sessions_found": len(matched),
        "sessions_cleaned": cleaned,
        "sessions_failed": len(failed),
        "failed_sessions": failed,
        "error": str(listed["error"]),
    }


def _cleanup_tmux_ipc_files(paths: Sequence[pathlib.Path]) -> Dict[str, Any]:
    removed = 0
    errors: List[str] = []
    for path in paths:
        existed = path.exists()
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            errors.append(f"{path.name}: {exc}")
            continue
        if existed:
            removed += 1

    if errors:
        result = "partial_failed" if removed else "failed"
    elif removed > 0:
        result = "removed"
    else:
        result = "already_absent"
    return {
        "result": result,
        "removed": removed,
        "error": "; ".join(errors)[:400],
    }


def _worker_discover_markdown(target_dir: pathlib.Path, output_dir: pathlib.Path) -> Dict[str, Any]:
    inventory: List[Dict[str, Any]] = []
    ignore_prefix = str(output_dir.resolve())
    for path in sorted(p for p in target_dir.rglob("*.md") if p.is_file()):
        absolute = str(path.resolve())
        if absolute.startswith(ignore_prefix):
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        lines = text.splitlines()
        headings = sum(1 for line in lines if line.lstrip().startswith("#"))
        inventory.append(
            {
                "path": str(path.relative_to(target_dir)),
                "line_count": len(lines),
                "heading_count": headings,
            }
        )
    return {
        "result": {"markdown_files": len(inventory), "sample": inventory[:3]},
        "state_updates": {"markdown_inventory": inventory},
    }


def _worker_discover_repository(target_dir: pathlib.Path, output_dir: pathlib.Path) -> Dict[str, Any]:
    inventory: List[Dict[str, Any]] = []
    ignore_prefix = str(output_dir.resolve())
    for path in sorted(p for p in target_dir.rglob("*") if p.is_file()):
        absolute = str(path.resolve())
        if absolute.startswith(ignore_prefix):
            continue
        relative_path = path.relative_to(target_dir)
        if ".git" in relative_path.parts:
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        lines = text.splitlines()
        inventory.append(
            {
                "path": str(relative_path),
                "extension": path.suffix.strip().lower() or "<no_ext>",
                "line_count": len(lines),
                "byte_count": path.stat().st_size,
                "top_level_dir": relative_path.parts[0] if len(relative_path.parts) > 1 else ".",
            }
        )
    return {
        "result": {"repository_files": len(inventory), "sample": inventory[:3]},
        "state_updates": {"repository_inventory": inventory},
    }


def _worker_heading_audit(inventory: List[Dict[str, Any]]) -> Dict[str, Any]:
    missing = [item for item in inventory if int(item.get("heading_count", 0)) == 0]
    return {
        "result": {
            "files_without_headings": len(missing),
            "examples": [str(item.get("path", "")) for item in missing[:10]],
        },
        "state_updates": {"heading_issues": missing},
    }


def _worker_length_audit(inventory: List[Dict[str, Any]], threshold: int) -> Dict[str, Any]:
    long_files = [item for item in inventory if int(item.get("line_count", 0)) >= threshold]
    return {
        "result": {
            "line_threshold": threshold,
            "long_files": len(long_files),
            "examples": [str(item.get("path", "")) for item in long_files[:10]],
        },
        "state_updates": {"length_issues": long_files},
    }


def _worker_extension_audit(inventory: List[Dict[str, Any]]) -> Dict[str, Any]:
    totals: Dict[str, Dict[str, Any]] = {}
    for item in inventory:
        extension = str(item.get("extension", "<no_ext>") or "<no_ext>")
        bucket = totals.setdefault(
            extension,
            {
                "extension": extension,
                "file_count": 0,
                "total_lines": 0,
                "total_bytes": 0,
            },
        )
        bucket["file_count"] += 1
        bucket["total_lines"] += int(item.get("line_count", 0))
        bucket["total_bytes"] += int(item.get("byte_count", 0))
    ranked = sorted(
        totals.values(),
        key=lambda row: (int(row["file_count"]), int(row["total_lines"]), str(row["extension"])),
        reverse=True,
    )
    result = {
        "total_files": len(inventory),
        "unique_extensions": len(totals),
        "files_without_extension": int(totals.get("<no_ext>", {}).get("file_count", 0)),
        "top_extensions": ranked[:6],
    }
    return {
        "result": result,
        "state_updates": {"repository_extension_summary": result},
    }


def _worker_large_file_audit(
    inventory: List[Dict[str, Any]],
    line_threshold: int,
    byte_threshold: int,
) -> Dict[str, Any]:
    oversized = [
        item
        for item in inventory
        if int(item.get("line_count", 0)) >= line_threshold
        or int(item.get("byte_count", 0)) >= byte_threshold
    ]
    ranked = sorted(
        oversized,
        key=lambda row: (int(row.get("line_count", 0)), int(row.get("byte_count", 0)), str(row.get("path", ""))),
        reverse=True,
    )
    return {
        "result": {
            "line_threshold": line_threshold,
            "byte_threshold": byte_threshold,
            "oversized_files": len(ranked),
            "examples": [str(item.get("path", "")) for item in ranked[:10]],
        },
        "state_updates": {"repository_large_files": ranked},
    }


def _worker_heading_followup(inventory: List[Dict[str, Any]], top_n: int) -> Dict[str, Any]:
    scored: List[Dict[str, Any]] = []
    for item in inventory:
        line_count = max(1, int(item.get("line_count", 0)))
        heading_count = int(item.get("heading_count", 0))
        density = round(heading_count / line_count, 4)
        scored.append(
            {
                "path": str(item.get("path", "")),
                "line_count": line_count,
                "heading_count": heading_count,
                "heading_density": density,
            }
        )
    lowest_density = sorted(scored, key=lambda row: (row["heading_density"], row["line_count"]), reverse=False)[
        :top_n
    ]
    result = {
        "top_n": top_n,
        "lowest_heading_density": lowest_density,
    }
    return {
        "result": result,
        "state_updates": {"heading_followup": result},
    }


def _worker_length_followup(inventory: List[Dict[str, Any]], threshold: int, top_n: int) -> Dict[str, Any]:
    risk_rows: List[Dict[str, Any]] = []
    for item in inventory:
        line_count = int(item.get("line_count", 0))
        if line_count < threshold:
            continue
        heading_count = int(item.get("heading_count", 0))
        heading_density = heading_count / max(1, line_count)
        risk_score = round(line_count * (1.0 - min(heading_density * 25.0, 1.0)), 2)
        risk_rows.append(
            {
                "path": str(item.get("path", "")),
                "line_count": line_count,
                "heading_count": heading_count,
                "heading_density": round(heading_density, 4),
                "risk_score": risk_score,
            }
        )
    top_risky = sorted(risk_rows, key=lambda row: (row["risk_score"], row["line_count"]), reverse=True)[:top_n]
    result = {
        "line_threshold": threshold,
        "top_n": top_n,
        "high_risk_long_files": top_risky,
    }
    return {
        "result": result,
        "state_updates": {"length_followup": result},
    }


def _worker_extension_hotspot_followup(inventory: List[Dict[str, Any]], top_n: int) -> Dict[str, Any]:
    totals: Dict[str, Dict[str, Any]] = {}
    for item in inventory:
        extension = str(item.get("extension", "<no_ext>") or "<no_ext>")
        bucket = totals.setdefault(
            extension,
            {
                "extension": extension,
                "file_count": 0,
                "total_lines": 0,
                "total_bytes": 0,
            },
        )
        bucket["file_count"] += 1
        bucket["total_lines"] += int(item.get("line_count", 0))
        bucket["total_bytes"] += int(item.get("byte_count", 0))
    hotspots = sorted(
        totals.values(),
        key=lambda row: (int(row["total_lines"]), int(row["file_count"]), str(row["extension"])),
        reverse=True,
    )[:top_n]
    result = {"top_n": top_n, "extension_hotspots": hotspots}
    return {
        "result": result,
        "state_updates": {"repo_extension_hotspots": result},
    }


def _worker_directory_hotspot_followup(inventory: List[Dict[str, Any]], top_n: int) -> Dict[str, Any]:
    totals: Dict[str, Dict[str, Any]] = {}
    for item in inventory:
        top_level_dir = str(item.get("top_level_dir", ".") or ".")
        bucket = totals.setdefault(
            top_level_dir,
            {
                "top_level_dir": top_level_dir,
                "file_count": 0,
                "total_lines": 0,
                "total_bytes": 0,
            },
        )
        bucket["file_count"] += 1
        bucket["total_lines"] += int(item.get("line_count", 0))
        bucket["total_bytes"] += int(item.get("byte_count", 0))
    busiest = sorted(
        totals.values(),
        key=lambda row: (int(row["total_lines"]), int(row["file_count"]), str(row["top_level_dir"])),
        reverse=True,
    )[:top_n]
    result = {"top_n": top_n, "busiest_directories": busiest}
    return {
        "result": result,
        "state_updates": {"repo_directory_hotspots": result},
    }


def run_tmux_worker_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    task_type = str(payload.get("task_type", "")).strip()
    task_payload = payload.get("task_payload", {})
    if not isinstance(task_payload, dict):
        task_payload = {}
    shared_state = payload.get("shared_state", {})
    if not isinstance(shared_state, dict):
        shared_state = {}

    if task_type == "discover_markdown":
        target_dir = pathlib.Path(str(payload.get("target_dir", "."))).resolve()
        output_dir = pathlib.Path(str(payload.get("output_dir", "."))).resolve()
        return _worker_discover_markdown(target_dir=target_dir, output_dir=output_dir)
    if task_type == "discover_repository":
        target_dir = pathlib.Path(str(payload.get("target_dir", "."))).resolve()
        output_dir = pathlib.Path(str(payload.get("output_dir", "."))).resolve()
        return _worker_discover_repository(target_dir=target_dir, output_dir=output_dir)

    inventory = shared_state.get("markdown_inventory", [])
    if not isinstance(inventory, list):
        inventory = []
    repository_inventory = shared_state.get("repository_inventory", [])
    if not isinstance(repository_inventory, list):
        repository_inventory = []

    if task_type == "heading_audit":
        return _worker_heading_audit(inventory=inventory)
    if task_type == "length_audit":
        threshold = int(task_payload.get("line_threshold", 200))
        return _worker_length_audit(inventory=inventory, threshold=threshold)
    if task_type == "extension_audit":
        return _worker_extension_audit(inventory=repository_inventory)
    if task_type == "large_file_audit":
        line_threshold = int(task_payload.get("line_threshold", 320))
        byte_threshold = int(task_payload.get("byte_threshold", 20000))
        return _worker_large_file_audit(
            inventory=repository_inventory,
            line_threshold=line_threshold,
            byte_threshold=byte_threshold,
        )
    if task_type == "heading_structure_followup":
        top_n = int(task_payload.get("top_n", 8))
        return _worker_heading_followup(inventory=inventory, top_n=top_n)
    if task_type == "length_risk_followup":
        top_n = int(task_payload.get("top_n", 8))
        threshold = int(task_payload.get("line_threshold", 180))
        return _worker_length_followup(inventory=inventory, threshold=threshold, top_n=top_n)
    if task_type == "extension_hotspot_followup":
        top_n = int(task_payload.get("top_n", 6))
        return _worker_extension_hotspot_followup(inventory=repository_inventory, top_n=top_n)
    if task_type == "directory_hotspot_followup":
        top_n = int(task_payload.get("top_n", 6))
        return _worker_directory_hotspot_followup(inventory=repository_inventory, top_n=top_n)
    if task_type == "dynamic_planning":
        return _run_handler_backed_worker(payload, handle_markdown_dynamic_planning)
    if task_type == "repo_dynamic_planning":
        return _run_handler_backed_worker(payload, handle_repo_dynamic_planning)
    if task_type == "peer_challenge":
        return _run_handler_backed_worker(payload, handle_shared_peer_challenge)
    if task_type == "evidence_pack":
        return _run_handler_backed_worker(payload, handle_shared_evidence_pack)
    if task_type == "llm_synthesis":
        workflow_pack = str(payload.get("workflow_pack", "markdown-audit"))
        if workflow_pack == "repo-audit":
            return _run_handler_backed_worker(payload, handle_repo_llm_synthesis)
        return _run_handler_backed_worker(payload, handle_markdown_llm_synthesis)
    if task_type == "recommendation_pack":
        return _run_handler_backed_worker(payload, handle_markdown_recommendation_pack)
    if task_type == "repo_recommendation_pack":
        return _run_handler_backed_worker(payload, handle_repo_recommendation_pack)

    raise ValueError(f"unsupported tmux worker task type: {task_type}")


def run_tmux_worker_entrypoint(
    task_file: pathlib.Path,
    run_payload_fn: Callable[[Dict[str, Any]], Dict[str, Any]] = run_tmux_worker_payload,
) -> int:
    try:
        payload = json.loads(task_file.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("worker payload must be a JSON object")
        result = run_payload_fn(payload)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except Exception as exc:
        print(f"worker_error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


def execute_worker_subprocess(command: List[str], timeout_sec: int) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
        timeout=timeout_sec,
    )


def execute_worker_tmux(
    command: List[str],
    workdir: pathlib.Path,
    session_prefix: str,
    timeout_sec: int,
    retain_session_for_reuse: bool = False,
    allow_existing_session_reuse: bool = False,
) -> subprocess.CompletedProcess[str]:
    ipc_dir = workdir / "_tmux_worker_ipc"
    ipc_dir.mkdir(parents=True, exist_ok=True)
    lifecycle: Dict[str, Any] = {
        "tmux_session_name": "",
        "tmux_preferred_session_name": "",
        "tmux_session_name_strategy": "",
        "tmux_preferred_session_found_preflight": False,
        "tmux_preferred_session_retried": False,
        "tmux_preferred_session_reused": False,
        "tmux_preferred_session_reuse_attempted": False,
        "tmux_preferred_session_reuse_result": "",
        "tmux_preferred_session_reuse_error": "",
        "tmux_preferred_session_reused_existing": False,
        "tmux_preferred_session_reuse_authorized": allow_existing_session_reuse,
        "tmux_reuse_retention_requested": retain_session_for_reuse,
        "tmux_session_retained_for_reuse": False,
        "tmux_session_started": False,
        "tmux_orphan_cleanup_attempted": False,
        "tmux_orphan_server_running": False,
        "tmux_orphan_sessions_found": 0,
        "tmux_orphan_sessions_cleaned": 0,
        "tmux_orphan_sessions_failed": 0,
        "tmux_orphan_failed_sessions": [],
        "tmux_orphan_cleanup_error": "",
        "tmux_spawn_attempts": 0,
        "tmux_spawn_retried": False,
        "tmux_spawn_retry_reason": "",
        "tmux_spawn_error": "",
        "tmux_stale_session_cleanup_attempted": False,
        "tmux_stale_session_name": "",
        "tmux_stale_session_cleanup_result": "",
        "tmux_stale_session_cleanup_error": "",
        "tmux_stale_session_cleanup_retry_attempted": False,
        "tmux_stale_session_cleanup_retry_result": "",
        "tmux_stale_session_cleanup_retry_error": "",
        "tmux_stale_session_exists_after_cleanup": False,
        "tmux_stale_session_cleanup_verification_error": "",
        "tmux_cleanup_retry_attempted": False,
        "tmux_cleanup_retry_result": "",
        "tmux_cleanup_retry_error": "",
        "tmux_session_exists_after_cleanup": False,
        "tmux_cleanup_verification_error": "",
        "tmux_status_observed": False,
        "tmux_timed_out": False,
        "tmux_cleanup_result": "",
        "tmux_cleanup_error": "",
        "tmux_ipc_cleanup_result": "",
        "tmux_ipc_cleanup_error": "",
        "tmux_ipc_files_removed": 0,
    }

    orphan_cleanup = _cleanup_orphan_tmux_sessions(
        session_prefix=session_prefix,
        keep_exact_session=allow_existing_session_reuse,
    )
    lifecycle["tmux_orphan_cleanup_attempted"] = bool(orphan_cleanup["attempted"])
    lifecycle["tmux_orphan_server_running"] = bool(orphan_cleanup["server_running"])
    lifecycle["tmux_preferred_session_found_preflight"] = bool(orphan_cleanup.get("preferred_session_found", False))
    lifecycle["tmux_orphan_sessions_found"] = int(orphan_cleanup["sessions_found"])
    lifecycle["tmux_orphan_sessions_cleaned"] = int(orphan_cleanup["sessions_cleaned"])
    lifecycle["tmux_orphan_sessions_failed"] = int(orphan_cleanup["sessions_failed"])
    lifecycle["tmux_orphan_failed_sessions"] = list(orphan_cleanup["failed_sessions"])
    lifecycle["tmux_orphan_cleanup_error"] = str(orphan_cleanup["error"])

    spawn_result = _spawn_tmux_session(
        command=command,
        ipc_dir=ipc_dir,
        session_prefix=session_prefix,
        preferred_session_found=bool(orphan_cleanup.get("preferred_session_found", False)),
        allow_existing_session_reuse=allow_existing_session_reuse,
    )
    session_name = str(spawn_result.get("session_name", ""))
    stdout_file = pathlib.Path(spawn_result.get("stdout_file"))
    stderr_file = pathlib.Path(spawn_result.get("stderr_file"))
    status_file = pathlib.Path(spawn_result.get("status_file"))
    ipc_files = [stdout_file, stderr_file, status_file]
    spawn = spawn_result.get("spawn")
    if not isinstance(spawn, subprocess.CompletedProcess):
        spawn = subprocess.CompletedProcess(args=["tmux"], returncode=1, stdout="", stderr="tmux spawn failed")

    lifecycle["tmux_session_name"] = session_name
    lifecycle["tmux_preferred_session_name"] = str(spawn_result.get("preferred_session_name", ""))
    lifecycle["tmux_session_name_strategy"] = str(spawn_result.get("session_name_strategy", ""))
    lifecycle["tmux_preferred_session_retried"] = bool(spawn_result.get("preferred_session_retried", False))
    lifecycle["tmux_preferred_session_reused"] = bool(spawn_result.get("preferred_session_reused", False))
    lifecycle["tmux_preferred_session_reuse_attempted"] = bool(
        spawn_result.get("preferred_session_reuse_attempted", False)
    )
    lifecycle["tmux_preferred_session_reuse_result"] = str(
        spawn_result.get("preferred_session_reuse_result", "")
    )
    lifecycle["tmux_preferred_session_reuse_error"] = str(
        spawn_result.get("preferred_session_reuse_error", "")
    )
    lifecycle["tmux_preferred_session_reused_existing"] = bool(
        spawn_result.get("preferred_session_reused_existing", False)
    )
    lifecycle["tmux_session_started"] = spawn.returncode == 0
    lifecycle["tmux_spawn_attempts"] = int(spawn_result.get("attempts", 0))
    lifecycle["tmux_spawn_retried"] = bool(spawn_result.get("retried", False))
    lifecycle["tmux_spawn_retry_reason"] = str(spawn_result.get("retry_reason", ""))
    lifecycle["tmux_spawn_error"] = str(spawn_result.get("spawn_error", ""))
    lifecycle["tmux_stale_session_cleanup_attempted"] = bool(spawn_result.get("stale_cleanup_attempted", False))
    lifecycle["tmux_stale_session_name"] = str(spawn_result.get("stale_cleanup_session_name", ""))
    lifecycle["tmux_stale_session_cleanup_result"] = str(spawn_result.get("stale_cleanup_result", ""))
    lifecycle["tmux_stale_session_cleanup_error"] = str(spawn_result.get("stale_cleanup_error", ""))
    lifecycle["tmux_stale_session_cleanup_retry_attempted"] = bool(
        spawn_result.get("stale_cleanup_retry_attempted", False)
    )
    lifecycle["tmux_stale_session_cleanup_retry_result"] = str(
        spawn_result.get("stale_cleanup_retry_result", "")
    )
    lifecycle["tmux_stale_session_cleanup_retry_error"] = str(
        spawn_result.get("stale_cleanup_retry_error", "")
    )
    lifecycle["tmux_stale_session_exists_after_cleanup"] = bool(
        spawn_result.get("stale_session_exists_after_cleanup", False)
    )
    lifecycle["tmux_stale_session_cleanup_verification_error"] = str(
        spawn_result.get("stale_cleanup_verification_error", "")
    )
    if spawn.returncode != 0:
        cleanup = _cleanup_tmux_ipc_files(ipc_files)
        lifecycle["tmux_cleanup_result"] = "spawn_failed"
        lifecycle["tmux_cleanup_error"] = str(spawn_result.get("spawn_error", "") or (spawn.stderr or "").strip())[:400]
        lifecycle["tmux_ipc_cleanup_result"] = cleanup["result"]
        lifecycle["tmux_ipc_cleanup_error"] = cleanup["error"]
        lifecycle["tmux_ipc_files_removed"] = cleanup["removed"]
        return _attach_tmux_lifecycle(
            subprocess.CompletedProcess(
                args=command,
                returncode=spawn.returncode,
                stdout="",
                stderr=f"tmux spawn failed: {spawn.stderr.strip()}",
            ),
            lifecycle,
        )

    deadline = time.time() + timeout_sec
    while time.time() < deadline and not status_file.exists():
        time.sleep(TMUX_SESSION_POLL_INTERVAL_SEC)

    lifecycle["tmux_status_observed"] = status_file.exists()
    if not status_file.exists():
        cleanup = _cleanup_active_tmux_session(session_name)
        lifecycle["tmux_cleanup_result"] = cleanup["result"]
        lifecycle["tmux_cleanup_error"] = cleanup["error"]
        lifecycle["tmux_cleanup_retry_attempted"] = bool(cleanup["retry_attempted"])
        lifecycle["tmux_cleanup_retry_result"] = str(cleanup["retry_result"])
        lifecycle["tmux_cleanup_retry_error"] = str(cleanup["retry_error"])
        lifecycle["tmux_session_exists_after_cleanup"] = bool(cleanup["session_exists_after_cleanup"])
        lifecycle["tmux_cleanup_verification_error"] = str(cleanup["verification_error"])
        lifecycle["tmux_timed_out"] = True
        stdout = stdout_file.read_text(encoding="utf-8", errors="ignore") if stdout_file.exists() else ""
        stderr = stderr_file.read_text(encoding="utf-8", errors="ignore") if stderr_file.exists() else ""
        if not stderr:
            stderr = "tmux worker timed out"
        ipc_cleanup = _cleanup_tmux_ipc_files(ipc_files)
        lifecycle["tmux_ipc_cleanup_result"] = ipc_cleanup["result"]
        lifecycle["tmux_ipc_cleanup_error"] = ipc_cleanup["error"]
        lifecycle["tmux_ipc_files_removed"] = ipc_cleanup["removed"]
        return _attach_tmux_lifecycle(
            subprocess.CompletedProcess(
                args=command,
                returncode=124,
                stdout=stdout,
                stderr=stderr,
            ),
            lifecycle,
        )

    try:
        returncode = int(status_file.read_text(encoding="utf-8").strip() or "1")
    except ValueError:
        returncode = 1
    stdout = stdout_file.read_text(encoding="utf-8", errors="ignore") if stdout_file.exists() else ""
    stderr = stderr_file.read_text(encoding="utf-8", errors="ignore") if stderr_file.exists() else ""
    if (
        returncode == 0
        and retain_session_for_reuse
        and session_name == str(spawn_result.get("preferred_session_name", ""))
        and str(spawn_result.get("session_name_strategy", "")) in {"preferred", "preferred_reused_existing"}
    ):
        lifecycle["tmux_cleanup_result"] = "leased_for_reuse"
        lifecycle["tmux_cleanup_error"] = ""
        lifecycle["tmux_cleanup_retry_attempted"] = False
        lifecycle["tmux_cleanup_retry_result"] = ""
        lifecycle["tmux_cleanup_retry_error"] = ""
        lifecycle["tmux_session_exists_after_cleanup"] = True
        lifecycle["tmux_cleanup_verification_error"] = ""
        lifecycle["tmux_session_retained_for_reuse"] = True
    else:
        cleanup = _cleanup_active_tmux_session(session_name)
        lifecycle["tmux_cleanup_result"] = cleanup["result"]
        lifecycle["tmux_cleanup_error"] = cleanup["error"]
        lifecycle["tmux_cleanup_retry_attempted"] = bool(cleanup["retry_attempted"])
        lifecycle["tmux_cleanup_retry_result"] = str(cleanup["retry_result"])
        lifecycle["tmux_cleanup_retry_error"] = str(cleanup["retry_error"])
        lifecycle["tmux_session_exists_after_cleanup"] = bool(cleanup["session_exists_after_cleanup"])
        lifecycle["tmux_cleanup_verification_error"] = str(cleanup["verification_error"])
    ipc_cleanup = _cleanup_tmux_ipc_files(ipc_files)
    lifecycle["tmux_ipc_cleanup_result"] = ipc_cleanup["result"]
    lifecycle["tmux_ipc_cleanup_error"] = ipc_cleanup["error"]
    lifecycle["tmux_ipc_files_removed"] = ipc_cleanup["removed"]
    return _attach_tmux_lifecycle(
        subprocess.CompletedProcess(args=command, returncode=returncode, stdout=stdout, stderr=stderr),
        lifecycle,
    )


def _merge_tmux_lifecycle_into_diagnostics(
    diagnostics: Dict[str, Any],
    completed: subprocess.CompletedProcess[str],
) -> Dict[str, Any]:
    lifecycle = _extract_tmux_lifecycle(completed)
    if not lifecycle:
        return {}
    for key in (
        "tmux_session_name",
        "tmux_preferred_session_name",
        "tmux_session_name_strategy",
        "tmux_preferred_session_found_preflight",
        "tmux_preferred_session_retried",
        "tmux_preferred_session_reused",
        "tmux_preferred_session_reuse_attempted",
        "tmux_preferred_session_reuse_result",
        "tmux_preferred_session_reuse_error",
        "tmux_preferred_session_reused_existing",
        "tmux_preferred_session_reuse_authorized",
        "tmux_reuse_retention_requested",
        "tmux_session_retained_for_reuse",
        "tmux_session_started",
        "tmux_orphan_cleanup_attempted",
        "tmux_orphan_server_running",
        "tmux_orphan_sessions_found",
        "tmux_orphan_sessions_cleaned",
        "tmux_orphan_sessions_failed",
        "tmux_orphan_failed_sessions",
        "tmux_orphan_cleanup_error",
        "tmux_spawn_attempts",
        "tmux_spawn_retried",
        "tmux_spawn_retry_reason",
        "tmux_spawn_error",
        "tmux_stale_session_cleanup_attempted",
        "tmux_stale_session_name",
        "tmux_stale_session_cleanup_result",
        "tmux_stale_session_cleanup_error",
        "tmux_stale_session_cleanup_retry_attempted",
        "tmux_stale_session_cleanup_retry_result",
        "tmux_stale_session_cleanup_retry_error",
        "tmux_stale_session_exists_after_cleanup",
        "tmux_stale_session_cleanup_verification_error",
        "tmux_cleanup_retry_attempted",
        "tmux_cleanup_retry_result",
        "tmux_cleanup_retry_error",
        "tmux_session_exists_after_cleanup",
        "tmux_cleanup_verification_error",
        "tmux_status_observed",
        "tmux_timed_out",
        "tmux_cleanup_result",
        "tmux_cleanup_error",
        "tmux_ipc_cleanup_result",
        "tmux_ipc_cleanup_error",
        "tmux_ipc_files_removed",
    ):
        diagnostics[key] = lifecycle.get(key, diagnostics.get(key))
    return lifecycle


def _merge_transport_timeout_into_diagnostics(
    diagnostics: Dict[str, Any],
    completed: subprocess.CompletedProcess[str],
) -> Dict[str, Any]:
    timeout_metadata = _extract_transport_timeout(completed)
    if not timeout_metadata:
        return {}
    for key in (
        "execution_timed_out",
        "timeout_transport",
        "timeout_phase",
        "timeout_message",
    ):
        diagnostics[key] = timeout_metadata.get(key, diagnostics.get(key))
    return timeout_metadata


def run_tmux_worker_task(
    runtime_script: pathlib.Path,
    output_dir: pathlib.Path,
    runtime_config: RuntimeConfig,
    payload: Dict[str, Any],
    worker_name: str,
    logger: EventLogger,
    timeout_sec: int = 120,
    retain_session_for_reuse: bool = False,
    allow_existing_session_reuse: bool = False,
    execute_worker_tmux_fn: Callable[
        [List[str], pathlib.Path, str, int, bool, bool], subprocess.CompletedProcess[str]
    ] = execute_worker_tmux,
    execute_worker_subprocess_fn: Callable[
        [List[str], int], subprocess.CompletedProcess[str]
    ] = execute_worker_subprocess,
    which_fn: Callable[[str], str | None] = shutil.which,
) -> Dict[str, Any]:
    payload_dir = output_dir / "_tmux_worker_payloads"
    payload_dir.mkdir(parents=True, exist_ok=True)
    payload_file = payload_dir / f"{worker_name}_{uuid.uuid4().hex}.json"
    payload_file.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    command = [
        sys.executable,
        str(runtime_script),
        "--worker-task-file",
        str(payload_file),
    ]
    transport = "subprocess"
    completed: subprocess.CompletedProcess[str]
    started_at = time.time()
    requested_transport = str(runtime_config.teammate_mode)
    fallback_reason = ""
    diagnostics: Dict[str, Any] = {
        "recorded_at": utc_now(),
        "worker": worker_name,
        "task_type": str(payload.get("task_type", "")),
        "transport_requested": requested_transport,
        "transport_used": "",
        "fallback_used": False,
        "fallback_reason": "",
        "timeout_sec": timeout_sec,
        "returncode": "",
        "duration_ms": 0,
        "stdout_preview": "",
        "stderr_preview": "",
        "result": "unknown",
        "error": "",
        "execution_timed_out": False,
        "timeout_transport": "",
        "timeout_phase": "",
        "timeout_message": "",
        "tmux_session_name": "",
        "tmux_preferred_session_name": "",
        "tmux_session_name_strategy": "",
        "tmux_preferred_session_found_preflight": False,
        "tmux_preferred_session_retried": False,
        "tmux_preferred_session_reused": False,
        "tmux_preferred_session_reuse_attempted": False,
        "tmux_preferred_session_reuse_result": "",
        "tmux_preferred_session_reuse_error": "",
        "tmux_preferred_session_reused_existing": False,
        "tmux_preferred_session_reuse_authorized": allow_existing_session_reuse,
        "tmux_reuse_retention_requested": retain_session_for_reuse,
        "tmux_session_retained_for_reuse": False,
        "tmux_session_started": False,
        "tmux_orphan_cleanup_attempted": False,
        "tmux_orphan_server_running": False,
        "tmux_orphan_sessions_found": 0,
        "tmux_orphan_sessions_cleaned": 0,
        "tmux_orphan_sessions_failed": 0,
        "tmux_orphan_failed_sessions": [],
        "tmux_orphan_cleanup_error": "",
        "tmux_spawn_attempts": 0,
        "tmux_spawn_retried": False,
        "tmux_spawn_retry_reason": "",
        "tmux_spawn_error": "",
        "tmux_stale_session_cleanup_attempted": False,
        "tmux_stale_session_name": "",
        "tmux_stale_session_cleanup_result": "",
        "tmux_stale_session_cleanup_error": "",
        "tmux_stale_session_cleanup_retry_attempted": False,
        "tmux_stale_session_cleanup_retry_result": "",
        "tmux_stale_session_cleanup_retry_error": "",
        "tmux_stale_session_exists_after_cleanup": False,
        "tmux_stale_session_cleanup_verification_error": "",
        "tmux_cleanup_retry_attempted": False,
        "tmux_cleanup_retry_result": "",
        "tmux_cleanup_retry_error": "",
        "tmux_session_exists_after_cleanup": False,
        "tmux_cleanup_verification_error": "",
        "tmux_status_observed": False,
        "tmux_timed_out": False,
        "tmux_cleanup_result": "",
        "tmux_cleanup_error": "",
        "tmux_ipc_cleanup_result": "",
        "tmux_ipc_cleanup_error": "",
        "tmux_ipc_files_removed": 0,
    }

    def _run_subprocess_fallback(reason: str) -> subprocess.CompletedProcess[str]:
        nonlocal fallback_reason
        fallback_reason = reason
        diagnostics["fallback_used"] = True
        diagnostics["fallback_reason"] = reason
        logger.log(
            "tmux_worker_fallback_attempt",
            worker=worker_name,
            reason=reason,
        )
        fallback_started = time.time()
        try:
            fallback_completed = execute_worker_subprocess_fn(command=command, timeout_sec=timeout_sec)
        except subprocess.TimeoutExpired as exc:
            fallback_completed = _completed_process_from_timeout(
                command=command,
                timeout_exc=exc,
                transport="tmux->subprocess_fallback",
                phase="fallback_subprocess",
            )
            timeout_metadata = _extract_transport_timeout(fallback_completed)
            logger.log(
                "tmux_worker_transport_timeout",
                worker=worker_name,
                transport=str(timeout_metadata.get("timeout_transport", "tmux->subprocess_fallback")),
                phase=str(timeout_metadata.get("timeout_phase", "fallback_subprocess")),
                timeout_sec=timeout_sec,
                stdout_len=len(fallback_completed.stdout or ""),
                stderr_len=len(fallback_completed.stderr or ""),
            )
        logger.log(
            "tmux_worker_fallback_result",
            worker=worker_name,
            returncode=fallback_completed.returncode,
            duration_ms=int((time.time() - fallback_started) * 1000),
            stdout_len=len(fallback_completed.stdout or ""),
            stderr_len=len(fallback_completed.stderr or ""),
        )
        return fallback_completed

    def _run_primary_subprocess(transport_name: str, phase: str) -> subprocess.CompletedProcess[str]:
        try:
            return execute_worker_subprocess_fn(command=command, timeout_sec=timeout_sec)
        except subprocess.TimeoutExpired as exc:
            completed_timeout = _completed_process_from_timeout(
                command=command,
                timeout_exc=exc,
                transport=transport_name,
                phase=phase,
            )
            timeout_metadata = _extract_transport_timeout(completed_timeout)
            logger.log(
                "tmux_worker_transport_timeout",
                worker=worker_name,
                transport=str(timeout_metadata.get("timeout_transport", transport_name)),
                phase=str(timeout_metadata.get("timeout_phase", phase)),
                timeout_sec=timeout_sec,
                stdout_len=len(completed_timeout.stdout or ""),
                stderr_len=len(completed_timeout.stderr or ""),
            )
            return completed_timeout

    try:
        if runtime_config.teammate_mode == "tmux":
            if which_fn("tmux"):
                transport = "tmux"
                completed = execute_worker_tmux_fn(
                    command=command,
                    workdir=output_dir,
                    session_prefix=preferred_tmux_session_name(worker_name),
                    timeout_sec=timeout_sec,
                    retain_session_for_reuse=retain_session_for_reuse,
                    allow_existing_session_reuse=allow_existing_session_reuse,
                )
            else:
                fallback_reason = "tmux binary not found"
                diagnostics["fallback_used"] = True
                diagnostics["fallback_reason"] = fallback_reason
                logger.log(
                    "tmux_unavailable_fallback_subprocess",
                    worker=worker_name,
                    reason=fallback_reason,
                )
                completed = _run_primary_subprocess(
                    transport_name="subprocess",
                    phase="tmux_unavailable_fallback",
                )
        else:
            completed = _run_primary_subprocess(
                transport_name="subprocess",
                phase="primary_subprocess",
            )

        lifecycle = _merge_tmux_lifecycle_into_diagnostics(diagnostics=diagnostics, completed=completed)
        timeout_metadata = _merge_transport_timeout_into_diagnostics(diagnostics=diagnostics, completed=completed)
        diagnostics["transport_used"] = transport
        diagnostics["returncode"] = completed.returncode
        diagnostics["duration_ms"] = int((time.time() - started_at) * 1000)
        diagnostics["stdout_preview"] = _output_preview(completed.stdout or "")
        diagnostics["stderr_preview"] = _output_preview(completed.stderr or "")
        logger.log(
            "tmux_worker_transport_result",
            worker=worker_name,
            transport=transport,
            returncode=completed.returncode,
            duration_ms=int((time.time() - started_at) * 1000),
            stdout_len=len(completed.stdout or ""),
            stderr_len=len(completed.stderr or ""),
            tmux_timed_out=bool(lifecycle.get("tmux_timed_out", False)),
            tmux_session_name_strategy=str(lifecycle.get("tmux_session_name_strategy", "")),
            tmux_preferred_session_found_preflight=bool(
                lifecycle.get("tmux_preferred_session_found_preflight", False)
            ),
            tmux_preferred_session_retried=bool(lifecycle.get("tmux_preferred_session_retried", False)),
            tmux_preferred_session_reused=bool(lifecycle.get("tmux_preferred_session_reused", False)),
            tmux_preferred_session_reuse_attempted=bool(
                lifecycle.get("tmux_preferred_session_reuse_attempted", False)
            ),
            tmux_preferred_session_reuse_result=str(
                lifecycle.get("tmux_preferred_session_reuse_result", "")
            ),
            tmux_preferred_session_reuse_authorized=bool(
                lifecycle.get("tmux_preferred_session_reuse_authorized", False)
            ),
            tmux_preferred_session_reused_existing=bool(
                lifecycle.get("tmux_preferred_session_reused_existing", False)
            ),
            tmux_reuse_retention_requested=bool(lifecycle.get("tmux_reuse_retention_requested", False)),
            tmux_session_retained_for_reuse=bool(lifecycle.get("tmux_session_retained_for_reuse", False)),
            tmux_cleanup_result=str(lifecycle.get("tmux_cleanup_result", "")),
            tmux_orphan_sessions_found=int(lifecycle.get("tmux_orphan_sessions_found", 0) or 0),
            tmux_orphan_sessions_cleaned=int(lifecycle.get("tmux_orphan_sessions_cleaned", 0) or 0),
            tmux_spawn_attempts=int(lifecycle.get("tmux_spawn_attempts", 0) or 0),
            tmux_spawn_retried=bool(lifecycle.get("tmux_spawn_retried", False)),
            tmux_stale_session_cleanup_attempted=bool(lifecycle.get("tmux_stale_session_cleanup_attempted", False)),
            tmux_stale_session_cleanup_result=str(lifecycle.get("tmux_stale_session_cleanup_result", "")),
            tmux_stale_session_cleanup_retry_attempted=bool(
                lifecycle.get("tmux_stale_session_cleanup_retry_attempted", False)
            ),
            tmux_stale_session_cleanup_retry_result=str(
                lifecycle.get("tmux_stale_session_cleanup_retry_result", "")
            ),
            tmux_cleanup_retry_attempted=bool(lifecycle.get("tmux_cleanup_retry_attempted", False)),
            tmux_cleanup_retry_result=str(lifecycle.get("tmux_cleanup_retry_result", "")),
            execution_timed_out=bool(timeout_metadata.get("execution_timed_out", False)),
            timeout_phase=str(timeout_metadata.get("timeout_phase", "")),
        )

        if completed.returncode != 0:
            if (
                transport == "tmux"
                and runtime_config.tmux_fallback_on_error
                and runtime_config.teammate_mode == "tmux"
            ):
                completed = _run_subprocess_fallback(reason=f"tmux_returncode={completed.returncode}")
                transport = "tmux->subprocess_fallback"
                diagnostics["transport_used"] = transport
                timeout_metadata = _merge_transport_timeout_into_diagnostics(
                    diagnostics=diagnostics,
                    completed=completed,
                )
                diagnostics["returncode"] = completed.returncode
                diagnostics["duration_ms"] = int((time.time() - started_at) * 1000)
                diagnostics["stdout_preview"] = _output_preview(completed.stdout or "")
                diagnostics["stderr_preview"] = _output_preview(completed.stderr or "")
            else:
                stderr = (completed.stderr or "").strip()
                stdout = (completed.stdout or "").strip()
                detail = (
                    str(timeout_metadata.get("timeout_message", "")).strip()
                    or stderr
                    or stdout
                    or f"worker exited with code {completed.returncode}"
                )
                if diagnostics.get("execution_timed_out"):
                    diagnostics["result"] = "timeout"
                    diagnostics["error"] = f"worker timed out via {transport}: {detail[:400]}"
                else:
                    diagnostics["result"] = "execution_failed"
                    diagnostics["error"] = f"worker execution failed via {transport}: {detail[:400]}"
                return {
                    "ok": False,
                    "error": diagnostics["error"],
                    "transport": transport,
                    "diagnostics": dict(diagnostics),
                }

        if completed.returncode != 0:
            stderr = (completed.stderr or "").strip()
            stdout = (completed.stdout or "").strip()
            detail = (
                str(timeout_metadata.get("timeout_message", "")).strip()
                or stderr
                or stdout
                or f"worker exited with code {completed.returncode}"
            )
            if diagnostics.get("execution_timed_out"):
                diagnostics["result"] = "timeout"
                diagnostics["error"] = f"worker timed out via {transport}: {detail[:400]}"
            else:
                diagnostics["result"] = "execution_failed"
                diagnostics["error"] = f"worker execution failed via {transport}: {detail[:400]}"
            return {
                "ok": False,
                "error": diagnostics["error"],
                "transport": transport,
                "diagnostics": dict(diagnostics),
            }

        try:
            parsed = json.loads((completed.stdout or "").strip())
        except json.JSONDecodeError as exc:
            if (
                transport == "tmux"
                and runtime_config.tmux_fallback_on_error
                and runtime_config.teammate_mode == "tmux"
            ):
                completed = _run_subprocess_fallback(reason=f"tmux_invalid_json={exc}")
                transport = "tmux->subprocess_fallback"
                diagnostics["transport_used"] = transport
                timeout_metadata = _merge_transport_timeout_into_diagnostics(
                    diagnostics=diagnostics,
                    completed=completed,
                )
                diagnostics["returncode"] = completed.returncode
                diagnostics["duration_ms"] = int((time.time() - started_at) * 1000)
                diagnostics["stdout_preview"] = _output_preview(completed.stdout or "")
                diagnostics["stderr_preview"] = _output_preview(completed.stderr or "")
                if completed.returncode != 0:
                    stderr = (completed.stderr or "").strip()
                    stdout = (completed.stdout or "").strip()
                    detail = (
                        str(timeout_metadata.get("timeout_message", "")).strip()
                        or stderr
                        or stdout
                        or f"worker exited with code {completed.returncode}"
                    )
                    if diagnostics.get("execution_timed_out"):
                        diagnostics["result"] = "timeout"
                        diagnostics["error"] = f"worker timed out via {transport}: {detail[:400]}"
                    else:
                        diagnostics["result"] = "execution_failed"
                        diagnostics["error"] = f"worker execution failed via {transport}: {detail[:400]}"
                    return {
                        "ok": False,
                        "error": diagnostics["error"],
                        "transport": transport,
                        "diagnostics": dict(diagnostics),
                    }
                try:
                    parsed = json.loads((completed.stdout or "").strip())
                except json.JSONDecodeError as exc2:
                    diagnostics["result"] = "invalid_json"
                    diagnostics["error"] = f"worker returned invalid JSON via {transport}: {exc2}"
                    return {
                        "ok": False,
                        "error": diagnostics["error"],
                        "transport": transport,
                        "diagnostics": dict(diagnostics),
                    }
            else:
                diagnostics["result"] = "invalid_json"
                diagnostics["error"] = f"worker returned invalid JSON via {transport}: {exc}"
                return {
                    "ok": False,
                    "error": diagnostics["error"],
                    "transport": transport,
                    "diagnostics": dict(diagnostics),
                }
        if not isinstance(parsed, dict):
            diagnostics["result"] = "non_object_payload"
            diagnostics["error"] = f"worker returned non-object payload via {transport}"
            return {
                "ok": False,
                "error": diagnostics["error"],
                "transport": transport,
                "diagnostics": dict(diagnostics),
            }
        diagnostics["result"] = "success"
        return {
            "ok": True,
            "payload": parsed,
            "transport": transport,
            "diagnostics": dict(diagnostics),
        }
    finally:
        diagnostics["transport_used"] = diagnostics.get("transport_used") or transport
        diagnostics["duration_ms"] = int((time.time() - started_at) * 1000)
        if diagnostics.get("returncode", "") == "" and "completed" in locals():
            diagnostics["returncode"] = completed.returncode
            diagnostics["stdout_preview"] = _output_preview(completed.stdout or "")
            diagnostics["stderr_preview"] = _output_preview(completed.stderr or "")
        _append_tmux_worker_diagnostics(output_dir=output_dir, record=diagnostics)
        try:
            payload_file.unlink(missing_ok=True)
        except OSError:
            pass


def run_tmux_analyst_task_once(
    lead_context: Any,
    analyst_profiles: Sequence[AgentProfile],
    runtime_script: pathlib.Path,
    run_worker_task_fn: Callable[..., Dict[str, Any]],
    supported_task_types: Collection[str],
    worker_timeout_sec: int = 120,
) -> bool:
    if not analyst_profiles:
        return False
    rr_index = int(lead_context.shared_state.get("_tmux_rr_index", 0))
    rr_index = rr_index % len(analyst_profiles)
    ordered_profiles = list(analyst_profiles[rr_index:]) + list(analyst_profiles[:rr_index])

    for offset, profile in enumerate(ordered_profiles):
        task = lead_context.board.claim_next(
            agent_name=profile.name,
            agent_skills=profile.skills,
            agent_type=profile.agent_type,
        )
        if task is None:
            continue
        next_index = (rr_index + offset + 1) % len(analyst_profiles)
        lead_context.shared_state.set("_tmux_rr_index", next_index)

        lock_paths = [str(pathlib.Path(path).resolve()) for path in task.locked_paths]
        if lock_paths and not lead_context.file_locks.acquire(profile.name, lock_paths):
            lead_context.board.defer(task_id=task.task_id, owner=profile.name, reason="file lock unavailable")
            return True

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
        if task.task_type not in supported_task_types:
            error = f"unsupported analyst task type for tmux mode: {task.task_type}"
            lead_context.board.fail(task_id=task.task_id, owner=profile.name, error=error)
            lead_context.mailbox.send(
                sender=profile.name,
                recipient=lead_context.profile.name,
                subject="task_failed",
                body=error,
                task_id=task.task_id,
            )
            if lock_paths:
                lead_context.file_locks.release(profile.name, lock_paths)
            return True

        payload = {
            "task_type": task.task_type,
            "task_payload": task.payload,
            "target_dir": str(lead_context.target_dir),
            "output_dir": str(lead_context.output_dir),
            "shared_state": lead_context.shared_state.snapshot(),
        }
        board_snapshot = lead_context.board.snapshot()
        retain_session_for_reuse = any(
            str(item.get("task_id", "")) != task.task_id
            and str(item.get("task_type", "")) in supported_task_types
            and str(item.get("status", "")) in {"pending", "blocked"}
            for item in board_snapshot.get("tasks", [])
        )
        allow_existing_session_reuse = _lease_allows_preferred_session_reuse(
            shared_state=lead_context.shared_state,
            worker_name=profile.name,
        )
        lead_context.logger.log(
            "tmux_worker_task_dispatched",
            worker=profile.name,
            task_id=task.task_id,
            task_type=task.task_type,
            retain_session_for_reuse=retain_session_for_reuse,
            allow_existing_session_reuse=allow_existing_session_reuse,
        )
        execution = run_worker_task_fn(
            runtime_script=runtime_script,
            output_dir=lead_context.output_dir,
            runtime_config=lead_context.runtime_config,
            payload=payload,
            worker_name=profile.name,
            logger=lead_context.logger,
            timeout_sec=worker_timeout_sec,
            retain_session_for_reuse=retain_session_for_reuse,
            allow_existing_session_reuse=allow_existing_session_reuse,
        )
        if not execution.get("ok"):
            error = str(execution.get("error", "unknown worker error"))
            execution_diagnostics = execution.get("diagnostics", {})
            transport = str(execution.get("transport", ""))
            lease_status = "failed"
            if "subprocess" in transport:
                lease_status = "fallback_subprocess"
            _update_tmux_session_lease(
                lead_context=lead_context,
                worker_name=profile.name,
                session_name=str(
                    execution_diagnostics.get("tmux_preferred_session_name", "")
                    or preferred_tmux_session_name(profile.name)
                ),
                status=lease_status,
                task_id=task.task_id,
                task_type=task.task_type,
                transport=transport,
                cleanup_result=str(execution_diagnostics.get("tmux_cleanup_result", "")),
                retained_for_reuse=bool(execution_diagnostics.get("tmux_session_retained_for_reuse", False)),
                reused_existing=bool(
                    execution_diagnostics.get("tmux_preferred_session_reused_existing", False)
                ),
                reuse_authorized=allow_existing_session_reuse,
                error=error,
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
                "tmux_worker_task_failed",
                worker=profile.name,
                task_id=task.task_id,
                error=error,
                transport=execution.get("transport"),
                fallback_used=bool(execution_diagnostics.get("fallback_used", False)),
                fallback_reason=str(execution_diagnostics.get("fallback_reason", "")),
                execution_timed_out=bool(execution_diagnostics.get("execution_timed_out", False)),
                timeout_phase=str(execution_diagnostics.get("timeout_phase", "")),
                tmux_timed_out=bool(execution_diagnostics.get("tmux_timed_out", False)),
                tmux_session_name_strategy=str(execution_diagnostics.get("tmux_session_name_strategy", "")),
                tmux_preferred_session_found_preflight=bool(
                    execution_diagnostics.get("tmux_preferred_session_found_preflight", False)
                ),
                tmux_preferred_session_retried=bool(
                    execution_diagnostics.get("tmux_preferred_session_retried", False)
                ),
                tmux_preferred_session_reused=bool(
                    execution_diagnostics.get("tmux_preferred_session_reused", False)
                ),
                tmux_preferred_session_reuse_attempted=bool(
                    execution_diagnostics.get("tmux_preferred_session_reuse_attempted", False)
                ),
                tmux_preferred_session_reuse_result=str(
                    execution_diagnostics.get("tmux_preferred_session_reuse_result", "")
                ),
                tmux_preferred_session_reuse_authorized=bool(
                    execution_diagnostics.get("tmux_preferred_session_reuse_authorized", False)
                ),
                tmux_preferred_session_reused_existing=bool(
                    execution_diagnostics.get("tmux_preferred_session_reused_existing", False)
                ),
                tmux_session_retained_for_reuse=bool(
                    execution_diagnostics.get("tmux_session_retained_for_reuse", False)
                ),
                tmux_orphan_sessions_found=int(execution_diagnostics.get("tmux_orphan_sessions_found", 0) or 0),
                tmux_orphan_sessions_cleaned=int(
                    execution_diagnostics.get("tmux_orphan_sessions_cleaned", 0) or 0
                ),
                tmux_spawn_attempts=int(execution_diagnostics.get("tmux_spawn_attempts", 0) or 0),
                tmux_spawn_retried=bool(execution_diagnostics.get("tmux_spawn_retried", False)),
                tmux_stale_session_cleanup_attempted=bool(
                    execution_diagnostics.get("tmux_stale_session_cleanup_attempted", False)
                ),
                tmux_stale_session_cleanup_result=str(
                    execution_diagnostics.get("tmux_stale_session_cleanup_result", "")
                ),
                tmux_stale_session_cleanup_retry_attempted=bool(
                    execution_diagnostics.get("tmux_stale_session_cleanup_retry_attempted", False)
                ),
                tmux_stale_session_cleanup_retry_result=str(
                    execution_diagnostics.get("tmux_stale_session_cleanup_retry_result", "")
                ),
                tmux_cleanup_retry_attempted=bool(execution_diagnostics.get("tmux_cleanup_retry_attempted", False)),
                tmux_cleanup_retry_result=str(execution_diagnostics.get("tmux_cleanup_retry_result", "")),
                tmux_cleanup_result=str(execution_diagnostics.get("tmux_cleanup_result", "")),
            )
            if lock_paths:
                lead_context.file_locks.release(profile.name, lock_paths)
            return True

        worker_payload = execution.get("payload", {})
        execution_diagnostics = execution.get("diagnostics", {})
        result = worker_payload.get("result", {})
        state_updates = worker_payload.get("state_updates", {})
        if isinstance(state_updates, dict):
            for key, value in state_updates.items():
                lead_context.shared_state.set(str(key), value)
        if not isinstance(result, dict):
            result = {"raw_result": result}
        transport = str(execution.get("transport", ""))
        retained_for_reuse = bool(execution_diagnostics.get("tmux_session_retained_for_reuse", False))
        lease_status = "retained" if retained_for_reuse else "released"
        if "subprocess" in transport:
            lease_status = "fallback_subprocess"
        _update_tmux_session_lease(
            lead_context=lead_context,
            worker_name=profile.name,
            session_name=str(
                execution_diagnostics.get("tmux_preferred_session_name", "")
                or preferred_tmux_session_name(profile.name)
            ),
            status=lease_status,
            task_id=task.task_id,
            task_type=task.task_type,
            transport=transport,
            cleanup_result=str(execution_diagnostics.get("tmux_cleanup_result", "")),
            retained_for_reuse=retained_for_reuse,
            reused_existing=bool(execution_diagnostics.get("tmux_preferred_session_reused_existing", False)),
            reuse_authorized=allow_existing_session_reuse,
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
            "tmux_worker_task_completed",
            worker=profile.name,
            task_id=task.task_id,
            transport=execution.get("transport"),
            fallback_used=bool(execution_diagnostics.get("fallback_used", False)),
            fallback_reason=str(execution_diagnostics.get("fallback_reason", "")),
            execution_timed_out=bool(execution_diagnostics.get("execution_timed_out", False)),
            timeout_phase=str(execution_diagnostics.get("timeout_phase", "")),
            tmux_timed_out=bool(execution_diagnostics.get("tmux_timed_out", False)),
            tmux_session_name_strategy=str(execution_diagnostics.get("tmux_session_name_strategy", "")),
            tmux_preferred_session_found_preflight=bool(
                execution_diagnostics.get("tmux_preferred_session_found_preflight", False)
            ),
            tmux_preferred_session_retried=bool(
                execution_diagnostics.get("tmux_preferred_session_retried", False)
            ),
            tmux_preferred_session_reused=bool(
                execution_diagnostics.get("tmux_preferred_session_reused", False)
            ),
            tmux_preferred_session_reuse_attempted=bool(
                execution_diagnostics.get("tmux_preferred_session_reuse_attempted", False)
            ),
            tmux_preferred_session_reuse_result=str(
                execution_diagnostics.get("tmux_preferred_session_reuse_result", "")
            ),
            tmux_preferred_session_reuse_authorized=bool(
                execution_diagnostics.get("tmux_preferred_session_reuse_authorized", False)
            ),
            tmux_preferred_session_reused_existing=bool(
                execution_diagnostics.get("tmux_preferred_session_reused_existing", False)
            ),
            tmux_session_retained_for_reuse=bool(
                execution_diagnostics.get("tmux_session_retained_for_reuse", False)
            ),
            tmux_orphan_sessions_found=int(execution_diagnostics.get("tmux_orphan_sessions_found", 0) or 0),
            tmux_orphan_sessions_cleaned=int(execution_diagnostics.get("tmux_orphan_sessions_cleaned", 0) or 0),
            tmux_spawn_attempts=int(execution_diagnostics.get("tmux_spawn_attempts", 0) or 0),
            tmux_spawn_retried=bool(execution_diagnostics.get("tmux_spawn_retried", False)),
            tmux_stale_session_cleanup_attempted=bool(
                execution_diagnostics.get("tmux_stale_session_cleanup_attempted", False)
            ),
            tmux_stale_session_cleanup_result=str(
                execution_diagnostics.get("tmux_stale_session_cleanup_result", "")
            ),
            tmux_stale_session_cleanup_retry_attempted=bool(
                execution_diagnostics.get("tmux_stale_session_cleanup_retry_attempted", False)
            ),
            tmux_stale_session_cleanup_retry_result=str(
                execution_diagnostics.get("tmux_stale_session_cleanup_retry_result", "")
            ),
            tmux_cleanup_retry_attempted=bool(execution_diagnostics.get("tmux_cleanup_retry_attempted", False)),
            tmux_cleanup_retry_result=str(execution_diagnostics.get("tmux_cleanup_retry_result", "")),
            tmux_cleanup_result=str(execution_diagnostics.get("tmux_cleanup_result", "")),
        )
        if lock_paths:
            lead_context.file_locks.release(profile.name, lock_paths)
        return True
    return False


def recover_tmux_worker_sessions(
    lead_context: Any,
    worker_profiles: Sequence[AgentProfile],
    resume_from: Optional[pathlib.Path] = None,
) -> Dict[str, Any]:
    leases = _load_tmux_session_leases(lead_context.shared_state)
    if not leases:
        summary = {
            "workers": [],
            "recovered": [],
            "missing": [],
            "inactive": [],
            "failed": [],
            "skipped": "no_leases",
            "resume_from": str(resume_from) if resume_from else "",
        }
        lead_context.logger.log(
            "tmux_worker_session_recovery_sweep",
            workers=[],
            recovered=[],
            missing=[],
            inactive=[],
            failed=[],
            skipped="no_leases",
            resume_from=str(resume_from) if resume_from else "",
        )
        lead_context.shared_state.set("tmux_session_recovery_summary", summary)
        return summary
    worker_names = [profile.name for profile in worker_profiles]
    recovered: List[str] = []
    missing: List[str] = []
    inactive: List[str] = []
    failed: List[str] = []
    if shutil.which("tmux") is None:
        for worker_name in worker_names:
            lease = leases.get(worker_name, {})
            if not isinstance(lease, dict) or not lease:
                continue
            _update_tmux_session_lease(
                lead_context=lead_context,
                worker_name=worker_name,
                session_name=str(lease.get("session_name", "") or preferred_tmux_session_name(worker_name)),
                status="recovery_tmux_unavailable",
                transport="tmux_resume_recovery",
                reuse_authorized=False,
                recovery_result="tmux_unavailable",
            )
            failed.append(worker_name)
        summary = {
            "workers": worker_names,
            "recovered": recovered,
            "missing": missing,
            "inactive": inactive,
            "failed": failed,
            "skipped": "tmux_unavailable",
            "resume_from": str(resume_from) if resume_from else "",
        }
        lead_context.logger.log(
            "tmux_worker_session_recovery_sweep",
            workers=worker_names,
            recovered=recovered,
            missing=missing,
            inactive=inactive,
            failed=failed,
            skipped="tmux_unavailable",
            resume_from=str(resume_from) if resume_from else "",
        )
        lead_context.shared_state.set("tmux_session_recovery_summary", summary)
        return summary

    for worker_name in worker_names:
        lease = leases.get(worker_name, {})
        if not isinstance(lease, dict) or not lease:
            continue
        session_name = str(lease.get("session_name", "") or preferred_tmux_session_name(worker_name))
        lease_status = str(lease.get("status", ""))
        if lease_status != "retained":
            _update_tmux_session_lease(
                lead_context=lead_context,
                worker_name=worker_name,
                session_name=session_name,
                status="recovery_inactive",
                transport="tmux_resume_recovery",
                reuse_authorized=False,
                recovery_result="inactive",
            )
            inactive.append(worker_name)
            continue
        existence = _tmux_session_exists(session_name=session_name)
        if existence.get("exists"):
            _update_tmux_session_lease(
                lead_context=lead_context,
                worker_name=worker_name,
                session_name=session_name,
                status="recovered_available",
                transport="tmux_resume_recovery",
                retained_for_reuse=True,
                reuse_authorized=True,
                recovery_result="available",
            )
            recovered.append(worker_name)
        else:
            error = str(existence.get("error", ""))
            status = "recovered_missing" if not error else "recovery_failed"
            _update_tmux_session_lease(
                lead_context=lead_context,
                worker_name=worker_name,
                session_name=session_name,
                status=status,
                transport="tmux_resume_recovery",
                retained_for_reuse=False,
                reuse_authorized=False,
                error=error,
                recovery_result="missing" if status == "recovered_missing" else "failed",
            )
            if status == "recovered_missing":
                missing.append(worker_name)
            else:
                failed.append(worker_name)

    summary = {
        "workers": worker_names,
        "recovered": recovered,
        "missing": missing,
        "inactive": inactive,
        "failed": failed,
        "skipped": "",
        "resume_from": str(resume_from) if resume_from else "",
    }
    lead_context.logger.log(
        "tmux_worker_session_recovery_sweep",
        workers=worker_names,
        recovered=recovered,
        missing=missing,
        inactive=inactive,
        failed=failed,
        skipped="",
        resume_from=str(resume_from) if resume_from else "",
    )
    lead_context.shared_state.set("tmux_session_recovery_summary", summary)
    return summary


def recover_tmux_analyst_sessions(
    lead_context: Any,
    analyst_profiles: Sequence[AgentProfile],
    resume_from: Optional[pathlib.Path] = None,
) -> Dict[str, Any]:
    return recover_tmux_worker_sessions(
        lead_context=lead_context,
        worker_profiles=analyst_profiles,
        resume_from=resume_from,
    )


def cleanup_tmux_worker_sessions(lead_context: Any, worker_profiles: Sequence[AgentProfile]) -> Dict[str, Any]:
    session_names = [preferred_tmux_session_name(profile.name) for profile in worker_profiles]
    defer_cleanup = bool(lead_context.shared_state.get("tmux_cleanup_deferred_for_resume", False))
    deferred_reason = str(lead_context.shared_state.get("tmux_cleanup_deferred_reason", "") or "")
    if defer_cleanup:
        summary = {
            "sessions": session_names,
            "cleaned": 0,
            "already_exited": 0,
            "failed": [],
            "skipped": "deferred_for_resume",
            "deferred_reason": deferred_reason,
        }
        lead_context.logger.log(
            "tmux_worker_session_cleanup_sweep",
            sessions=session_names,
            cleaned=0,
            already_exited=0,
            failed=[],
            skipped="deferred_for_resume",
            deferred_reason=deferred_reason,
        )
        lead_context.shared_state.set("tmux_session_cleanup_summary", summary)
        return summary
    if shutil.which("tmux") is None:
        summary = {
            "sessions": session_names,
            "cleaned": 0,
            "already_exited": 0,
            "failed": [],
            "skipped": "tmux_unavailable",
            "deferred_reason": "",
        }
        lead_context.logger.log(
            "tmux_worker_session_cleanup_sweep",
            sessions=session_names,
            cleaned=0,
            already_exited=0,
            failed=[],
            skipped="tmux_unavailable",
            deferred_reason="",
        )
        for profile in worker_profiles:
            _update_tmux_session_lease(
                lead_context=lead_context,
                worker_name=profile.name,
                session_name=preferred_tmux_session_name(profile.name),
                status="cleanup_skipped_tmux_unavailable",
                transport="tmux_cleanup_sweep",
                cleanup_result="tmux_unavailable",
            )
        lead_context.shared_state.set("tmux_session_cleanup_summary", summary)
        return summary
    cleaned = 0
    already_exited = 0
    failed: List[str] = []
    cleanup_results: Dict[str, str] = {}
    for session_name in session_names:
        cleanup = _cleanup_tmux_session_with_recovery(session_name)
        result = str(cleanup.get("result", ""))
        cleanup_results[session_name] = result
        if result in {"killed", "recovered_after_retry"}:
            cleaned += 1
        elif result == "already_exited":
            already_exited += 1
        else:
            failed.append(session_name)
    for profile in worker_profiles:
        session_name = preferred_tmux_session_name(profile.name)
        cleanup_status = "cleanup_failed" if session_name in failed else "cleanup_swept"
        _update_tmux_session_lease(
            lead_context=lead_context,
            worker_name=profile.name,
            session_name=session_name,
            status=cleanup_status,
            transport="tmux_cleanup_sweep",
            cleanup_result=str(cleanup_results.get(session_name, "")),
        )
    summary = {
        "sessions": session_names,
        "cleaned": cleaned,
        "already_exited": already_exited,
        "failed": failed,
        "skipped": "",
        "deferred_reason": "",
    }
    lead_context.logger.log(
        "tmux_worker_session_cleanup_sweep",
        sessions=session_names,
        cleaned=cleaned,
        already_exited=already_exited,
        failed=failed,
        skipped="",
        deferred_reason="",
    )
    lead_context.shared_state.set("tmux_session_cleanup_summary", summary)
    return summary


def cleanup_tmux_analyst_sessions(lead_context: Any, analyst_profiles: Sequence[AgentProfile]) -> Dict[str, Any]:
    return cleanup_tmux_worker_sessions(
        lead_context=lead_context,
        worker_profiles=analyst_profiles,
    )
