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
    task_from_dict,
)
from ..host import (
    HOST_RUNTIME_ENFORCEMENT_KEY,
    apply_host_session_backend_enforcement,
    build_host_adapter,
)
from ..models import LLMProvider, build_provider
from ..transports.inprocess import InProcessTeammateAgent
import agent_team.transports.host as host_transport
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
    write_live_lead_interaction_artifacts,
)
from .lead_interaction import (
    PLAN_APPROVAL_STATUS_APPLIED,
    PLAN_APPROVAL_STATUS_PENDING,
    PLAN_APPROVAL_STATUS_REJECTED,
    consume_lead_commands,
    ensure_lead_command_channel,
    list_plan_approval_requests,
    record_lead_command,
    update_plan_approval_request,
)
from .session_state import (
    TeammateSessionRegistry,
    teammate_transport_for_profile,
)
from .task_mutations import apply_task_mutation_payload


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
    runtime_script: Optional[pathlib.Path] = None
    task_context: Dict[str, Any] = dataclasses.field(default_factory=dict)
    session_state: Dict[str, Any] = dataclasses.field(default_factory=dict)
    session_registry: Optional[TeammateSessionRegistry] = None


TaskHandler = Callable[[AgentContext, Task], Dict[str, Any]]
TaskHandlers = Mapping[str, TaskHandler]
TeammateFactory = Callable[..., threading.Thread]
ExternalTaskRunner = Callable[[AgentContext, Task], Optional[Dict[str, Any]]]
TmuxRunner = Callable[[AgentContext, Sequence[AgentProfile], pathlib.Path, int], bool]
HostRunner = Callable[[AgentContext, Sequence[AgentProfile], TaskHandlers], bool]
TmuxCleanupRunner = Callable[[AgentContext, Sequence[AgentProfile]], Any]
TmuxRecoveryRunner = Callable[[AgentContext, Sequence[AgentProfile], Optional[pathlib.Path]], Any]


def _normalize_lead_task_outcome(raw_outcome: Any) -> Dict[str, Any]:
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


def apply_requested_plan_approvals(
    lead_context: AgentContext,
    approve_task_ids: Sequence[str] | None = None,
    reject_task_ids: Sequence[str] | None = None,
    approve_all_pending: bool = False,
    decision_source: str = "runtime",
) -> Dict[str, Any]:
    approved = {str(task_id) for task_id in (approve_task_ids or []) if str(task_id)}
    rejected = {str(task_id) for task_id in (reject_task_ids or []) if str(task_id)}
    pending = list_plan_approval_requests(
        shared_state=lead_context.shared_state,
        status=PLAN_APPROVAL_STATUS_PENDING,
    )
    applied_task_ids: List[str] = []
    rejected_task_ids: List[str] = []
    remaining_task_ids: List[str] = []

    for request in pending:
        task_id = str(request.get("task_id", "") or "")
        if not task_id:
            continue
        if task_id in rejected:
            update_plan_approval_request(
                shared_state=lead_context.shared_state,
                task_id=task_id,
                status=PLAN_APPROVAL_STATUS_REJECTED,
                decision_source=decision_source,
            )
            lead_context.logger.log(
                "plan_approval_rejected",
                task_id=task_id,
                task_type=str(request.get("task_type", "") or ""),
                requested_by=str(request.get("requested_by", "") or ""),
                decision_source=decision_source,
            )
            rejected_task_ids.append(task_id)
            continue
        if approve_all_pending or task_id in approved:
            applied = apply_task_mutation_payload(
                board=lead_context.board,
                shared_state=lead_context.shared_state,
                task_type=str(request.get("task_type", "") or ""),
                updated_by=str(request.get("requested_by", "") or lead_context.profile.name),
                result=request.get("result", {}),
                state_updates=request.get("state_updates", {}),
                task_mutations=request.get("task_mutations", {}),
            )
            update_plan_approval_request(
                shared_state=lead_context.shared_state,
                task_id=task_id,
                status=PLAN_APPROVAL_STATUS_APPLIED,
                decision_source=decision_source,
                applied_task_ids=list(applied.get("inserted_task_ids", [])),
                applied_dependency_ids=list(applied.get("added_dependency_ids", [])),
            )
            lead_context.logger.log(
                "plan_approval_applied",
                task_id=task_id,
                task_type=str(request.get("task_type", "") or ""),
                requested_by=str(request.get("requested_by", "") or ""),
                decision_source=decision_source,
                insert_task_count=len(applied.get("inserted_task_ids", [])),
                add_dependency_count=len(applied.get("added_dependency_ids", [])),
            )
            applied_task_ids.append(task_id)
            continue
        remaining_task_ids.append(task_id)

    return {
        "applied_task_ids": applied_task_ids,
        "rejected_task_ids": rejected_task_ids,
        "pending_task_ids": remaining_task_ids,
    }


def apply_lead_commands(
    lead_context: AgentContext,
    decision_source: str = "lead_command",
) -> Dict[str, Any]:
    consumed = consume_lead_commands(
        output_dir=lead_context.output_dir,
        shared_state=lead_context.shared_state,
        logger=lead_context.logger,
    )
    resolution = apply_requested_plan_approvals(
        lead_context=lead_context,
        approve_task_ids=consumed.get("approve_task_ids", []),
        reject_task_ids=consumed.get("reject_task_ids", []),
        approve_all_pending=bool(consumed.get("approve_all_pending_plans", False)),
        decision_source=decision_source,
    )
    return {
        **consumed,
        **resolution,
    }


def refresh_live_lead_interaction(lead_context: AgentContext) -> Dict[str, Any]:
    return write_live_lead_interaction_artifacts(
        output_dir=lead_context.output_dir,
        shared_state=lead_context.shared_state,
        logger=lead_context.logger,
    )


def parse_interactive_plan_command(raw_command: str) -> Dict[str, Any]:
    text = str(raw_command or "").strip()
    normalized = text.lower()
    if not text or normalized in {"show", "status"}:
        return {"action": "show", "raw": text, "task_id": ""}
    if normalized in {"help", "?"}:
        return {"action": "help", "raw": text, "task_id": ""}
    if normalized in {"pause", "quit", "exit"}:
        return {"action": "pause", "raw": text, "task_id": ""}
    if normalized in {
        "approve-all",
        "approve_all",
        "approve-all-pending",
        "approve_all_pending",
        "approve-all-pending-plans",
        "approve_all_pending_plans",
    }:
        return {"action": "approve_all_pending_plans", "raw": text, "task_id": ""}
    parts = text.split()
    if len(parts) == 2:
        verb = parts[0].strip().lower()
        task_id = parts[1].strip()
        if task_id and verb in {"approve", "approve-plan"}:
            return {"action": "approve_plan", "raw": text, "task_id": task_id}
        if task_id and verb in {"reject", "reject-plan"}:
            return {"action": "reject_plan", "raw": text, "task_id": task_id}
    return {"action": "invalid", "raw": text, "task_id": ""}


def _print_interactive_plan_approval_status(
    pending_plan_approvals: Sequence[Dict[str, Any]],
) -> None:
    print("[lead] interactive_pending_approvals:", flush=True)
    for item in pending_plan_approvals:
        if not isinstance(item, Mapping):
            continue
        print(
            "[lead] "
            f"- {item.get('task_id', '')} ({item.get('task_type', '')}) "
            f"requested_by={item.get('requested_by', '')} "
            f"proposed_tasks={','.join(item.get('proposed_task_ids', [])) or 'none'} "
            f"proposed_dependencies={','.join(item.get('proposed_dependency_ids', [])) or 'none'}",
            flush=True,
        )
    print(
        "[lead] commands: approve <task_id> | reject <task_id> | approve-all | show | pause",
        flush=True,
    )


def run_interactive_plan_approval_prompt(
    lead_context: AgentContext,
) -> Dict[str, Any]:
    pending_plan_approvals = list_plan_approval_requests(
        shared_state=lead_context.shared_state,
        status=PLAN_APPROVAL_STATUS_PENDING,
    )
    if not pending_plan_approvals:
        return {
            "applied_task_ids": [],
            "rejected_task_ids": [],
            "pending_task_ids": [],
            "pause_requested": False,
        }

    lead_context.logger.log(
        "run_waiting_for_plan_approval_interactive",
        pending_task_ids=[
            str(item.get("task_id", "") or "")
            for item in pending_plan_approvals
            if str(item.get("task_id", "") or "")
        ],
        pending_count=len(pending_plan_approvals),
    )
    while pending_plan_approvals:
        refresh_live_lead_interaction(lead_context=lead_context)
        _print_interactive_plan_approval_status(pending_plan_approvals=pending_plan_approvals)
        try:
            raw_command = input("lead-approval> ")
        except EOFError:
            lead_context.logger.log("lead_interactive_input_unavailable", reason="eof")
            return {
                "applied_task_ids": [],
                "rejected_task_ids": [],
                "pending_task_ids": [
                    str(item.get("task_id", "") or "")
                    for item in pending_plan_approvals
                    if str(item.get("task_id", "") or "")
                ],
                "pause_requested": False,
                "interactive_unavailable": True,
            }
        except KeyboardInterrupt:
            print("", flush=True)
            lead_context.logger.log("lead_interactive_pause_requested", reason="keyboard_interrupt")
            return {
                "applied_task_ids": [],
                "rejected_task_ids": [],
                "pending_task_ids": [
                    str(item.get("task_id", "") or "")
                    for item in pending_plan_approvals
                    if str(item.get("task_id", "") or "")
                ],
                "pause_requested": True,
            }

        parsed = parse_interactive_plan_command(raw_command=raw_command)
        action = str(parsed.get("action", "") or "")
        task_id = str(parsed.get("task_id", "") or "")
        task_ids = [task_id] if task_id else []
        if str(raw_command or "").strip():
            record_lead_command(
                shared_state=lead_context.shared_state,
                command=action,
                task_ids=task_ids,
                raw=str(raw_command or ""),
                valid=action not in {"invalid"},
                source="interactive",
            )
        lead_context.logger.log(
            "lead_interactive_command_received",
            action=action,
            task_id=task_id,
            raw_command=str(raw_command or ""),
        )
        if action == "show":
            continue
        if action == "help":
            print(
                "[lead] help: approve <task_id> | reject <task_id> | approve-all | show | pause",
                flush=True,
            )
            continue
        if action == "pause":
            lead_context.logger.log("lead_interactive_pause_requested", reason="user_command")
            return {
                "applied_task_ids": [],
                "rejected_task_ids": [],
                "pending_task_ids": [
                    str(item.get("task_id", "") or "")
                    for item in pending_plan_approvals
                    if str(item.get("task_id", "") or "")
                ],
                "pause_requested": True,
            }
        if action == "invalid":
            print("[lead] invalid command. Type `help` for options.", flush=True)
            continue

        resolution = apply_requested_plan_approvals(
            lead_context=lead_context,
            approve_task_ids=task_ids if action == "approve_plan" else [],
            reject_task_ids=task_ids if action == "reject_plan" else [],
            approve_all_pending=action == "approve_all_pending_plans",
            decision_source="interactive",
        )
        if not resolution.get("applied_task_ids") and not resolution.get("rejected_task_ids"):
            if task_id:
                print(f"[lead] no matching pending plan for {task_id}", flush=True)
            else:
                print("[lead] no pending plans matched that command", flush=True)
        pending_plan_approvals = list_plan_approval_requests(
            shared_state=lead_context.shared_state,
            status=PLAN_APPROVAL_STATUS_PENDING,
        )
        if not pending_plan_approvals:
            refresh_live_lead_interaction(lead_context=lead_context)
            return {
                **resolution,
                "pending_task_ids": [],
                "pause_requested": False,
            }
    return {
        "applied_task_ids": [],
        "rejected_task_ids": [],
        "pending_task_ids": [],
        "pause_requested": False,
    }


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


def _missing_host_runner(
    lead_context: AgentContext,
    teammate_profiles: Sequence[AgentProfile],
    handlers: TaskHandlers,
) -> bool:
    del lead_context, teammate_profiles, handlers
    raise RuntimeError("host teammate mode requires a host runner function")


def run_lead_task_once(
    lead_context: AgentContext,
    task_id: str,
    handlers: TaskHandlers,
    external_task_runner: Optional[ExternalTaskRunner] = None,
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
        delegated = None
        if external_task_runner is not None:
            delegated = external_task_runner(lead_context, task)
        if delegated is not None:
            if not delegated.get("ok", False):
                error = str(delegated.get("error", "external task failed"))
                lead_context.board.fail(task_id=task.task_id, owner=lead_context.profile.name, error=error)
                lead_context.mailbox.send(
                    sender=lead_context.profile.name,
                    recipient=lead_context.profile.name,
                    subject="task_failed",
                    body=error,
                    task_id=task.task_id,
                )
                return True
            delegated_outcome = _normalize_lead_task_outcome(
                {
                    "result": delegated.get("result", {}),
                    "state_updates": delegated.get("state_updates", {}),
                    "task_mutations": delegated.get("task_mutations", delegated.get("board_mutations", {})),
                }
            )
            applied = apply_task_mutation_payload(
                board=lead_context.board,
                shared_state=lead_context.shared_state,
                task_type=task.task_type,
                updated_by=lead_context.profile.name,
                result=delegated_outcome.get("result", {}),
                state_updates=delegated_outcome.get("state_updates", {}),
                task_mutations=delegated_outcome.get("task_mutations", {}),
            )
            result = applied.get("result", {})
        else:
            handler_outcome = _normalize_lead_task_outcome(handler(lead_context, task))
            applied = apply_task_mutation_payload(
                board=lead_context.board,
                shared_state=lead_context.shared_state,
                task_type=task.task_type,
                updated_by=lead_context.profile.name,
                result=handler_outcome.get("result", {}),
                state_updates=handler_outcome.get("state_updates", {}),
                task_mutations=handler_outcome.get("task_mutations", {}),
            )
            result = applied.get("result", {})
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
    external_task_runner: Optional[ExternalTaskRunner] = None,
    traceback_module: Any = traceback,
) -> bool:
    ran_any = False
    for task_id in lead_task_order:
        if run_lead_task_once(
            lead_context=lead_context,
            task_id=str(task_id),
            handlers=handlers,
            external_task_runner=external_task_runner,
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
    external_lead_task_runner: Optional[ExternalTaskRunner] = None,
    run_tmux_analyst_task_once_fn: Optional[TmuxRunner] = None,
    run_host_teammate_task_once_fn: Optional[HostRunner] = None,
    recover_tmux_analyst_sessions_fn: Optional[TmuxRecoveryRunner] = None,
    cleanup_tmux_analyst_sessions_fn: Optional[TmuxCleanupRunner] = None,
    runtime_script: Optional[pathlib.Path] = None,
    approve_plan_task_ids: Optional[Sequence[str]] = None,
    reject_plan_task_ids: Optional[Sequence[str]] = None,
    approve_all_pending_plans: bool = False,
    lead_command_wait_seconds: float = 0.0,
    lead_interactive: bool = False,
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
    host_runner = run_host_teammate_task_once_fn or _missing_host_runner
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
    host_metadata = host_adapter.runtime_metadata()
    host_runtime_enforcement = host_adapter.runtime_enforcement(
        runtime_config=runtime_config,
        policies=effective_agent_team_config.policies,
    )
    if runtime_config.teammate_mode == "host":
        host_runtime_enforcement = apply_host_session_backend_enforcement(
            host_runtime_enforcement,
            backend=host_transport.host_session_backend_metadata(),
        )

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
        host=host_metadata,
        host_runtime_enforcement=host_runtime_enforcement,
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
    mailbox = Mailbox(
        participants=participants,
        logger=logger,
        storage_dir=output_dir / "_mailbox",
        clear_storage=True,
    )
    shared_state = SharedState()
    if resume_payload:
        restore_shared_state_from_checkpoint_payload(shared_state=shared_state, checkpoint_payload=resume_payload)
    shared_state.set("lead_name", lead_name)
    shared_state.set("agent_team_config", effective_agent_team_config.to_dict())
    shared_state.set("host", host_metadata)
    shared_state.set(HOST_RUNTIME_ENFORCEMENT_KEY, host_runtime_enforcement)
    logger.log("host_runtime_enforcement_resolved", **host_runtime_enforcement)
    runtime_team_snapshot = effective_agent_team_config.team.to_dict()
    runtime_team_snapshot["mailbox_model"] = mailbox.model_name()
    runtime_team_snapshot["mailbox_storage_dir"] = str(mailbox.storage_dir) if mailbox.storage_dir else ""
    shared_state.set("team", runtime_team_snapshot)
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
    shared_state.set(
        "plan_approval_controls",
        {
            "approve_all_pending": bool(approve_all_pending_plans),
            "approve_task_ids": [str(task_id) for task_id in (approve_plan_task_ids or []) if str(task_id)],
            "reject_task_ids": [str(task_id) for task_id in (reject_plan_task_ids or []) if str(task_id)],
            "lead_command_wait_seconds": float(lead_command_wait_seconds),
            "lead_interactive": bool(lead_interactive),
        },
    )
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
    ensure_lead_command_channel(output_dir=output_dir, shared_state=shared_state)
    session_registry = TeammateSessionRegistry(shared_state=shared_state)
    initial_session_states: Dict[str, Dict[str, Any]] = {}
    for profile in profiles:
        transport = teammate_transport_for_profile(profile=profile, runtime_config=runtime_config)
        session_state = session_registry.activate_for_run(
            profile=profile,
            transport=transport,
            resume_from=str(resume_from) if resume_from else "",
        )
        initial_session_states[profile.name] = session_state
        lifecycle_event = str(session_state.get("lifecycle_event", "") or "initialized")
        logger.log(
            f"teammate_session_{lifecycle_event}",
            agent=profile.name,
            agent_type=profile.agent_type,
            transport=transport,
            session_id=str(session_state.get("session_id", "") or ""),
            resume_from=str(session_state.get("resume_from", "") or ""),
            provider_memory_entries=len(session_state.get("provider_memory", [])),
            task_history_entries=len(session_state.get("task_history", [])),
            run_activations=int(session_state.get("run_activations", 0) or 0),
            initialization_count=int(session_state.get("initialization_count", 0) or 0),
            resume_count=int(session_state.get("resume_count", 0) or 0),
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
        runtime_script=runtime_script_path,
        session_registry=session_registry,
    )
    refresh_live_lead_interaction(lead_context=lead_context)
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
    workers: List[threading.Thread] = []
    if runtime_config.teammate_mode != "host":
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
                    mailbox=mailbox.transport_view(),
                    file_locks=file_locks,
                    shared_state=shared_state,
                    logger=logger,
                    runtime_script=runtime_script_path,
                    session_state=initial_session_states.get(profile.name, session_registry.session_for(profile.name)),
                    session_registry=session_registry,
                ),
                stop_event=stop_event,
                handlers=workflow_handlers,
                claim_tasks=not (
                    runtime_config.teammate_mode in {"tmux", "subprocess"}
                    and profile.agent_type == "analyst"
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
    if runtime_config.teammate_mode == "subprocess":
        logger.log(
            "teammate_mode_subprocess_enabled",
            analyst_workers=[profile.name for profile in analyst_profiles],
            reviewer_workers=[profile.name for profile in profiles if profile.agent_type != "analyst"],
        )
    if runtime_config.teammate_mode == "host":
        host_transport.configure_host_session_workers(
            lead_context=lead_context,
            workflow_pack=workflow_pack_name,
            model_config=effective_agent_team_config.model,
        )
        logger.log(
            "teammate_mode_host_enabled",
            teammate_workers=[profile.name for profile in profiles],
            host_kind=host_metadata.get("kind", ""),
            host_session_transport=host_metadata.get("session_transport", ""),
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
            live_command_resolution = apply_lead_commands(lead_context=lead_context)
            approval_resolution = apply_requested_plan_approvals(
                lead_context=lead_context,
                approve_task_ids=approve_plan_task_ids,
                reject_task_ids=reject_plan_task_ids,
                approve_all_pending=approve_all_pending_plans,
                decision_source="cli",
            )
            ran_approval_action = bool(
                live_command_resolution.get("applied_task_ids")
                or live_command_resolution.get("rejected_task_ids")
                or live_command_resolution.get("consumed_count")
                or approval_resolution.get("applied_task_ids")
                or approval_resolution.get("rejected_task_ids")
            )
            refresh_live_lead_interaction(lead_context=lead_context)
            ran_tmux_task = False
            ran_host_task = False
            if runtime_config.teammate_mode in {"tmux", "subprocess"}:
                while tmux_runner(
                    lead_context=lead_context,
                    analyst_profiles=analyst_profiles,
                    runtime_script=runtime_script_path,
                    worker_timeout_sec=runtime_config.tmux_worker_timeout_sec,
                ):
                    ran_tmux_task = True
            if runtime_config.teammate_mode == "host":
                while host_runner(
                    lead_context=lead_context,
                    teammate_profiles=profiles,
                    handlers=workflow_handlers,
                ):
                    ran_host_task = True
            ran_lead_task = run_lead_tasks_once(
                lead_context=lead_context,
                lead_task_order=lead_task_order,
                handlers=workflow_handlers,
                external_task_runner=external_lead_task_runner,
            )
            ran_lead_task = ran_lead_task or ran_tmux_task or ran_host_task or ran_approval_action
            messages = mailbox.pull(lead_context.profile.name)
            for message in messages:
                logger.log(
                    "lead_mail_seen",
                    from_agent=message.sender,
                    subject=message.subject,
                    task_id=message.task_id,
                )
                if runtime_config.teammate_mode == "host":
                    if message.subject == host_transport.SESSION_TELEMETRY_SUBJECT:
                        host_transport.apply_host_session_telemetry_message(
                            lead_context=lead_context,
                            message=message,
                        )
                        continue
                    if message.subject == host_transport.SESSION_TASK_RESULT_SUBJECT:
                        host_transport.apply_host_session_result_message(
                            lead_context=lead_context,
                            message=message,
                        )
                        continue
                if message.subject == "task_failed":
                    mailbox.broadcast(
                        sender=lead_context.profile.name,
                        subject="halt_notice",
                        body=f"Task failed: {message.task_id}. Continuing to completion checks.",
                    )
            approval_resolution = apply_requested_plan_approvals(
                lead_context=lead_context,
                approve_task_ids=approve_plan_task_ids,
                reject_task_ids=reject_plan_task_ids,
                approve_all_pending=approve_all_pending_plans,
                decision_source="cli",
            )
            live_command_resolution = apply_lead_commands(lead_context=lead_context)
            ran_lead_task = ran_lead_task or bool(
                live_command_resolution.get("applied_task_ids")
                or live_command_resolution.get("rejected_task_ids")
                or approval_resolution.get("applied_task_ids")
                or approval_resolution.get("rejected_task_ids")
            )
            refresh_live_lead_interaction(lead_context=lead_context)
            pending_plan_approvals = list_plan_approval_requests(
                shared_state=lead_context.shared_state,
                status=PLAN_APPROVAL_STATUS_PENDING,
            )
            if pending_plan_approvals:
                if lead_command_wait_seconds > 0:
                    logger.log(
                        "run_waiting_for_plan_approval_live",
                        pending_task_ids=[
                            str(item.get("task_id", "") or "")
                            for item in pending_plan_approvals
                            if str(item.get("task_id", "") or "")
                        ],
                        pending_count=len(pending_plan_approvals),
                        lead_command_wait_seconds=float(lead_command_wait_seconds),
                    )
                wait_deadline = time.time() + max(0.0, float(lead_command_wait_seconds))
                while pending_plan_approvals and time.time() < wait_deadline:
                    live_command_resolution = apply_lead_commands(lead_context=lead_context)
                    if (
                        live_command_resolution.get("applied_task_ids")
                        or live_command_resolution.get("rejected_task_ids")
                    ):
                        ran_lead_task = True
                    refresh_live_lead_interaction(lead_context=lead_context)
                    pending_plan_approvals = list_plan_approval_requests(
                        shared_state=lead_context.shared_state,
                        status=PLAN_APPROVAL_STATUS_PENDING,
                    )
                    if not pending_plan_approvals:
                        break
                    time.sleep(0.2)
            if pending_plan_approvals and lead_interactive:
                interactive_resolution = run_interactive_plan_approval_prompt(
                    lead_context=lead_context,
                )
                if (
                    interactive_resolution.get("applied_task_ids")
                    or interactive_resolution.get("rejected_task_ids")
                ):
                    ran_lead_task = True
                pending_plan_approvals = list_plan_approval_requests(
                    shared_state=lead_context.shared_state,
                    status=PLAN_APPROVAL_STATUS_PENDING,
                )
                if interactive_resolution.get("pause_requested"):
                    pending_task_ids = [
                        str(item.get("task_id", "") or "")
                        for item in pending_plan_approvals
                        if str(item.get("task_id", "") or "")
                    ]
                    interrupted_reason = (
                        "pending_plan_approval: " + ",".join(pending_task_ids[:5])
                    )
            if pending_plan_approvals:
                pending_task_ids = [str(item.get("task_id", "") or "") for item in pending_plan_approvals if str(item.get("task_id", "") or "")]
                interrupted_reason = (
                    "pending_plan_approval: " + ",".join(pending_task_ids[:5])
                )
                refresh_live_lead_interaction(lead_context=lead_context)
                logger.log(
                    "run_paused_for_plan_approval",
                    pending_task_ids=pending_task_ids,
                    pending_count=len(pending_task_ids),
                    approve_all_pending=bool(approve_all_pending_plans),
                    approved_task_ids=[str(task_id) for task_id in (approve_plan_task_ids or []) if str(task_id)],
                    rejected_task_ids=[str(task_id) for task_id in (reject_plan_task_ids or []) if str(task_id)],
                    lead_command_wait_seconds=float(lead_command_wait_seconds),
                    lead_interactive=bool(lead_interactive),
                )
                break
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
            refresh_live_lead_interaction(lead_context=lead_context)
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
        if runtime_config.teammate_mode == "host":
            host_transport.stop_host_session_workers(lead_context=lead_context)
            host_transport.apply_host_session_telemetry_messages(lead_context=lead_context)
            host_transport.apply_host_session_result_messages(lead_context=lead_context)
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
