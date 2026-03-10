from __future__ import annotations

import json
import pathlib
import traceback
from typing import Any, Dict, Mapping, Sequence

from ..core import AgentProfile
from ..runtime.task_context import ScopedSharedState, build_task_context_snapshot
from .inprocess import SESSION_TASK_ASSIGNMENT_SUBJECT
from . import tmux as tmux_transport


MAILBOX_REVIEWER_TASK_TYPES = set(tmux_transport.MAILBOX_REVIEWER_TASK_TYPES)
HOST_WORKER_THREADS_ATTR = "_host_worker_threads"


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
    workspace_isolation_active = bool(host_enforcement.get("host_native_workspace_active", False))
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
        workspace_root=str(transport_identity.get("workspace_root", "") or ""),
        workspace_workdir=str(transport_identity.get("workspace_workdir", "") or ""),
        workspace_home_dir=str(transport_identity.get("workspace_home_dir", "") or ""),
        workspace_target_dir=str(transport_identity.get("workspace_target_dir", "") or ""),
        workspace_tmp_dir=str(transport_identity.get("workspace_tmp_dir", "") or ""),
        workspace_scope=str(transport_identity.get("workspace_scope", "") or ""),
        workspace_isolation_active=bool(transport_identity.get("workspace_isolation_active", False)),
    )


def run_host_teammate_task_once(
    lead_context: Any,
    teammate_profiles: Sequence[AgentProfile],
    handlers: Mapping[str, Any],
) -> bool:
    if not teammate_profiles:
        return False
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
        transport_identity = _host_transport_identity(lead_context=lead_context, profile=profile)
        handler = handlers.get(task.task_type)

        if handler is None:
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

        if task.task_type in MAILBOX_REVIEWER_TASK_TYPES and session_worker is not None:
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
            assignment_payload = {
                "contract": "session_task_assignment",
                "contract_version": 1,
                "transport": "host",
                "execution_mode": "session_thread",
                "task": task.to_dict(),
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
            )
            return True

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
            )
        finally:
            worker_context.shared_state = original_shared_state
            worker_context.task_context = {}
            if lock_paths:
                lead_context.file_locks.release(profile.name, lock_paths)
        return True
    return False
