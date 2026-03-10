from __future__ import annotations

import dataclasses
import datetime as dt
import json
import pathlib
import shutil
import threading
import uuid
from typing import Any, Callable, Dict, List, Optional, Sequence, Set


TERMINAL_TASK_STATES = {"completed", "failed"}
ACTIVE_TASK_STATES = {"pending", "blocked", "in_progress"}
HOOK_EVENT_TEAMMATE_IDLE = "TeammateIdle"
HOOK_EVENT_TASK_COMPLETED = "TaskCompleted"
TEAMMATE_IDLE_HOOK_INTERVAL_SEC = 1.0


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


@dataclasses.dataclass
class Task:
    task_id: str
    title: str
    task_type: str
    required_skills: Set[str]
    dependencies: List[str]
    payload: Dict[str, Any]
    locked_paths: List[str]
    allowed_agent_types: Set[str] = dataclasses.field(default_factory=set)
    status: str = "pending"
    owner: Optional[str] = None
    result: Optional[Dict[str, Any]] = None
    error: Optional[str] = None
    created_at: str = dataclasses.field(default_factory=utc_now)
    updated_at: str = dataclasses.field(default_factory=utc_now)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "task_id": self.task_id,
            "title": self.title,
            "task_type": self.task_type,
            "required_skills": sorted(self.required_skills),
            "dependencies": list(self.dependencies),
            "payload": self.payload,
            "locked_paths": list(self.locked_paths),
            "allowed_agent_types": sorted(self.allowed_agent_types),
            "status": self.status,
            "owner": self.owner,
            "result": self.result,
            "error": self.error,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


def task_from_dict(payload: Dict[str, Any]) -> Task:
    required_skills = {str(skill) for skill in payload.get("required_skills", [])}
    dependencies = [str(dep) for dep in payload.get("dependencies", [])]
    locked_paths = [str(path) for path in payload.get("locked_paths", [])]
    allowed_agent_types = {str(name) for name in payload.get("allowed_agent_types", [])}
    status = str(payload.get("status", "pending"))
    owner = payload.get("owner")
    if status in {"pending", "blocked", "failed"}:
        owner = None
    if status == "in_progress":
        status = "pending"
        owner = None
    return Task(
        task_id=str(payload.get("task_id", "")),
        title=str(payload.get("title", "")),
        task_type=str(payload.get("task_type", "")),
        required_skills=required_skills,
        dependencies=dependencies,
        payload=dict(payload.get("payload", {})),
        locked_paths=locked_paths,
        allowed_agent_types=allowed_agent_types,
        status=status,
        owner=owner,
        result=payload.get("result"),
        error=payload.get("error"),
        created_at=str(payload.get("created_at", utc_now())),
        updated_at=str(payload.get("updated_at", utc_now())),
    )


@dataclasses.dataclass
class Message:
    message_id: str
    sent_at: str
    sender: str
    recipient: str
    subject: str
    body: str
    task_id: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return dataclasses.asdict(self)


@dataclasses.dataclass
class AgentProfile:
    name: str
    skills: Set[str]
    agent_type: str = "general"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "skills": sorted(self.skills),
            "agent_type": self.agent_type,
        }


class EventLogger:
    def __init__(self, output_dir: pathlib.Path, truncate: bool = True) -> None:
        self._output_dir = output_dir
        self._output_dir.mkdir(parents=True, exist_ok=True)
        self._path = self._output_dir / "events.jsonl"
        self._lock = threading.Lock()
        if truncate or not self._path.exists():
            self._path.write_text("", encoding="utf-8")
            self._next_index = 0
        else:
            self._next_index = self._recover_next_index()

    @property
    def path(self) -> pathlib.Path:
        return self._path

    def _recover_next_index(self) -> int:
        next_index = 0
        try:
            with self._path.open("r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    next_index += 1
                    try:
                        payload = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(payload, dict):
                        continue
                    raw_idx = payload.get("event_index")
                    if isinstance(raw_idx, int):
                        next_index = max(next_index, raw_idx + 1)
        except FileNotFoundError:
            return 0
        return next_index

    def event_count(self) -> int:
        with self._lock:
            return int(self._next_index)

    def log(self, event: str, **fields: Any) -> None:
        with self._lock:
            event_index = int(self._next_index)
            self._next_index += 1
            payload = {
                "ts": utc_now(),
                "event": event,
                "event_index": event_index,
                **fields,
            }
            with self._path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(payload, ensure_ascii=False) + "\n")


class SharedState:
    def __init__(self) -> None:
        self._data: Dict[str, Any] = {}
        self._lock = threading.Lock()

    def set(self, key: str, value: Any) -> None:
        with self._lock:
            self._data[key] = value

    def get(self, key: str, default: Any = None) -> Any:
        with self._lock:
            return self._data.get(key, default)

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return json.loads(json.dumps(self._data, ensure_ascii=False))


class Mailbox:
    def __init__(
        self,
        participants: Sequence[str],
        logger: EventLogger,
        storage_dir: Optional[pathlib.Path] = None,
        clear_storage: bool = False,
    ) -> None:
        normalized_participants = [str(name) for name in participants if str(name)]
        self._participants: Set[str] = set(normalized_participants)
        self._queues: Dict[str, List[Message]] = {name: [] for name in normalized_participants}
        self._lock = threading.Lock()
        self._logger = logger
        self._storage_dir = pathlib.Path(storage_dir).resolve() if storage_dir else None
        if self._storage_dir is not None:
            if clear_storage and self._storage_dir.exists():
                shutil.rmtree(self._storage_dir)
            self._storage_dir.mkdir(parents=True, exist_ok=True)
            for participant in sorted(self._participants):
                self._recipient_dir(participant).mkdir(parents=True, exist_ok=True)

    @property
    def storage_dir(self) -> Optional[pathlib.Path]:
        return self._storage_dir

    def model_name(self) -> str:
        if self._storage_dir is not None:
            return "asynchronous file-backed inbox"
        return "asynchronous pull-based inbox"

    def _recipient_dir(self, recipient: str) -> pathlib.Path:
        if self._storage_dir is None:
            raise RuntimeError("recipient_dir requested for in-memory mailbox")
        return self._storage_dir / str(recipient)

    def _message_file_path(self, recipient: str, message: Message) -> pathlib.Path:
        if self._storage_dir is None:
            raise RuntimeError("message_file_path requested for in-memory mailbox")
        stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        return self._recipient_dir(recipient) / f"{stamp}_{message.message_id}.json"

    def _claim_dir(self, recipient: str) -> pathlib.Path:
        if self._storage_dir is None:
            raise RuntimeError("claim_dir requested for in-memory mailbox")
        return self._recipient_dir(recipient) / "_claims"

    def _deserialize_message(self, payload: Dict[str, Any]) -> Message:
        return Message(
            message_id=str(payload.get("message_id", "") or ""),
            sent_at=str(payload.get("sent_at", "") or utc_now()),
            sender=str(payload.get("sender", "") or ""),
            recipient=str(payload.get("recipient", "") or ""),
            subject=str(payload.get("subject", "") or ""),
            body=str(payload.get("body", "") or ""),
            task_id=str(payload.get("task_id", "")) or None,
        )

    def _store_message_file(self, message: Message) -> None:
        recipient_dir = self._recipient_dir(message.recipient)
        recipient_dir.mkdir(parents=True, exist_ok=True)
        target = self._message_file_path(message.recipient, message)
        temp_path = target.with_suffix(".tmp")
        temp_path.write_text(json.dumps(message.to_dict(), ensure_ascii=False), encoding="utf-8")
        temp_path.replace(target)

    def transport_view(self) -> "Mailbox":
        if self._storage_dir is None:
            return self
        return Mailbox(
            participants=sorted(self._participants),
            logger=self._logger,
            storage_dir=self._storage_dir,
            clear_storage=False,
        )

    def _pull_file_messages(
        self,
        recipient: str,
        matcher: Optional[Callable[[Message], bool]] = None,
    ) -> List[Message]:
        recipient_dir = self._recipient_dir(recipient)
        recipient_dir.mkdir(parents=True, exist_ok=True)
        claim_dir = self._claim_dir(recipient)
        claim_dir.mkdir(parents=True, exist_ok=True)
        matched: List[Message] = []
        with self._lock:
            for message_path in sorted(recipient_dir.glob("*.json")):
                claimed_path = claim_dir / f"{message_path.stem}.{uuid.uuid4().hex}.json"
                try:
                    message_path.replace(claimed_path)
                except FileNotFoundError:
                    continue
                except OSError:
                    continue
                try:
                    payload = json.loads(claimed_path.read_text(encoding="utf-8"))
                except FileNotFoundError:
                    continue
                except (OSError, json.JSONDecodeError):
                    try:
                        claimed_path.unlink()
                    except OSError:
                        pass
                    continue
                if not isinstance(payload, dict):
                    try:
                        claimed_path.unlink()
                    except OSError:
                        pass
                    continue
                message = self._deserialize_message(payload)
                if matcher is not None and not matcher(message):
                    try:
                        claimed_path.replace(message_path)
                    except OSError:
                        pass
                    continue
                matched.append(message)
                try:
                    claimed_path.unlink()
                except FileNotFoundError:
                    continue
        return matched

    def send(
        self,
        sender: str,
        recipient: str,
        subject: str,
        body: str,
        task_id: Optional[str] = None,
    ) -> None:
        message = Message(
            message_id=str(uuid.uuid4()),
            sent_at=utc_now(),
            sender=sender,
            recipient=recipient,
            subject=subject,
            body=body,
            task_id=task_id,
        )
        with self._lock:
            self._participants.add(str(recipient))
            if self._storage_dir is None:
                self._queues.setdefault(recipient, []).append(message)
        if self._storage_dir is not None:
            self._store_message_file(message)
        self._logger.log("mail_sent", **message.to_dict())

    def broadcast(self, sender: str, subject: str, body: str) -> None:
        with self._lock:
            recipients = [name for name in self._participants if name != sender]
        for recipient in recipients:
            self.send(sender=sender, recipient=recipient, subject=subject, body=body)

    def pull(self, recipient: str) -> List[Message]:
        if self._storage_dir is not None:
            pending = self._pull_file_messages(recipient=recipient)
        else:
            with self._lock:
                pending = self._queues.get(recipient, [])
                self._queues[recipient] = []
        if pending:
            self._logger.log(
                "mail_pulled",
                recipient=recipient,
                count=len(pending),
                message_ids=[message.message_id for message in pending],
            )
        return pending

    def pull_matching(
        self,
        recipient: str,
        matcher: Callable[[Message], bool],
    ) -> List[Message]:
        if self._storage_dir is not None:
            matched = self._pull_file_messages(recipient=recipient, matcher=matcher)
        else:
            with self._lock:
                queue = self._queues.get(recipient, [])
                matched = []
                rest: List[Message] = []
                for message in queue:
                    if matcher(message):
                        matched.append(message)
                    else:
                        rest.append(message)
                self._queues[recipient] = rest
        if matched:
            self._logger.log(
                "mail_pulled_matching",
                recipient=recipient,
                count=len(matched),
                message_ids=[message.message_id for message in matched],
            )
        return matched


class FileLockRegistry:
    def __init__(self, logger: EventLogger) -> None:
        self._owners: Dict[str, str] = {}
        self._lock = threading.Lock()
        self._logger = logger

    def acquire(self, agent_name: str, paths: Sequence[str]) -> bool:
        normalized = [str(pathlib.Path(path).resolve()) for path in paths]
        with self._lock:
            for path in normalized:
                owner = self._owners.get(path)
                if owner is not None and owner != agent_name:
                    return False
            for path in normalized:
                self._owners[path] = agent_name
        self._logger.log("file_lock_acquired", agent=agent_name, paths=normalized)
        return True

    def release(self, agent_name: str, paths: Optional[Sequence[str]] = None) -> None:
        with self._lock:
            if paths is None:
                release_paths = [path for path, owner in self._owners.items() if owner == agent_name]
            else:
                normalized = [str(pathlib.Path(path).resolve()) for path in paths]
                release_paths = [path for path in normalized if self._owners.get(path) == agent_name]
            for path in release_paths:
                self._owners.pop(path, None)
        if release_paths:
            self._logger.log("file_lock_released", agent=agent_name, paths=release_paths)

    def snapshot(self) -> Dict[str, str]:
        with self._lock:
            return dict(self._owners)


class TaskBoard:
    def __init__(self, tasks: Sequence[Task], logger: EventLogger) -> None:
        self._tasks = {task.task_id: task for task in tasks}
        self._ordered_ids = [task.task_id for task in tasks]
        self._lock = threading.Lock()
        self._logger = logger
        self._refresh_blocked_states_locked()

    def _deps_satisfied_locked(self, task: Task) -> bool:
        for dep_id in task.dependencies:
            dep = self._tasks.get(dep_id)
            if dep is None or dep.status != "completed":
                return False
        return True

    def _refresh_blocked_states_locked(self) -> None:
        for task in self._tasks.values():
            if task.status in TERMINAL_TASK_STATES or task.status == "in_progress":
                continue
            if self._deps_satisfied_locked(task):
                if task.status == "blocked":
                    task.status = "pending"
                    task.updated_at = utc_now()
            else:
                if task.status == "pending":
                    task.status = "blocked"
                    task.updated_at = utc_now()

    def _agent_type_allowed_locked(self, task: Task, agent_type: str) -> bool:
        if not task.allowed_agent_types:
            return True
        return agent_type in task.allowed_agent_types

    def _fail_dependents_locked(self, failed_task_id: str, seen: Optional[Set[str]] = None) -> List[Dict[str, str]]:
        visited = seen or set()
        if failed_task_id in visited:
            return []
        visited.add(failed_task_id)
        failed_dependents: List[Dict[str, str]] = []
        for task in self._tasks.values():
            if failed_task_id not in task.dependencies:
                continue
            if task.status in TERMINAL_TASK_STATES or task.status == "in_progress":
                continue
            task.status = "failed"
            task.owner = None
            task.error = f"blocked by failed dependency: {failed_task_id}"
            task.updated_at = utc_now()
            failed_dependents.append({"task_id": task.task_id, "dependency_id": failed_task_id})
            failed_dependents.extend(self._fail_dependents_locked(task.task_id, seen=visited))
        return failed_dependents

    def claim_next(self, agent_name: str, agent_skills: Set[str], agent_type: str) -> Optional[Task]:
        with self._lock:
            self._refresh_blocked_states_locked()
            for task_id in self._ordered_ids:
                task = self._tasks[task_id]
                if task.status != "pending":
                    continue
                if task.required_skills and not task.required_skills.issubset(agent_skills):
                    continue
                if not self._agent_type_allowed_locked(task=task, agent_type=agent_type):
                    continue
                task.status = "in_progress"
                task.owner = agent_name
                task.updated_at = utc_now()
                self._logger.log(
                    "task_claimed",
                    task_id=task.task_id,
                    title=task.title,
                    agent=agent_name,
                    required_skills=sorted(task.required_skills),
                    allowed_agent_types=sorted(task.allowed_agent_types),
                )
                return dataclasses.replace(task)
        return None

    def claim_specific(
        self,
        task_id: str,
        agent_name: str,
        agent_skills: Set[str],
        agent_type: str,
    ) -> Optional[Task]:
        with self._lock:
            self._refresh_blocked_states_locked()
            task = self._tasks.get(task_id)
            if task is None or task.status != "pending":
                return None
            if task.required_skills and not task.required_skills.issubset(agent_skills):
                return None
            if not self._agent_type_allowed_locked(task=task, agent_type=agent_type):
                return None
            task.status = "in_progress"
            task.owner = agent_name
            task.updated_at = utc_now()
            self._logger.log(
                "task_claimed",
                task_id=task.task_id,
                title=task.title,
                agent=agent_name,
                required_skills=sorted(task.required_skills),
                allowed_agent_types=sorted(task.allowed_agent_types),
            )
            return dataclasses.replace(task)

    def defer(self, task_id: str, owner: str, reason: str) -> None:
        with self._lock:
            task = self._tasks[task_id]
            if task.owner != owner or task.status != "in_progress":
                return
            task.status = "pending"
            task.owner = None
            task.updated_at = utc_now()
            self._logger.log("task_deferred", task_id=task_id, owner=owner, reason=reason)

    def complete(self, task_id: str, owner: str, result: Dict[str, Any]) -> None:
        with self._lock:
            task = self._tasks[task_id]
            if task.owner != owner or task.status != "in_progress":
                raise RuntimeError(f"invalid completion state for task={task_id} owner={owner}")
            task.status = "completed"
            task.result = result
            task.error = None
            task.updated_at = utc_now()
            self._refresh_blocked_states_locked()
        self._logger.log("task_completed", task_id=task_id, owner=owner)
        self._logger.log(HOOK_EVENT_TASK_COMPLETED, task_id=task_id, owner=owner)

    def fail(self, task_id: str, owner: str, error: str) -> None:
        with self._lock:
            task = self._tasks[task_id]
            if task.owner != owner or task.status != "in_progress":
                raise RuntimeError(f"invalid failure state for task={task_id} owner={owner}")
            task.status = "failed"
            task.error = error
            task.updated_at = utc_now()
            failed_dependents = self._fail_dependents_locked(task_id)
            self._refresh_blocked_states_locked()
        self._logger.log("task_failed", task_id=task_id, owner=owner, error=error)
        for item in failed_dependents:
            self._logger.log(
                "task_failed_due_to_dependency",
                task_id=item["task_id"],
                dependency_id=item["dependency_id"],
                error=f"blocked by failed dependency: {item['dependency_id']}",
            )

    def all_terminal(self) -> bool:
        with self._lock:
            return all(task.status in TERMINAL_TASK_STATES for task in self._tasks.values())

    def has_active_tasks(self) -> bool:
        with self._lock:
            return any(task.status in ACTIVE_TASK_STATES for task in self._tasks.values())

    def get_task_result(self, task_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            task = self._tasks.get(task_id)
            return None if task is None else task.result

    def has_task(self, task_id: str) -> bool:
        with self._lock:
            return task_id in self._tasks

    def add_tasks(self, tasks: Sequence[Task], inserted_by: str) -> List[str]:
        inserted_ids: List[str] = []
        with self._lock:
            for task in tasks:
                if task.task_id in self._tasks:
                    continue
                self._tasks[task.task_id] = task
                self._ordered_ids.append(task.task_id)
                inserted_ids.append(task.task_id)
                self._logger.log(
                    "task_inserted",
                    task_id=task.task_id,
                    title=task.title,
                    inserted_by=inserted_by,
                    required_skills=sorted(task.required_skills),
                    allowed_agent_types=sorted(task.allowed_agent_types),
                    dependencies=list(task.dependencies),
                )
            self._refresh_blocked_states_locked()
        return inserted_ids

    def add_dependency(self, task_id: str, dependency_id: str, updated_by: str) -> bool:
        with self._lock:
            task = self._tasks.get(task_id)
            dependency = self._tasks.get(dependency_id)
            if task is None or dependency is None:
                return False
            if dependency_id in task.dependencies:
                return False
            if task.status in TERMINAL_TASK_STATES or task.status == "in_progress":
                return False
            task.dependencies.append(dependency_id)
            task.updated_at = utc_now()
            self._refresh_blocked_states_locked()
        self._logger.log(
            "task_dependency_added",
            task_id=task_id,
            dependency_id=dependency_id,
            updated_by=updated_by,
        )
        return True

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "generated_at": utc_now(),
                "tasks": [self._tasks[task_id].to_dict() for task_id in self._ordered_ids],
            }
