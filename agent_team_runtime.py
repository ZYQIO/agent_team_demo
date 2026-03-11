#!/usr/bin/env python3
"""
Local in-process agent team runtime (MVP).

This runtime is inspired by Claude Code Agent Teams patterns:
- lead + teammates
- shared task board with dependencies and claim-lock
- inter-agent mailbox
- file-path lock registry
- event logs and final artifacts
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import pathlib
import shutil
import subprocess
import sys
import threading
import traceback
from typing import Any, Dict, List, Mapping, Optional, Sequence

from agent_team.config import (
    AgentTeamConfig,
    RuntimeConfig,
    TeamAgentConfig,
    TeamConfig,
    build_agent_team_config,
    default_host_config,
    load_agent_team_config,
)
from agent_team.core import (
    AgentProfile,
    EventLogger,
    FileLockRegistry,
    HOOK_EVENT_TASK_COMPLETED,
    Mailbox,
    Message,
    SharedState,
    Task,
    TaskBoard,
    task_from_dict,
    utc_now,
)
from agent_team.runtime import (
    CHECKPOINT_FILENAME,
    CHECKPOINT_HISTORY_DIRNAME,
    CHECKPOINT_VERSION,
    CONTEXT_BOUNDARY_FILENAME,
    HOST_ENFORCEMENT_FILENAME,
    LEAD_COMMANDS_FILENAME,
    LEAD_INTERACTION_FILENAME,
    LEAD_INTERACTION_REPORT_FILENAME,
    PLAN_APPROVAL_STATUS_APPLIED,
    PLAN_APPROVAL_STATUS_PENDING,
    PLAN_APPROVAL_STATUS_REJECTED,
    SESSION_BOUNDARY_FILENAME,
    ScopedSharedState,
    TEAMMATE_SESSIONS_FILENAME,
    TEAM_PROGRESS_FILENAME,
    TEAM_PROGRESS_REPORT_FILENAME,
    TeammateSessionRegistry,
    build_context_boundary_summary,
    build_host_enforcement_snapshot,
    build_lead_interaction_snapshot,
    build_session_boundary_snapshot,
    build_targeted_evidence_question,
    build_teammate_sessions_snapshot,
    build_team_progress_snapshot,
    build_task_context_snapshot,
    consume_lead_commands,
    compute_adjudication,
    compute_evidence_bonus,
    default_event_rewind_branch_output_dir,
    default_history_replay_report_path,
    default_rewind_branch_output_dir,
    derive_evidence_focus_areas,
    ensure_lead_command_channel,
    get_lead_interaction_state,
    load_checkpoint,
    list_plan_approval_requests,
    queue_plan_approval_request,
    replay_task_states_from_events,
    resolve_checkpoint_by_event_index,
    resolve_checkpoint_by_history_index,
    seed_branch_events_from_source,
    teammate_transport_for_profile,
    visible_state_keys_for_task,
    write_event_replay_report,
    write_history_replay_report,
    write_live_lead_interaction_artifacts,
    write_team_progress_report,
)
from agent_team.runtime.engine import (
    AgentContext,
    TaskHandler,
    apply_requested_plan_approvals,
    build_profiles,
    get_lead_name,
    get_team_member_names,
    get_team_profiles,
    profile_has_skill,
    run_lead_task_once as run_lead_task_once_impl,
    run_team as run_team_impl,
)
from agent_team.transports.inprocess import (
    SESSION_TASK_ASSIGNMENT_SUBJECT,
    SESSION_TASK_RESULT_SUBJECT,
    InProcessTeammateAgent,
)
from agent_team.runtime.session_state import SESSION_TELEMETRY_SUBJECT
import agent_team.transports.host as host_transport
import agent_team.transports.tmux as tmux_transport
from agent_team.workflows import build_workflow_handlers, build_workflow_tasks
from agent_team.workflows.markdown_audit_handlers import (
    get_latest_agent_reply,
    handle_discover_markdown,
    handle_dynamic_planning,
    handle_evidence_pack,
    handle_heading_audit,
    handle_heading_structure_followup,
    handle_lead_adjudication,
    handle_lead_re_adjudication,
    handle_length_audit,
    handle_length_risk_followup,
    handle_llm_synthesis,
    handle_peer_challenge,
    handle_recommendation_pack,
)
from agent_team.models import build_provider


TMUX_ANALYST_TASK_TYPES = tmux_transport.TMUX_ANALYST_TASK_TYPES
MAILBOX_REVIEWER_TASK_TYPES = tmux_transport.MAILBOX_REVIEWER_TASK_TYPES
SUBPROCESS_REVIEWER_TASK_TYPES = tmux_transport.SUBPROCESS_REVIEWER_TASK_TYPES


def run_tmux_worker_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    return tmux_transport.run_tmux_worker_payload(payload)


def run_tmux_worker_entrypoint(task_file: pathlib.Path) -> int:
    return tmux_transport.run_tmux_worker_entrypoint(
        task_file=task_file,
        run_payload_fn=run_tmux_worker_payload,
    )

HANDLERS: Dict[str, TaskHandler] = build_workflow_handlers("markdown-audit")


class TeammateAgent(InProcessTeammateAgent):
    def __init__(
        self,
        context: AgentContext,
        stop_event: threading.Event,
        handlers: Optional[Mapping[str, TaskHandler]] = None,
        claim_tasks: bool = True,
    ) -> None:
        selected_handlers = dict(handlers or HANDLERS)
        super().__init__(
            context=context,
            stop_event=stop_event,
            claim_tasks=claim_tasks,
            handlers=selected_handlers,
            get_lead_name_fn=get_lead_name,
            profile_has_skill_fn=profile_has_skill,
            traceback_module=traceback,
        )


def _execute_worker_subprocess(
    command: List[str],
    timeout_sec: int,
    worker_env: Optional[Dict[str, str]] = None,
    workdir: Optional[pathlib.Path] = None,
) -> subprocess.CompletedProcess[str]:
    return tmux_transport.execute_worker_subprocess(
        command=command,
        timeout_sec=timeout_sec,
        worker_env=worker_env,
        workdir=workdir,
    )


def _execute_worker_tmux(
    command: List[str],
    workdir: pathlib.Path,
    session_prefix: str,
    timeout_sec: int,
    retain_session_for_reuse: bool = False,
    allow_existing_session_reuse: bool = False,
    worker_env: Optional[Dict[str, str]] = None,
    session_workspace_root: str = "",
    session_workspace_workdir: str = "",
    session_workspace_home_dir: str = "",
    session_workspace_target_dir: str = "",
    session_workspace_tmp_dir: str = "",
) -> subprocess.CompletedProcess[str]:
    return tmux_transport.execute_worker_tmux(
        command=command,
        workdir=workdir,
        session_prefix=session_prefix,
        timeout_sec=timeout_sec,
        retain_session_for_reuse=retain_session_for_reuse,
        allow_existing_session_reuse=allow_existing_session_reuse,
        worker_env=worker_env,
        session_workspace_root=session_workspace_root,
        session_workspace_workdir=session_workspace_workdir,
        session_workspace_home_dir=session_workspace_home_dir,
        session_workspace_target_dir=session_workspace_target_dir,
        session_workspace_tmp_dir=session_workspace_tmp_dir,
    )


def _run_tmux_worker_task(
    runtime_script: pathlib.Path,
    output_dir: pathlib.Path,
    runtime_config: RuntimeConfig,
    payload: Dict[str, Any],
    worker_name: str,
    logger: EventLogger,
    timeout_sec: int = 120,
    retain_session_for_reuse: bool = False,
    allow_existing_session_reuse: bool = False,
) -> Dict[str, Any]:
    return tmux_transport.run_tmux_worker_task(
        runtime_script=runtime_script,
        output_dir=output_dir,
        runtime_config=runtime_config,
        payload=payload,
        worker_name=worker_name,
        logger=logger,
        timeout_sec=timeout_sec,
        retain_session_for_reuse=retain_session_for_reuse,
        allow_existing_session_reuse=allow_existing_session_reuse,
        execute_worker_tmux_fn=_execute_worker_tmux,
        execute_worker_subprocess_fn=_execute_worker_subprocess,
        which_fn=shutil.which,
    )


def cleanup_tmux_analyst_sessions(
    lead_context: AgentContext,
    analyst_profiles: Sequence[AgentProfile],
) -> Dict[str, Any]:
    return tmux_transport.cleanup_tmux_analyst_sessions(
        lead_context=lead_context,
        analyst_profiles=analyst_profiles,
    )


def recover_tmux_analyst_sessions(
    lead_context: AgentContext,
    analyst_profiles: Sequence[AgentProfile],
    resume_from: Optional[pathlib.Path] = None,
) -> Dict[str, Any]:
    return tmux_transport.recover_tmux_analyst_sessions(
        lead_context=lead_context,
        analyst_profiles=analyst_profiles,
        resume_from=resume_from,
    )


def run_host_teammate_task_once(
    lead_context: AgentContext,
    teammate_profiles: Sequence[AgentProfile],
    handlers: Mapping[str, TaskHandler],
) -> bool:
    return host_transport.run_host_teammate_task_once(
        lead_context=lead_context,
        teammate_profiles=teammate_profiles,
        handlers=handlers,
    )


def run_host_session_worker_entrypoint(payload_file: pathlib.Path) -> int:
    return host_transport.run_host_session_worker_entrypoint(payload_file=payload_file)


def apply_host_session_result_messages(lead_context: AgentContext) -> int:
    return host_transport.apply_host_session_result_messages(lead_context=lead_context)


def apply_host_session_telemetry_messages(lead_context: AgentContext) -> int:
    return host_transport.apply_host_session_telemetry_messages(lead_context=lead_context)


def run_tmux_analyst_task_once(
    lead_context: AgentContext,
    analyst_profiles: Sequence[AgentProfile],
    runtime_script: pathlib.Path,
    worker_timeout_sec: int = 120,
) -> bool:
    return tmux_transport.run_tmux_analyst_task_once(
        lead_context=lead_context,
        analyst_profiles=analyst_profiles,
        runtime_script=runtime_script,
        run_worker_task_fn=_run_tmux_worker_task,
        supported_task_types=TMUX_ANALYST_TASK_TYPES,
        worker_timeout_sec=worker_timeout_sec,
    )


def run_external_lead_task(
    lead_context: AgentContext,
    task: Task,
) -> Optional[Dict[str, Any]]:
    runtime_script = getattr(lead_context, "runtime_script", None)
    if lead_context.runtime_config.teammate_mode != "tmux":
        return None
    if runtime_script is None:
        return None
    if task.task_type not in tmux_transport.TMUX_LEAD_EXTERNAL_TASK_TYPES:
        return None
    task_context = build_task_context_snapshot(lead_context, task)
    execution = tmux_transport.run_external_tmux_task(
        context=lead_context,
        task=task,
        runtime_script=pathlib.Path(runtime_script).resolve(),
        task_context=task_context,
        record_boundary=False,
        timeout_sec=int(lead_context.runtime_config.tmux_worker_timeout_sec),
    )
    if not execution.get("ok"):
        return {
            "ok": False,
            "error": str(execution.get("error", "unknown worker error")),
            "transport": str(execution.get("transport", "") or "tmux"),
        }
    payload = execution.get("payload", {})
    if not isinstance(payload, dict):
        payload = {}
    return {
        "ok": True,
        "result": payload.get("result", {}),
        "state_updates": payload.get("state_updates", {}),
        "task_mutations": payload.get("task_mutations", {}),
        "transport": str(execution.get("transport", "") or "tmux"),
    }


def build_tasks(
    output_dir: pathlib.Path,
    runtime_config: RuntimeConfig,
    workflow_pack: str = "markdown-audit",
    workflow_options: Optional[Dict[str, Any]] = None,
) -> List[Task]:
    return build_workflow_tasks(
        workflow_pack=workflow_pack,
        output_dir=output_dir,
        runtime_config=runtime_config,
        workflow_options=workflow_options,
    )


def run_lead_task_once(lead_context: AgentContext, task_id: str) -> bool:
    return run_lead_task_once_impl(
        lead_context=lead_context,
        task_id=task_id,
        handlers=HANDLERS,
        external_task_runner=run_external_lead_task,
        traceback_module=traceback,
    )


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
    approve_plan_task_ids: Optional[Sequence[str]] = None,
    reject_plan_task_ids: Optional[Sequence[str]] = None,
    approve_all_pending_plans: bool = False,
    lead_command_wait_seconds: float = 0.0,
) -> int:
    return run_team_impl(
        goal=goal,
        target_dir=target_dir,
        output_dir=output_dir,
        runtime_config=runtime_config,
        provider_name=provider_name,
        model=model,
        openai_api_key_env=openai_api_key_env,
        openai_base_url=openai_base_url,
        require_llm=require_llm,
        provider_timeout_sec=provider_timeout_sec,
        resume_from=resume_from,
        max_completed_tasks=max_completed_tasks,
        rewind_history_index=rewind_history_index,
        rewind_event_index=rewind_event_index,
        rewind_event_resolution=rewind_event_resolution,
        rewind_source_output_dir=rewind_source_output_dir,
        rewind_source_checkpoint=rewind_source_checkpoint,
        branch_run_id=branch_run_id,
        agent_team_config=agent_team_config,
        approve_plan_task_ids=approve_plan_task_ids,
        reject_plan_task_ids=reject_plan_task_ids,
        approve_all_pending_plans=approve_all_pending_plans,
        lead_command_wait_seconds=lead_command_wait_seconds,
        teammate_agent_factory=TeammateAgent,
        external_lead_task_runner=run_external_lead_task,
        run_tmux_analyst_task_once_fn=run_tmux_analyst_task_once,
        run_host_teammate_task_once_fn=run_host_teammate_task_once,
        recover_tmux_analyst_sessions_fn=recover_tmux_analyst_sessions,
        cleanup_tmux_analyst_sessions_fn=cleanup_tmux_analyst_sessions,
        runtime_script=pathlib.Path(__file__).resolve(),
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run local agent team runtime (MVP).")
    parser.add_argument(
        "--goal",
        default="Audit repository quality with a lead and teammate workflow.",
        help="Natural language goal statement.",
    )
    parser.add_argument(
        "--target",
        default=".",
        help="Target directory for workflow analysis.",
    )
    parser.add_argument(
        "--output",
        default="agent_team_demo/output",
        help="Output directory for artifacts.",
    )
    parser.add_argument(
        "--config",
        default="",
        help="Optional JSON config file for host/model/team/workflow defaults.",
    )
    parser.add_argument(
        "--host-kind",
        default="",
        help="Host adapter metadata used to describe runtime capabilities.",
    )
    parser.add_argument(
        "--workflow-pack",
        default="",
        help="Workflow pack to run. Built-in options: markdown-audit, repo-audit.",
    )
    parser.add_argument(
        "--workflow-preset",
        default="",
        help="Workflow preset label stored in artifacts for skill/host integration.",
    )
    parser.add_argument(
        "--resume-from",
        default="",
        help="Checkpoint path to resume from (for example: agent_team_demo/output/run_checkpoint.json).",
    )
    parser.add_argument(
        "--rewind-to-history-index",
        type=int,
        default=-1,
        help="Resume from a checkpoint history index under output/_checkpoint_history (>=0 enables rewind).",
    )
    parser.add_argument(
        "--rewind-to-event-index",
        type=int,
        default=-1,
        help="Resume from the nearest checkpoint mapped from an events.jsonl event index (>=0 enables rewind).",
    )
    parser.add_argument(
        "--rewind-branch",
        action="store_true",
        help="When rewinding, write outputs to a new branch directory under output/branches.",
    )
    parser.add_argument(
        "--rewind-branch-output",
        default="",
        help="Optional explicit output directory for rewind branch runs (requires --rewind-to-history-index).",
    )
    parser.add_argument(
        "--max-completed-tasks",
        type=int,
        default=0,
        help="Stop early after this many completed tasks to simulate an interrupted run (0 disables).",
    )
    parser.add_argument(
        "--teammate-plan-required",
        dest="teammate_plan_required",
        action="store_true",
        help="Require explicit lead approval before teammate-generated task mutations are applied.",
    )
    parser.add_argument(
        "--no-teammate-plan-required",
        dest="teammate_plan_required",
        action="store_false",
        help="Allow teammate-generated task mutations to apply immediately.",
    )
    parser.add_argument(
        "--approve-plan",
        action="append",
        default=[],
        help="Approve a pending teammate plan by task id. Can be specified multiple times.",
    )
    parser.add_argument(
        "--reject-plan",
        action="append",
        default=[],
        help="Reject a pending teammate plan by task id. Can be specified multiple times.",
    )
    parser.add_argument(
        "--approve-all-pending-plans",
        action="store_true",
        help="Automatically approve all pending teammate plan requests during this run.",
    )
    parser.add_argument(
        "--lead-command-wait-seconds",
        type=float,
        default=0.0,
        help="When plan approval is pending, keep the run alive for this many seconds to consume live lead commands from lead_commands.jsonl before pausing.",
    )
    parser.add_argument(
        "--history-replay-report",
        action="store_true",
        help="Generate checkpoint history replay report and exit.",
    )
    parser.add_argument(
        "--history-replay-report-path",
        default="",
        help="Optional output path for history replay report markdown.",
    )
    parser.add_argument(
        "--history-replay-start-index",
        type=int,
        default=-1,
        help="Start history index for replay report (default: earliest).",
    )
    parser.add_argument(
        "--history-replay-end-index",
        type=int,
        default=-1,
        help="End history index for replay report (default: latest).",
    )
    parser.add_argument(
        "--event-replay-report",
        action="store_true",
        help="Generate event replay report from events.jsonl and exit.",
    )
    parser.add_argument(
        "--event-replay-report-path",
        default="",
        help="Optional output path for event replay report markdown.",
    )
    parser.add_argument(
        "--event-replay-max-transitions",
        type=int,
        default=200,
        help="Maximum transition rows included in event replay report.",
    )
    parser.add_argument(
        "--teammate-mode",
        default="in-process",
        choices=["in-process", "subprocess", "tmux", "host"],
        help=(
            "Teammate execution mode. `subprocess` uses process workers for analyst tasks plus selected "
            "reviewer tasks; `tmux` uses process workers for analyst tasks; `host` uses the host transport "
            "skeleton for teammate dispatch when host-managed sessions are requested."
        ),
    )
    parser.add_argument(
        "--provider",
        default="heuristic",
        choices=["heuristic", "openai"],
        help="LLM provider used by llm_synthesis task.",
    )
    parser.add_argument(
        "--model",
        default="heuristic-v1",
        help="Model name for provider. For openai, example: gpt-4.1-mini.",
    )
    parser.add_argument(
        "--openai-api-key-env",
        default="OPENAI_API_KEY",
        help="Environment variable that stores OpenAI API key.",
    )
    parser.add_argument(
        "--openai-base-url",
        default="https://api.openai.com/v1",
        help="OpenAI-compatible base URL.",
    )
    parser.add_argument(
        "--provider-timeout-sec",
        type=int,
        default=60,
        help="Timeout seconds for provider HTTP requests.",
    )
    parser.add_argument(
        "--require-llm",
        action="store_true",
        help="Fail fast if remote provider is unavailable (no heuristic fallback).",
    )
    parser.add_argument(
        "--peer-wait-seconds",
        type=float,
        default=4.0,
        help="Max wait time for each peer challenge round replies.",
    )
    parser.add_argument(
        "--evidence-wait-seconds",
        type=float,
        default=4.0,
        help="Max wait time for supplemental evidence replies.",
    )
    parser.add_argument(
        "--auto-round3-on-challenge",
        dest="auto_round3_on_challenge",
        action="store_true",
        help="Trigger a third debate round when provisional verdict is challenge/reject.",
    )
    parser.add_argument(
        "--no-auto-round3-on-challenge",
        dest="auto_round3_on_challenge",
        action="store_false",
        help="Disable automatic third debate round.",
    )
    parser.add_argument(
        "--dynamic-tasks",
        dest="enable_dynamic_tasks",
        action="store_true",
        help="Enable runtime insertion of follow-up tasks based on intermediate findings.",
    )
    parser.add_argument(
        "--no-dynamic-tasks",
        dest="enable_dynamic_tasks",
        action="store_false",
        help="Disable runtime insertion of follow-up tasks.",
    )
    parser.add_argument(
        "--teammate-provider-replies",
        dest="teammate_provider_replies",
        action="store_true",
        help="Generate teammate peer/evidence replies through the configured provider.",
    )
    parser.add_argument(
        "--no-teammate-provider-replies",
        dest="teammate_provider_replies",
        action="store_false",
        help="Use deterministic local teammate reply templates.",
    )
    parser.add_argument(
        "--teammate-memory-turns",
        type=int,
        default=4,
        help="Number of recent teammate local memory turns to include in provider prompts.",
    )
    parser.add_argument(
        "--tmux-worker-timeout-sec",
        type=int,
        default=120,
        help="Timeout seconds for tmux/subprocess analyst task workers.",
    )
    parser.add_argument(
        "--tmux-fallback-on-error",
        dest="tmux_fallback_on_error",
        action="store_true",
        help="Fallback to subprocess worker when tmux worker execution fails.",
    )
    parser.add_argument(
        "--no-tmux-fallback-on-error",
        dest="tmux_fallback_on_error",
        action="store_false",
        help="Do not fallback to subprocess when tmux worker execution fails.",
    )
    parser.set_defaults(
        auto_round3_on_challenge=True,
        enable_dynamic_tasks=True,
        teammate_provider_replies=False,
        tmux_fallback_on_error=True,
        teammate_plan_required=False,
    )
    parser.add_argument(
        "--adjudication-accept-threshold",
        type=int,
        default=75,
        help="Score threshold for accept verdict.",
    )
    parser.add_argument(
        "--adjudication-challenge-threshold",
        type=int,
        default=50,
        help="Score threshold for challenge verdict.",
    )
    parser.add_argument(
        "--adjudication-weight-completeness",
        type=float,
        default=0.45,
        help="Weight for round1 completeness metric.",
    )
    parser.add_argument(
        "--adjudication-weight-rebuttal-coverage",
        type=float,
        default=0.35,
        help="Weight for rebuttal coverage metric.",
    )
    parser.add_argument(
        "--adjudication-weight-argument-depth",
        type=float,
        default=0.20,
        help="Weight for argument depth metric.",
    )
    parser.add_argument(
        "--re-adjudication-max-bonus",
        type=int,
        default=15,
        help="Maximum score bonus applied during lead re-adjudication.",
    )
    parser.add_argument(
        "--re-adjudication-weight-coverage",
        type=float,
        default=0.60,
        help="Weight for evidence coverage in re-adjudication bonus.",
    )
    parser.add_argument(
        "--re-adjudication-weight-depth",
        type=float,
        default=0.40,
        help="Weight for evidence depth in re-adjudication bonus.",
    )
    parser.add_argument(
        "--worker-task-file",
        default="",
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--host-session-worker-file",
        default="",
        help=argparse.SUPPRESS,
    )
    return parser.parse_args()


def build_runtime_config_from_args(args: argparse.Namespace) -> RuntimeConfig:
    if args.rewind_to_history_index < -1:
        raise ValueError("--rewind-to-history-index must be >= -1")
    if args.rewind_to_event_index < -1:
        raise ValueError("--rewind-to-event-index must be >= -1")
    if args.history_replay_start_index < -1:
        raise ValueError("--history-replay-start-index must be >= -1")
    if args.history_replay_end_index < -1:
        raise ValueError("--history-replay-end-index must be >= -1")
    if args.event_replay_max_transitions <= 0:
        raise ValueError("--event-replay-max-transitions must be > 0")
    if args.rewind_to_history_index >= 0 and args.rewind_to_event_index >= 0:
        raise ValueError("--rewind-to-history-index and --rewind-to-event-index are mutually exclusive")
    has_any_rewind = args.rewind_to_history_index >= 0 or args.rewind_to_event_index >= 0
    if args.rewind_branch and not has_any_rewind:
        raise ValueError(
            "--rewind-branch requires --rewind-to-history-index >= 0 or --rewind-to-event-index >= 0"
        )
    if args.rewind_branch_output and not has_any_rewind:
        raise ValueError(
            "--rewind-branch-output requires --rewind-to-history-index >= 0 or --rewind-to-event-index >= 0"
        )
    if args.max_completed_tasks < 0:
        raise ValueError("--max-completed-tasks must be >= 0")
    if args.lead_command_wait_seconds < 0:
        raise ValueError("--lead-command-wait-seconds must be >= 0")
    if args.peer_wait_seconds <= 0:
        raise ValueError("--peer-wait-seconds must be > 0")
    if args.evidence_wait_seconds <= 0:
        raise ValueError("--evidence-wait-seconds must be > 0")
    if not (0 <= args.adjudication_challenge_threshold <= 100):
        raise ValueError("--adjudication-challenge-threshold must be in [0, 100]")
    if not (0 <= args.adjudication_accept_threshold <= 100):
        raise ValueError("--adjudication-accept-threshold must be in [0, 100]")
    if args.adjudication_accept_threshold <= args.adjudication_challenge_threshold:
        raise ValueError("--adjudication-accept-threshold must be greater than challenge threshold")

    weight_values = [
        args.adjudication_weight_completeness,
        args.adjudication_weight_rebuttal_coverage,
        args.adjudication_weight_argument_depth,
    ]
    if any(weight < 0 for weight in weight_values):
        raise ValueError("adjudication weights must be >= 0")
    if sum(weight_values) <= 0:
        raise ValueError("sum of adjudication weights must be > 0")
    if args.re_adjudication_max_bonus < 0:
        raise ValueError("--re-adjudication-max-bonus must be >= 0")
    if args.teammate_memory_turns <= 0:
        raise ValueError("--teammate-memory-turns must be > 0")
    if args.tmux_worker_timeout_sec <= 0:
        raise ValueError("--tmux-worker-timeout-sec must be > 0")
    re_weight_values = [
        args.re_adjudication_weight_coverage,
        args.re_adjudication_weight_depth,
    ]
    if any(weight < 0 for weight in re_weight_values):
        raise ValueError("re-adjudication weights must be >= 0")
    if sum(re_weight_values) <= 0:
        raise ValueError("sum of re-adjudication weights must be > 0")

    runtime_config = RuntimeConfig(
        teammate_mode=str(args.teammate_mode),
        enable_dynamic_tasks=bool(args.enable_dynamic_tasks),
        teammate_provider_replies=bool(args.teammate_provider_replies),
        teammate_memory_turns=args.teammate_memory_turns,
        tmux_worker_timeout_sec=args.tmux_worker_timeout_sec,
        tmux_fallback_on_error=bool(args.tmux_fallback_on_error),
        peer_wait_seconds=args.peer_wait_seconds,
        evidence_wait_seconds=args.evidence_wait_seconds,
        auto_round3_on_challenge=bool(args.auto_round3_on_challenge),
        adjudication_accept_threshold=args.adjudication_accept_threshold,
        adjudication_challenge_threshold=args.adjudication_challenge_threshold,
        adjudication_weight_completeness=args.adjudication_weight_completeness,
        adjudication_weight_rebuttal_coverage=args.adjudication_weight_rebuttal_coverage,
        adjudication_weight_argument_depth=args.adjudication_weight_argument_depth,
        re_adjudication_max_bonus=args.re_adjudication_max_bonus,
        re_adjudication_weight_coverage=args.re_adjudication_weight_coverage,
        re_adjudication_weight_depth=args.re_adjudication_weight_depth,
    )
    validate_runtime_config(runtime_config)
    return runtime_config


def validate_runtime_config(runtime_config: RuntimeConfig) -> None:
    if runtime_config.peer_wait_seconds <= 0:
        raise ValueError("--peer-wait-seconds must be > 0")
    if runtime_config.evidence_wait_seconds <= 0:
        raise ValueError("--evidence-wait-seconds must be > 0")
    if not (0 <= runtime_config.adjudication_challenge_threshold <= 100):
        raise ValueError("--adjudication-challenge-threshold must be in [0, 100]")
    if not (0 <= runtime_config.adjudication_accept_threshold <= 100):
        raise ValueError("--adjudication-accept-threshold must be in [0, 100]")
    if runtime_config.adjudication_accept_threshold <= runtime_config.adjudication_challenge_threshold:
        raise ValueError("--adjudication-accept-threshold must be greater than challenge threshold")
    weight_values = [
        runtime_config.adjudication_weight_completeness,
        runtime_config.adjudication_weight_rebuttal_coverage,
        runtime_config.adjudication_weight_argument_depth,
    ]
    if any(weight < 0 for weight in weight_values):
        raise ValueError("adjudication weights must be >= 0")
    if sum(weight_values) <= 0:
        raise ValueError("sum of adjudication weights must be > 0")
    if runtime_config.re_adjudication_max_bonus < 0:
        raise ValueError("--re-adjudication-max-bonus must be >= 0")
    if runtime_config.teammate_memory_turns <= 0:
        raise ValueError("--teammate-memory-turns must be > 0")
    if runtime_config.tmux_worker_timeout_sec <= 0:
        raise ValueError("--tmux-worker-timeout-sec must be > 0")
    re_weight_values = [
        runtime_config.re_adjudication_weight_coverage,
        runtime_config.re_adjudication_weight_depth,
    ]
    if any(weight < 0 for weight in re_weight_values):
        raise ValueError("re-adjudication weights must be >= 0")
    if sum(re_weight_values) <= 0:
        raise ValueError("sum of re-adjudication weights must be > 0")


def runtime_config_from_checkpoint_payload(payload: Dict[str, Any]) -> Optional[RuntimeConfig]:
    runtime_payload = payload.get("runtime_config", {})
    if not isinstance(runtime_payload, dict) or not runtime_payload:
        return None
    return RuntimeConfig(
        **{
            key: value
            for key, value in runtime_payload.items()
            if key in RuntimeConfig.__annotations__
        }
    )


def apply_resume_runtime_defaults(
    agent_team_config: AgentTeamConfig,
    resume_from: Optional[pathlib.Path],
) -> AgentTeamConfig:
    if resume_from is None:
        return agent_team_config
    resume_payload = load_checkpoint(resume_from)
    checkpoint_runtime = runtime_config_from_checkpoint_payload(resume_payload)
    if checkpoint_runtime is None:
        return agent_team_config
    default_runtime = RuntimeConfig()
    runtime_overrides = {
        field: getattr(agent_team_config.runtime, field)
        for field in RuntimeConfig.__annotations__
        if getattr(agent_team_config.runtime, field) != getattr(default_runtime, field)
    }
    effective_runtime = dataclasses.replace(checkpoint_runtime, **runtime_overrides)
    validate_runtime_config(effective_runtime)
    return dataclasses.replace(agent_team_config, runtime=effective_runtime)


def build_agent_team_config_from_args(
    args: argparse.Namespace,
    runtime_config: RuntimeConfig,
) -> AgentTeamConfig:
    policy_override_requested = bool(getattr(args, "teammate_plan_required", False))
    if args.config:
        config_path = pathlib.Path(args.config).resolve()
        loaded = load_agent_team_config(config_path)
        default_runtime = RuntimeConfig()
        runtime_overrides = {
            field: getattr(runtime_config, field)
            for field in RuntimeConfig.__annotations__
            if getattr(runtime_config, field) != getattr(default_runtime, field)
        }
        effective_runtime = dataclasses.replace(loaded.runtime, **runtime_overrides)
        validate_runtime_config(effective_runtime)

        effective_host = loaded.host
        if args.host_kind:
            effective_host = default_host_config(args.host_kind)

        effective_workflow = loaded.workflow
        if args.workflow_pack:
            effective_workflow = dataclasses.replace(effective_workflow, pack=str(args.workflow_pack))
        if args.workflow_preset:
            effective_workflow = dataclasses.replace(effective_workflow, preset=str(args.workflow_preset))

        effective_model = loaded.model
        if args.provider != "heuristic":
            effective_model = dataclasses.replace(effective_model, provider_name=str(args.provider))
        if args.model != "heuristic-v1":
            effective_model = dataclasses.replace(effective_model, model=str(args.model))
        if args.openai_api_key_env != "OPENAI_API_KEY":
            effective_model = dataclasses.replace(
                effective_model,
                openai_api_key_env=str(args.openai_api_key_env),
            )
        if args.openai_base_url != "https://api.openai.com/v1":
            effective_model = dataclasses.replace(
                effective_model,
                openai_base_url=str(args.openai_base_url),
            )
        if args.require_llm:
            effective_model = dataclasses.replace(effective_model, require_llm=True)
        if args.provider_timeout_sec != 60:
            effective_model = dataclasses.replace(
                effective_model,
                timeout_sec=int(args.provider_timeout_sec),
            )
        effective_policies = loaded.policies
        if policy_override_requested:
            effective_policies = dataclasses.replace(
                effective_policies,
                teammate_plan_required=True,
            )

        return AgentTeamConfig(
            runtime=effective_runtime,
            host=effective_host,
            model=effective_model,
            team=loaded.team,
            workflow=effective_workflow,
            policies=effective_policies,
            source_path=loaded.source_path,
        )

    built = build_agent_team_config(
        runtime_config=runtime_config,
        provider_name=str(args.provider),
        model=str(args.model),
        openai_api_key_env=str(args.openai_api_key_env),
        openai_base_url=str(args.openai_base_url),
        require_llm=bool(args.require_llm),
        provider_timeout_sec=int(args.provider_timeout_sec),
        workflow_pack=str(args.workflow_pack or "markdown-audit"),
        workflow_preset=str(args.workflow_preset or "default"),
        host_kind=str(args.host_kind or "generic-cli"),
    )
    if policy_override_requested:
        built = dataclasses.replace(
            built,
            policies=dataclasses.replace(built.policies, teammate_plan_required=True),
        )
    return built


if __name__ == "__main__":
    args = parse_args()
    if args.worker_task_file:
        raise SystemExit(run_tmux_worker_entrypoint(pathlib.Path(args.worker_task_file).resolve()))
    if args.host_session_worker_file:
        raise SystemExit(run_host_session_worker_entrypoint(pathlib.Path(args.host_session_worker_file).resolve()))
    try:
        runtime_config = build_runtime_config_from_args(args)
        approve_plan_task_ids = [str(task_id) for task_id in args.approve_plan if str(task_id)]
        reject_plan_task_ids = [str(task_id) for task_id in args.reject_plan if str(task_id)]
        conflicting_plan_ids = sorted(set(approve_plan_task_ids) & set(reject_plan_task_ids))
        if conflicting_plan_ids:
            raise ValueError(
                "--approve-plan and --reject-plan overlap for task ids: "
                + ", ".join(conflicting_plan_ids)
            )
        output_dir_path = pathlib.Path(args.output).resolve()
        if args.history_replay_report and args.event_replay_report:
            raise ValueError("--history-replay-report and --event-replay-report are mutually exclusive")
        if args.history_replay_report:
            report_path = (
                pathlib.Path(args.history_replay_report_path).resolve()
                if args.history_replay_report_path
                else default_history_replay_report_path(output_dir_path)
            )
            report_meta = write_history_replay_report(
                output_dir=output_dir_path,
                report_path=report_path,
                start_index=int(args.history_replay_start_index),
                end_index=int(args.history_replay_end_index),
            )
            print(f"[lead] history_replay_report: {report_meta['report_path']}")
            print(
                f"[lead] history_replay_range: "
                f"[{report_meta['start_index']}, {report_meta['end_index']}] "
                f"snapshots={report_meta['snapshot_count']}"
            )
            raise SystemExit(0)
        if args.event_replay_report:
            report_path = (
                pathlib.Path(args.event_replay_report_path).resolve()
                if args.event_replay_report_path
                else output_dir_path / "event_replay.md"
            )
            report_meta = write_event_replay_report(
                output_dir=output_dir_path,
                report_path=report_path,
                max_transitions=int(args.event_replay_max_transitions),
            )
            print(f"[lead] event_replay_report: {report_meta['report_path']}")
            print(
                f"[lead] event_replay_summary: "
                f"events={report_meta['event_count']} "
                f"tasks={report_meta['task_count']} "
                f"mismatches={report_meta['mismatch_count']}"
            )
            raise SystemExit(0)
        rewind_history_index: Optional[int] = None
        rewind_event_index: Optional[int] = None
        rewind_event_resolution: Optional[Dict[str, Any]] = None
        if args.resume_from and (args.rewind_to_history_index >= 0 or args.rewind_to_event_index >= 0):
            raise ValueError(
                "--resume-from is mutually exclusive with --rewind-to-history-index/--rewind-to-event-index"
            )

        resume_from_path: Optional[pathlib.Path] = None
        run_output_dir = output_dir_path
        rewind_source_output_dir: Optional[pathlib.Path] = None
        rewind_source_checkpoint: Optional[pathlib.Path] = None
        branch_run_id = ""
        if args.rewind_to_history_index >= 0:
            rewind_history_index = int(args.rewind_to_history_index)
            rewind_source_output_dir = output_dir_path
            rewind_source_checkpoint = resolve_checkpoint_by_history_index(
                output_dir=output_dir_path,
                history_index=rewind_history_index,
            )
            resume_from_path = rewind_source_checkpoint
            if args.rewind_branch or args.rewind_branch_output:
                run_output_dir = (
                    pathlib.Path(args.rewind_branch_output).resolve()
                    if args.rewind_branch_output
                    else default_rewind_branch_output_dir(
                        source_output_dir=output_dir_path,
                        history_index=rewind_history_index,
                    )
                )
                branch_run_id = f"rewind_{rewind_history_index:06d}_{utc_now()}"
        elif args.rewind_to_event_index >= 0:
            rewind_event_index = int(args.rewind_to_event_index)
            rewind_source_output_dir = output_dir_path
            rewind_event_resolution = resolve_checkpoint_by_event_index(
                output_dir=output_dir_path,
                event_index=rewind_event_index,
            )
            rewind_source_checkpoint = pathlib.Path(str(rewind_event_resolution["resolved_checkpoint"])).resolve()
            resume_from_path = rewind_source_checkpoint
            if args.rewind_branch or args.rewind_branch_output:
                run_output_dir = (
                    pathlib.Path(args.rewind_branch_output).resolve()
                    if args.rewind_branch_output
                    else default_event_rewind_branch_output_dir(
                        source_output_dir=output_dir_path,
                        event_index=rewind_event_index,
                    )
                )
                branch_run_id = f"rewind_event_{rewind_event_index:08d}_{utc_now()}"
        elif args.resume_from:
            resume_from_path = pathlib.Path(args.resume_from).resolve()
            if not resume_from_path.exists():
                raise ValueError(f"--resume-from file does not exist: {resume_from_path}")
        agent_team_config = build_agent_team_config_from_args(args=args, runtime_config=runtime_config)
        agent_team_config = apply_resume_runtime_defaults(
            agent_team_config=agent_team_config,
            resume_from=resume_from_path,
        )
        exit_code = run_team(
            goal=args.goal,
            target_dir=pathlib.Path(args.target).resolve(),
            output_dir=run_output_dir,
            runtime_config=agent_team_config.runtime,
            provider_name=agent_team_config.model.provider_name,
            model=agent_team_config.model.model,
            openai_api_key_env=agent_team_config.model.openai_api_key_env,
            openai_base_url=agent_team_config.model.openai_base_url,
            require_llm=agent_team_config.model.require_llm,
            provider_timeout_sec=agent_team_config.model.timeout_sec,
            resume_from=resume_from_path,
            max_completed_tasks=args.max_completed_tasks,
            rewind_history_index=rewind_history_index,
            rewind_event_index=rewind_event_index,
            rewind_event_resolution=rewind_event_resolution,
            rewind_source_output_dir=rewind_source_output_dir,
            rewind_source_checkpoint=rewind_source_checkpoint,
            branch_run_id=branch_run_id,
            agent_team_config=agent_team_config,
            approve_plan_task_ids=approve_plan_task_ids,
            reject_plan_task_ids=reject_plan_task_ids,
            approve_all_pending_plans=bool(args.approve_all_pending_plans),
            lead_command_wait_seconds=float(args.lead_command_wait_seconds),
        )
    except Exception as exc:
        print(f"[lead] startup_error: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(2)
    raise SystemExit(exit_code)
