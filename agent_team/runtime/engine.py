from __future__ import annotations

import dataclasses
import json
import pathlib
import threading
import time
import traceback
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence

from ..config import (
    AgentTeamConfig,
    RuntimeConfig,
    TeamConfig,
    build_agent_team_config,
    default_team_config,
)
from ..core import (
    AgentProfile,
    EventLogger,
    FileLockRegistry,
    Mailbox,
    SharedState,
    Task,
    TaskBoard,
)
from ..host import build_host_adapter
from ..models import LLMProvider, build_provider
from ..transports.inprocess import InProcessTeammateAgent
from ..workflows import (
    resolve_workflow_pack,
)
from .persistence import (
    CHECKPOINT_FILENAME,
    events_file,
    load_checkpoint,
    restore_shared_state_from_checkpoint_payload,
    restore_tasks_from_checkpoint_payload,
    seed_branch_events_from_source,
    write_artifacts,
    write_checkpoint,
)
from .session_state import (
    TeammateSessionRegistry,
    teammate_transport_for_profile,
)


@dataclasses.dataclass
class AgentContext:
    profile: AgentProfile
    target_dir: pathlib.Path
    output_dir: pathlib.Path
    goal: str
    provider: LLMProvider
    runtime_config: RuntimeConfig
    board: TaskBoard
    mailbox: Mailbox
    file_locks: FileLockRegistry
    shared_state: SharedState
    logger: EventLogger
    task_context: Dict[str, Any] = dataclasses.field(default_factory=dict)
    session_state: Dict[str, Any] = dataclasses.field(default_factory=dict)
    session_registry: Optional[TeammateSessionRegistry] = None


TaskHandler = Callable[[AgentContext, Task], Dict[str, Any]]
TaskHandlers = Mapping[str, TaskHandler]
TeammateFactory = Callable[..., threading.Thread]
TmuxRunner = Callable[[AgentContext, Sequence[AgentProfile], pathlib.Path, int], bool]
TmuxCleanupRunner = Callable[[AgentContext, Sequence[AgentProfile]], Any]
TmuxRecoveryRunner = Callable[[AgentContext, Sequence[AgentProfile], Optional[pathlib.Path]], Any]


def get_team_profiles(context: AgentContext) -> List[Dict[str, Any]]:
    raw_profiles = context.shared_state.get("team_profiles", [])
    if not isinstance(raw_profiles, list):
        return []
    profiles: List[Dict[str, Any]] = []
    for item in raw_profiles:
        if isinstance(item, dict):
            profiles.append(item)
    return profiles


def get_team_member_names(
    context: AgentContext,
    agent_type: str = "",
    exclude: Optional[Sequence[str]] = None,
) -> List[str]:
    excluded = {name for name in (exclude or [])}
    names: List[str] = []
    for profile in get_team_profiles(context):
        name = str(profile.get("name", "") or "")
        if not name or name in excluded:
            continue
        if agent_type and str(profile.get("agent_type", "")) != agent_type:
            continue
        names.append(name)
    return names


def profile_has_skill(profile: AgentProfile, skill: str) -> bool:
    return skill in profile.skills


def get_lead_name(context: AgentContext) -> str:
    return str(context.shared_state.get("lead_name", "lead") or "lead")


def build_profiles(team_config: Optional[TeamConfig] = None) -> List[AgentProfile]:
    effective_team = team_config or default_team_config()
    return effective_team.to_profiles()


def _default_teammate_agent_factory(
    context: AgentContext,
    stop_event: threading.Event,
    handlers: TaskHandlers,
    claim_tasks: bool = True,
) -> threading.Thread:
    return InProcessTeammateAgent(
        context=context,
        stop_event=stop_event,
        claim_tasks=claim_tasks,
        handlers=handlers,
        get_lead_name_fn=get_lead_name,
        profile_has_skill_fn=profile_has_skill,
        traceback_module=traceback,
    )


def _missing_tmux_runner(
    lead_context: AgentContext,
    analyst_profiles: Sequence[AgentProfile],
    runtime_script: pathlib.Path,
    worker_timeout_sec: int,
) -> bool:
    del lead_context, analyst_profiles, runtime_script, worker_timeout_sec
    raise RuntimeError("tmux teammate mode requires a tmux runner function")


def run_lead_task_once(
    lead_context: AgentContext,
    task_id: str,
    handlers: TaskHandlers,
    traceback_module: Any = traceback,
) -> bool:
    task = lead_context.board.claim_specific(
        task_id=task_id,
        agent_name=lead_context.profile.name,
        agent_skills=lead_context.profile.skills,
        agent_type=lead_context.profile.agent_type,
    )
    if task is None:
        return False

    lead_context.logger.log(
        "task_started",
        task_id=task.task_id,
        agent=lead_context.profile.name,
        task_type=task.task_type,
    )
    lead_context.mailbox.send(
        sender=lead_context.profile.name,
        recipient=lead_context.profile.name,
        subject="task_started",
        body=f"lead started {task.task_id}",
        task_id=task.task_id,
    )
    handler = handlers.get(task.task_type)
    if handler is None:
        error = f"no handler registered for task_type={task.task_type}"
        lead_context.board.fail(task_id=task.task_id, owner=lead_context.profile.name, error=error)
        lead_context.mailbox.send(
            sender=lead_context.profile.name,
            recipient=lead_context.profile.name,
            subject="task_failed",
            body=error,
            task_id=task.task_id,
        )
        return True
    try:
        result = handler(lead_context, task)
        lead_context.board.complete(task_id=task.task_id, owner=lead_context.profile.name, result=result)
        lead_context.mailbox.send(
            sender=lead_context.profile.name,
            recipient=lead_context.profile.name,
            subject="task_completed",
            body=f"{task.task_id} done",
            task_id=task.task_id,
        )
    except Exception as exc:  # pragma: no cover - defensive path
        error = f"{type(exc).__name__}: {exc}"
        lead_context.board.fail(task_id=task.task_id, owner=lead_context.profile.name, error=error)
        lead_context.mailbox.send(
            sender=lead_context.profile.name,
            recipient=lead_context.profile.name,
            subject="task_failed",
            body=error,
            task_id=task.task_id,
        )
        lead_context.logger.log(
            "task_exception",
            task_id=task.task_id,
            agent=lead_context.profile.name,
            traceback=traceback_module.format_exc(),
        )
    return True


def run_lead_tasks_once(
    lead_context: AgentContext,
    lead_task_order: Sequence[str],
    handlers: TaskHandlers,
    traceback_module: Any = traceback,
) -> bool:
    ran_any = False
    for task_id in lead_task_order:
        if run_lead_task_once(
            lead_context=lead_context,
            task_id=str(task_id),
            handlers=handlers,
            traceback_module=traceback_module,
        ):
            ran_any = True
    return ran_any


def run_team(
    goal: str,
    target_dir: pathlib.Path,
    output_dir: pathlib.Path,
    runtime_config: RuntimeConfig,
    provider_name: str,
    model: str,
    openai_api_key_env: str,
    openai_base_url: str,
    require_llm: bool,
    provider_timeout_sec: int,
    resume_from: Optional[pathlib.Path] = None,
    max_completed_tasks: int = 0,
    rewind_history_index: Optional[int] = None,
    rewind_event_index: Optional[int] = None,
    rewind_event_resolution: Optional[Dict[str, Any]] = None,
    rewind_source_output_dir: Optional[pathlib.Path] = None,
    rewind_source_checkpoint: Optional[pathlib.Path] = None,
    branch_run_id: str = "",
    agent_team_config: Optional[AgentTeamConfig] = None,
    teammate_agent_factory: Optional[TeammateFactory] = None,
    run_tmux_analyst_task_once_fn: Optional[TmuxRunner] = None,
    recover_tmux_analyst_sessions_fn: Optional[TmuxRecoveryRunner] = None,
    cleanup_tmux_analyst_sessions_fn: Optional[TmuxCleanupRunner] = None,
    runtime_script: Optional[pathlib.Path] = None,
) -> int:
    output_dir.mkdir(parents=True, exist_ok=True)
    checkpoint_path = output_dir / CHECKPOINT_FILENAME
    effective_agent_team_config = agent_team_config or build_agent_team_config(
        runtime_config=runtime_config,
        provider_name=provider_name,
        model=model,
        openai_api_key_env=openai_api_key_env,
        openai_base_url=openai_base_url,
        require_llm=require_llm,
        provider_timeout_sec=provider_timeout_sec,
    )
    workflow_pack_name = effective_agent_team_config.workflow.pack
    workflow_pack = resolve_workflow_pack(workflow_pack_name)
    workflow_handlers = workflow_pack.build_handlers()
    workflow_runtime_metadata = workflow_pack.runtime_metadata
    lead_task_order = [str(task_id) for task_id in workflow_runtime_metadata.lead_task_order]
    report_task_ids = [str(task_id) for task_id in workflow_runtime_metadata.report_task_ids]
    worker_factory = teammate_agent_factory or _default_teammate_agent_factory
    tmux_runner = run_tmux_analyst_task_once_fn or _missing_tmux_runner
    runtime_script_path = runtime_script or pathlib.Path(__file__).resolve()

    resume_payload: Dict[str, Any] = {}
    if resume_from is not None:
        resume_payload = load_checkpoint(resume_from)
    effective_rewind_history_index: Optional[int] = rewind_history_index
    if effective_rewind_history_index is None:
        rewind_from_payload = resume_payload.get("rewind_history_index", "")
        if rewind_from_payload != "":
            try:
                effective_rewind_history_index = int(rewind_from_payload)
            except (TypeError, ValueError):
                effective_rewind_history_index = None
    effective_rewind_event_index: Optional[int] = rewind_event_index
    if effective_rewind_event_index is None:
        rewind_event_from_payload = resume_payload.get("rewind_event_index", "")
        if rewind_event_from_payload != "":
            try:
                effective_rewind_event_index = int(rewind_event_from_payload)
            except (TypeError, ValueError):
                effective_rewind_event_index = None

    effective_rewind_event_resolution: Dict[str, Any] = {}
    if isinstance(rewind_event_resolution, dict) and rewind_event_resolution:
        effective_rewind_event_resolution = dict(rewind_event_resolution)
    else:
        resume_resolution = resume_payload.get("rewind_event_resolution", {})
        if isinstance(resume_resolution, dict):
            effective_rewind_event_resolution = dict(resume_resolution)

    effective_rewind_source_output_dir = rewind_source_output_dir
    if effective_rewind_source_output_dir is None:
        source_output_from_payload = str(resume_payload.get("rewind_source_output_dir", "") or "")
        if source_output_from_payload:
            effective_rewind_source_output_dir = pathlib.Path(source_output_from_payload)

    effective_rewind_source_checkpoint = rewind_source_checkpoint
    if effective_rewind_source_checkpoint is None:
        source_ckpt_from_payload = str(resume_payload.get("rewind_source_checkpoint", "") or "")
        if source_ckpt_from_payload:
            effective_rewind_source_checkpoint = pathlib.Path(source_ckpt_from_payload)

    effective_branch_run_id = branch_run_id or str(resume_payload.get("branch_run_id", "") or "")

    branch_event_seed: Dict[str, Any] = {}
    branch_seed_event_index: Optional[int] = None
    should_seed_branch_events = (
        resume_from is not None
        and effective_rewind_source_output_dir is not None
        and output_dir.resolve() != effective_rewind_source_output_dir.resolve()
        and not events_file(output_dir).exists()
    )
    if should_seed_branch_events:
        if effective_rewind_event_index is not None:
            branch_seed_event_index = effective_rewind_event_index
        else:
            raw_event_count = resume_payload.get("event_count", "")
            try:
                resolved_event_count = int(raw_event_count)
            except (TypeError, ValueError):
                resolved_event_count = -1
            if resolved_event_count > 0:
                branch_seed_event_index = resolved_event_count - 1
        if branch_seed_event_index is not None and branch_seed_event_index >= 0:
            branch_event_seed = seed_branch_events_from_source(
                source_output_dir=effective_rewind_source_output_dir,
                target_output_dir=output_dir,
                max_event_index=branch_seed_event_index,
            )

    truncate_events = resume_from is None
    if resume_from is not None and resume_from.resolve().parent != output_dir.resolve():
        truncate_events = True
    if branch_event_seed.get("seeded"):
        truncate_events = False
    logger = EventLogger(output_dir=output_dir, truncate=truncate_events)
    host_adapter = build_host_adapter(effective_agent_team_config.host)

    provider, provider_meta = build_provider(
        provider_name=effective_agent_team_config.model.provider_name,
        model=effective_agent_team_config.model.model,
        openai_api_key_env=effective_agent_team_config.model.openai_api_key_env,
        openai_base_url=effective_agent_team_config.model.openai_base_url,
        require_llm=effective_agent_team_config.model.require_llm,
        timeout_sec=effective_agent_team_config.model.timeout_sec,
    )
    if resume_from is not None:
        logger.log(
            "run_resume_loaded",
            checkpoint=str(resume_from),
            checkpoint_saved_at=resume_payload.get("saved_at", ""),
            checkpoint_version=resume_payload.get("version", 0),
        )
    if branch_event_seed.get("seeded"):
        logger.log(
            "run_branch_events_seeded",
            source_events_path=branch_event_seed.get("source_events_path", ""),
            target_events_path=branch_event_seed.get("target_events_path", ""),
            seeded_count=int(branch_event_seed.get("seeded_count", 0)),
            seed_event_index=branch_event_seed.get("seed_event_index", ""),
        )
    effective_rewind_seed_event_index: Optional[int] = None
    raw_seed_idx = branch_event_seed.get("seed_event_index", "")
    if raw_seed_idx != "":
        try:
            effective_rewind_seed_event_index = int(raw_seed_idx)
        except (TypeError, ValueError):
            effective_rewind_seed_event_index = None
    else:
        resume_seed_idx = resume_payload.get("rewind_seed_event_index", "")
        if resume_seed_idx != "":
            try:
                effective_rewind_seed_event_index = int(resume_seed_idx)
            except (TypeError, ValueError):
                effective_rewind_seed_event_index = None
    if branch_event_seed.get("seeded"):
        effective_rewind_seed_event_count = int(branch_event_seed.get("seeded_count", 0))
    else:
        raw_seed_count = resume_payload.get("rewind_seed_event_count", 0)
        try:
            effective_rewind_seed_event_count = max(0, int(raw_seed_count))
        except (TypeError, ValueError):
            effective_rewind_seed_event_count = 0
    logger.log(
        "run_started",
        goal=goal,
        target_dir=str(target_dir),
        output_dir=str(output_dir),
        provider=provider_meta.to_dict(),
        host=host_adapter.runtime_metadata(),
        workflow=effective_agent_team_config.workflow.to_dict(),
        workflow_runtime_metadata={
            "lead_task_order": lead_task_order,
            "report_task_ids": report_task_ids,
        },
        team=effective_agent_team_config.team.to_dict(),
        policies=effective_agent_team_config.policies.to_dict(),
        config_source=effective_agent_team_config.source_path,
        runtime_config=runtime_config.to_dict(),
        resume_from=str(resume_from) if resume_from else "",
        rewind_history_index=(
            effective_rewind_history_index if effective_rewind_history_index is not None else ""
        ),
        rewind_event_index=(
            effective_rewind_event_index if effective_rewind_event_index is not None else ""
        ),
        rewind_event_resolution=effective_rewind_event_resolution,
        rewind_source_output_dir=(
            str(effective_rewind_source_output_dir) if effective_rewind_source_output_dir else ""
        ),
        rewind_source_checkpoint=(
            str(effective_rewind_source_checkpoint) if effective_rewind_source_checkpoint else ""
        ),
        branch_run_id=effective_branch_run_id,
        rewind_seed_event_index=(
            effective_rewind_seed_event_index if effective_rewind_seed_event_index is not None else ""
        ),
        rewind_seed_event_count=effective_rewind_seed_event_count,
    )

    tasks = (
        restore_tasks_from_checkpoint_payload(resume_payload)
        if resume_payload
        else workflow_pack.build_tasks(
            output_dir=output_dir,
            runtime_config=runtime_config,
            workflow_options=effective_agent_team_config.workflow.options,
        )
    )
    board = TaskBoard(tasks=tasks, logger=logger)
    profiles = build_profiles(team_config=effective_agent_team_config.team)
    analyst_profiles = [profile for profile in profiles if profile.agent_type == "analyst"]
    lead_name = str(effective_agent_team_config.team.lead_name or "lead")
    participants = [lead_name] + [profile.name for profile in profiles]
    mailbox = Mailbox(participants=participants, logger=logger)
    shared_state = SharedState()
    if resume_payload:
        restore_shared_state_from_checkpoint_payload(shared_state=shared_state, checkpoint_payload=resume_payload)
    shared_state.set("lead_name", lead_name)
    shared_state.set("agent_team_config", effective_agent_team_config.to_dict())
    shared_state.set("host", host_adapter.runtime_metadata())
    shared_state.set("team", effective_agent_team_config.team.to_dict())
    shared_state.set("workflow", effective_agent_team_config.workflow.to_dict())
    shared_state.set("workflow_lead_task_order", lead_task_order)
    shared_state.set("workflow_report_task_ids", report_task_ids)
    shared_state.set(
        "workflow_runtime_metadata",
        {
            "lead_task_order": lead_task_order,
            "report_task_ids": report_task_ids,
        },
    )
    shared_state.set("policies", effective_agent_team_config.policies.to_dict())
    shared_state.set("team_profiles", [profile.to_dict() for profile in profiles])
    shared_state.set("runtime_config", runtime_config.to_dict())
    shared_state.set("run_resume_from", str(resume_from) if resume_from else "")
    shared_state.set(
        "run_rewind_history_index",
        effective_rewind_history_index if effective_rewind_history_index is not None else "",
    )
    shared_state.set(
        "run_rewind_event_index",
        effective_rewind_event_index if effective_rewind_event_index is not None else "",
    )
    shared_state.set("run_rewind_event_resolution", effective_rewind_event_resolution)
    shared_state.set(
        "run_rewind_source_output_dir",
        str(effective_rewind_source_output_dir) if effective_rewind_source_output_dir else "",
    )
    shared_state.set(
        "run_rewind_source_checkpoint",
        str(effective_rewind_source_checkpoint) if effective_rewind_source_checkpoint else "",
    )
    shared_state.set("run_branch_run_id", effective_branch_run_id)
    shared_state.set(
        "run_rewind_seed_event_index",
        effective_rewind_seed_event_index if effective_rewind_seed_event_index is not None else "",
    )
    shared_state.set("run_rewind_seed_event_count", effective_rewind_seed_event_count)
    shared_state.set("tmux_cleanup_deferred_for_resume", False)
    shared_state.set("tmux_cleanup_deferred_reason", "")
    session_registry = TeammateSessionRegistry(shared_state=shared_state)
    for profile in profiles:
        session_registry.ensure_profile(
            profile=profile,
            transport=teammate_transport_for_profile(profile=profile, runtime_config=runtime_config),
            status="created",
        )
    file_locks = FileLockRegistry(logger=logger)
    lead_context = AgentContext(
        profile=AgentProfile(name=lead_name, skills={"lead"}, agent_type="lead"),
        target_dir=target_dir,
        output_dir=output_dir,
        goal=goal,
        provider=provider,
        runtime_config=runtime_config,
        board=board,
        mailbox=mailbox,
        file_locks=file_locks,
        shared_state=shared_state,
        logger=logger,
        session_registry=session_registry,
    )
    if runtime_config.teammate_mode == "tmux" and recover_tmux_analyst_sessions_fn is not None:
        try:
            recover_tmux_analyst_sessions_fn(
                lead_context=lead_context,
                analyst_profiles=analyst_profiles,
                resume_from=resume_from,
            )
        except Exception as exc:  # pragma: no cover - defensive path
            logger.log(
                "tmux_worker_session_recovery_failed",
                error=f"{type(exc).__name__}: {exc}",
                resume_from=str(resume_from) if resume_from else "",
            )

    stop_event = threading.Event()
    workers = [
        worker_factory(
            context=AgentContext(
                profile=profile,
                target_dir=target_dir,
                output_dir=output_dir,
                goal=goal,
                provider=provider,
                runtime_config=runtime_config,
                board=board,
                mailbox=mailbox,
                file_locks=file_locks,
                shared_state=shared_state,
                logger=logger,
                session_state=session_registry.session_for(profile.name),
                session_registry=session_registry,
            ),
            stop_event=stop_event,
            handlers=workflow_handlers,
            claim_tasks=not (
                runtime_config.teammate_mode == "tmux" and profile.agent_type == "analyst"
            ),
        )
        for profile in profiles
    ]
    if runtime_config.teammate_mode == "tmux":
        logger.log(
            "teammate_mode_tmux_enabled",
            analyst_workers=[profile.name for profile in analyst_profiles],
            reviewer_workers=[profile.name for profile in profiles if profile.agent_type != "analyst"],
        )

    for worker in workers:
        worker.start()

    write_checkpoint(
        checkpoint_path=checkpoint_path,
        goal=goal,
        target_dir=target_dir,
        output_dir=output_dir,
        board=board,
        shared_state=shared_state,
        runtime_config=runtime_config,
        provider_meta=provider_meta,
        resume_from=resume_from,
        interrupted_reason="",
        rewind_history_index=effective_rewind_history_index,
        rewind_event_index=effective_rewind_event_index,
        rewind_event_resolution=effective_rewind_event_resolution,
        rewind_source_output_dir=effective_rewind_source_output_dir,
        rewind_source_checkpoint=effective_rewind_source_checkpoint,
        branch_run_id=effective_branch_run_id,
        event_count=logger.event_count(),
        rewind_seed_event_index=effective_rewind_seed_event_index,
        rewind_seed_event_count=effective_rewind_seed_event_count,
    )

    idle_rounds = 0
    interrupted_reason = ""
    max_completed_tasks = max(0, int(max_completed_tasks))
    try:
        while True:
            ran_tmux_task = False
            if runtime_config.teammate_mode == "tmux":
                while tmux_runner(
                    lead_context=lead_context,
                    analyst_profiles=analyst_profiles,
                    runtime_script=runtime_script_path,
                    worker_timeout_sec=runtime_config.tmux_worker_timeout_sec,
                ):
                    ran_tmux_task = True
            ran_lead_task = run_lead_tasks_once(
                lead_context=lead_context,
                lead_task_order=lead_task_order,
                handlers=workflow_handlers,
            )
            ran_lead_task = ran_lead_task or ran_tmux_task
            messages = mailbox.pull(lead_context.profile.name)
            for message in messages:
                logger.log(
                    "lead_mail_seen",
                    from_agent=message.sender,
                    subject=message.subject,
                    task_id=message.task_id,
                )
                if message.subject == "task_failed":
                    mailbox.broadcast(
                        sender=lead_context.profile.name,
                        subject="halt_notice",
                        body=f"Task failed: {message.task_id}. Continuing to completion checks.",
                    )
            if board.all_terminal():
                break
            if max_completed_tasks > 0:
                board_snapshot = board.snapshot()
                completed_count = sum(
                    1 for task in board_snapshot.get("tasks", []) if task.get("status") == "completed"
                )
                if completed_count >= max_completed_tasks:
                    interrupted_reason = f"max_completed_tasks reached ({completed_count})"
                    logger.log(
                        "run_paused_for_resume",
                        reason=interrupted_reason,
                        checkpoint=str(checkpoint_path),
                    )
                    break
            if not board.has_active_tasks():
                idle_rounds += 1
            elif ran_lead_task:
                idle_rounds = 0
            else:
                idle_rounds = 0
            if idle_rounds > 60:
                interrupted_reason = "run_timeout: no active tasks for prolonged rounds"
                logger.log("run_timeout", reason=interrupted_reason)
                break
            write_checkpoint(
                checkpoint_path=checkpoint_path,
                goal=goal,
                target_dir=target_dir,
                output_dir=output_dir,
                board=board,
                shared_state=shared_state,
                runtime_config=runtime_config,
                provider_meta=provider_meta,
                resume_from=resume_from,
                interrupted_reason=interrupted_reason,
                rewind_history_index=effective_rewind_history_index,
                rewind_event_index=effective_rewind_event_index,
                rewind_event_resolution=effective_rewind_event_resolution,
                rewind_source_output_dir=effective_rewind_source_output_dir,
                rewind_source_checkpoint=effective_rewind_source_checkpoint,
                branch_run_id=effective_branch_run_id,
                event_count=logger.event_count(),
                rewind_seed_event_index=effective_rewind_seed_event_index,
                rewind_seed_event_count=effective_rewind_seed_event_count,
            )
            time.sleep(0.1)
    finally:
        stop_event.set()
        for worker in workers:
            worker.join(timeout=2.0)
        if runtime_config.teammate_mode == "tmux" and cleanup_tmux_analyst_sessions_fn is not None:
            defer_tmux_cleanup = interrupted_reason.startswith("max_completed_tasks reached")
            shared_state.set("tmux_cleanup_deferred_for_resume", defer_tmux_cleanup)
            shared_state.set(
                "tmux_cleanup_deferred_reason",
                interrupted_reason if defer_tmux_cleanup else "",
            )
            try:
                cleanup_tmux_analyst_sessions_fn(
                    lead_context=lead_context,
                    analyst_profiles=analyst_profiles,
                )
            except Exception as exc:  # pragma: no cover - defensive path
                logger.log(
                    "tmux_worker_session_cleanup_failed",
                    error=f"{type(exc).__name__}: {exc}",
                )

    write_checkpoint(
        checkpoint_path=checkpoint_path,
        goal=goal,
        target_dir=target_dir,
        output_dir=output_dir,
        board=board,
        shared_state=shared_state,
        runtime_config=runtime_config,
        provider_meta=provider_meta,
        resume_from=resume_from,
        interrupted_reason=interrupted_reason,
        rewind_history_index=effective_rewind_history_index,
        rewind_event_index=effective_rewind_event_index,
        rewind_event_resolution=effective_rewind_event_resolution,
        rewind_source_output_dir=effective_rewind_source_output_dir,
        rewind_source_checkpoint=effective_rewind_source_checkpoint,
        branch_run_id=effective_branch_run_id,
        event_count=logger.event_count(),
        rewind_seed_event_index=effective_rewind_seed_event_index,
        rewind_seed_event_count=effective_rewind_seed_event_count,
    )

    write_artifacts(
        output_dir=output_dir,
        board=board,
        mailbox=mailbox,
        shared_state=shared_state,
        file_locks=file_locks,
        logger=logger,
        provider_meta=provider_meta,
        runtime_config=runtime_config,
        checkpoint_path=checkpoint_path,
        resume_from=resume_from,
        interrupted_reason=interrupted_reason,
        rewind_history_index=effective_rewind_history_index,
        rewind_event_index=effective_rewind_event_index,
        rewind_event_resolution=effective_rewind_event_resolution,
        rewind_source_output_dir=effective_rewind_source_output_dir,
        rewind_source_checkpoint=effective_rewind_source_checkpoint,
        branch_run_id=effective_branch_run_id,
        rewind_seed_event_index=effective_rewind_seed_event_index,
        rewind_seed_event_count=effective_rewind_seed_event_count,
    )

    snapshot = board.snapshot()
    failures = [task for task in snapshot["tasks"] if task["status"] == "failed"]
    incomplete = [task for task in snapshot["tasks"] if task["status"] != "completed"]
    logger.log(
        "run_finished",
        failed_tasks=len(failures),
        incomplete_tasks=len(incomplete),
        interrupted_reason=interrupted_reason,
        rewind_history_index=(
            effective_rewind_history_index if effective_rewind_history_index is not None else ""
        ),
        rewind_event_index=(
            effective_rewind_event_index if effective_rewind_event_index is not None else ""
        ),
        rewind_event_resolution=effective_rewind_event_resolution,
        rewind_source_output_dir=(
            str(effective_rewind_source_output_dir) if effective_rewind_source_output_dir else ""
        ),
        rewind_source_checkpoint=(
            str(effective_rewind_source_checkpoint) if effective_rewind_source_checkpoint else ""
        ),
        branch_run_id=effective_branch_run_id,
        rewind_seed_event_index=(
            effective_rewind_seed_event_index if effective_rewind_seed_event_index is not None else ""
        ),
        rewind_seed_event_count=effective_rewind_seed_event_count,
    )

    print(f"[lead] goal: {goal}")
    print(f"[lead] target: {target_dir.resolve()}")
    print(f"[lead] output: {output_dir.resolve()}")
    print(f"[lead] tasks total: {len(snapshot['tasks'])}")
    print(f"[lead] tasks failed: {len(failures)}")
    print(f"[lead] tasks incomplete: {len(incomplete)}")
    if interrupted_reason:
        print(f"[lead] run_interrupted: {interrupted_reason}")
    if effective_rewind_history_index is not None:
        print(f"[lead] rewind_history_index: {effective_rewind_history_index}")
    if effective_rewind_event_index is not None:
        print(f"[lead] rewind_event_index: {effective_rewind_event_index}")
    if effective_rewind_event_resolution:
        print(f"[lead] rewind_event_resolution: {json.dumps(effective_rewind_event_resolution, ensure_ascii=False)}")
    if effective_rewind_source_output_dir is not None:
        print(f"[lead] rewind_source_output_dir: {effective_rewind_source_output_dir}")
    if effective_rewind_source_checkpoint is not None:
        print(f"[lead] rewind_source_checkpoint: {effective_rewind_source_checkpoint}")
    if effective_branch_run_id:
        print(f"[lead] branch_run_id: {effective_branch_run_id}")
    if effective_rewind_seed_event_index is not None:
        print(f"[lead] rewind_seed_event_index: {effective_rewind_seed_event_index}")
    if effective_rewind_seed_event_count:
        print(f"[lead] rewind_seed_event_count: {effective_rewind_seed_event_count}")
    print(
        f"[lead] provider: {provider_meta.provider} model={provider_meta.model} mode={provider_meta.mode}"
    )
    print(
        f"[lead] host: {effective_agent_team_config.host.kind} "
        f"transport={effective_agent_team_config.host.session_transport}"
    )
    print(
        f"[lead] workflow: {effective_agent_team_config.workflow.pack} "
        f"preset={effective_agent_team_config.workflow.preset}"
    )
    if effective_agent_team_config.source_path:
        print(f"[lead] config: {effective_agent_team_config.source_path}")
    if provider_meta.note:
        print(f"[lead] provider_note: {provider_meta.note}")
    print(
        f"[lead] adjudication: accept>={runtime_config.adjudication_accept_threshold} "
        f"challenge>={runtime_config.adjudication_challenge_threshold} "
        f"teammate_mode={runtime_config.teammate_mode} "
        f"auto_round3={runtime_config.auto_round3_on_challenge} "
        f"dynamic_tasks={runtime_config.enable_dynamic_tasks} "
        f"teammate_provider_replies={runtime_config.teammate_provider_replies} "
        f"teammate_memory_turns={runtime_config.teammate_memory_turns} "
        f"evidence_wait={runtime_config.evidence_wait_seconds}s "
        f"re_bonus_max={runtime_config.re_adjudication_max_bonus}"
    )
    print(f"[lead] report: {output_dir / 'final_report.md'}")
    print(f"[lead] event log: {output_dir / 'events.jsonl'}")
    print(f"[lead] checkpoint: {checkpoint_path}")
    return 1 if failures else 0
