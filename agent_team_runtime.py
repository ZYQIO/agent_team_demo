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
import time
import traceback
import uuid
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
from agent_team.host import build_host_adapter
from agent_team.core import (
    AgentProfile,
    EventLogger,
    FileLockRegistry,
    HOOK_EVENT_TASK_COMPLETED,
    Mailbox,
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
    build_targeted_evidence_question,
    compute_adjudication,
    compute_evidence_bonus,
    default_event_rewind_branch_output_dir,
    default_history_replay_report_path,
    default_rewind_branch_output_dir,
    derive_evidence_focus_areas,
    load_checkpoint,
    replay_task_states_from_events,
    resolve_checkpoint_by_event_index,
    resolve_checkpoint_by_history_index,
    seed_branch_events_from_source,
    write_event_replay_report,
    write_history_replay_report,
)
from agent_team.runtime.engine import (
    AgentContext,
    TaskHandler,
    build_agent_context,
    build_profiles,
    get_lead_name,
    get_team_member_names,
    get_team_profiles,
    profile_has_skill,
    run_lead_task_once as run_lead_task_once_impl,
    run_team as run_team_impl,
)
from agent_team.transports.inprocess import InProcessTeammateAgent
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
TMUX_EXTERNAL_TASK_TYPES = tmux_transport.TMUX_EXTERNAL_TASK_TYPES
TMUX_MAILBOX_BRIDGE_TASK_TYPES = {"peer_challenge", "evidence_pack"}


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
        external_task_runner: Optional[Any] = None,
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
            external_task_runner=external_task_runner,
        )


def _execute_worker_subprocess(command: List[str], timeout_sec: int) -> subprocess.CompletedProcess[str]:
    return tmux_transport.execute_worker_subprocess(command=command, timeout_sec=timeout_sec)


def _execute_worker_tmux(
    command: List[str],
    workdir: pathlib.Path,
    session_prefix: str,
    timeout_sec: int,
    retain_session_for_reuse: bool = False,
    allow_existing_session_reuse: bool = False,
) -> subprocess.CompletedProcess[str]:
    return tmux_transport.execute_worker_tmux(
        command=command,
        workdir=workdir,
        session_prefix=session_prefix,
        timeout_sec=timeout_sec,
        retain_session_for_reuse=retain_session_for_reuse,
        allow_existing_session_reuse=allow_existing_session_reuse,
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


def cleanup_tmux_worker_sessions(
    lead_context: AgentContext,
    worker_profiles: Sequence[AgentProfile],
) -> Dict[str, Any]:
    return tmux_transport.cleanup_tmux_worker_sessions(
        lead_context=lead_context,
        worker_profiles=worker_profiles,
    )


def cleanup_tmux_analyst_sessions(
    lead_context: AgentContext,
    analyst_profiles: Sequence[AgentProfile],
) -> Dict[str, Any]:
    return cleanup_tmux_worker_sessions(
        lead_context=lead_context,
        worker_profiles=analyst_profiles,
    )


def recover_tmux_worker_sessions(
    lead_context: AgentContext,
    worker_profiles: Sequence[AgentProfile],
    resume_from: Optional[pathlib.Path] = None,
) -> Dict[str, Any]:
    return tmux_transport.recover_tmux_worker_sessions(
        lead_context=lead_context,
        worker_profiles=worker_profiles,
        resume_from=resume_from,
    )


def recover_tmux_analyst_sessions(
    lead_context: AgentContext,
    analyst_profiles: Sequence[AgentProfile],
    resume_from: Optional[pathlib.Path] = None,
) -> Dict[str, Any]:
    return recover_tmux_worker_sessions(
        lead_context=lead_context,
        worker_profiles=analyst_profiles,
        resume_from=resume_from,
    )


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


def _task_results_snapshot(board: Any) -> Dict[str, Any]:
    snapshot = board.snapshot()
    results: Dict[str, Any] = {}
    for item in snapshot.get("tasks", []):
        task_id = str(item.get("task_id", ""))
        if not task_id:
            continue
        results[task_id] = item.get("result")
    return results


def _task_ids_snapshot(board: Any) -> List[str]:
    snapshot = board.snapshot()
    return [str(item.get("task_id", "")) for item in snapshot.get("tasks", []) if item.get("task_id")]


def _serve_mailbox_bridge(
    context: AgentContext,
    requests_dir: pathlib.Path,
    responses_dir: pathlib.Path,
    stop_event: threading.Event,
    poll_interval_sec: float = 0.05,
) -> None:
    requests_dir.mkdir(parents=True, exist_ok=True)
    responses_dir.mkdir(parents=True, exist_ok=True)
    while not stop_event.is_set() or any(requests_dir.glob("*.json")):
        request_paths = sorted(requests_dir.glob("*.json"))
        if not request_paths:
            time.sleep(poll_interval_sec)
            continue
        for request_path in request_paths:
            try:
                payload = json.loads(request_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(payload, dict):
                request_path.unlink(missing_ok=True)
                continue
            request_id = str(payload.get("request_id", "") or "")
            op = str(payload.get("op", "") or "")
            request_payload = payload.get("payload", {})
            if not request_id or not isinstance(request_payload, dict):
                request_path.unlink(missing_ok=True)
                continue
            response_path = responses_dir / f"{request_id}.json"
            response: Dict[str, Any] = {"ok": True, "payload": {}}
            try:
                if op == "send":
                    context.mailbox.send(
                        sender=str(request_payload.get("sender", "")),
                        recipient=str(request_payload.get("recipient", "")),
                        subject=str(request_payload.get("subject", "")),
                        body=str(request_payload.get("body", "")),
                        task_id=request_payload.get("task_id"),
                    )
                elif op == "broadcast":
                    context.mailbox.broadcast(
                        sender=str(request_payload.get("sender", "")),
                        subject=str(request_payload.get("subject", "")),
                        body=str(request_payload.get("body", "")),
                    )
                elif op == "pull":
                    messages = context.mailbox.pull(str(request_payload.get("recipient", "")))
                    response["payload"] = {
                        "messages": [message.to_dict() for message in messages],
                    }
                else:
                    raise ValueError(f"unsupported mailbox bridge op: {op}")
                context.logger.log(
                    "tmux_mailbox_bridge_request_served",
                    worker=context.profile.name,
                    op=op,
                )
            except Exception as exc:
                response = {
                    "ok": False,
                    "error": f"{type(exc).__name__}: {exc}",
                }
            response_path.write_text(json.dumps(response, ensure_ascii=False), encoding="utf-8")
            request_path.unlink(missing_ok=True)


def run_external_teammate_task(context: AgentContext, task: Task) -> Optional[Dict[str, Any]]:
    if context.runtime_config.teammate_mode != "tmux":
        return None
    if task.task_type not in TMUX_EXTERNAL_TASK_TYPES:
        return None

    workflow = context.shared_state.get("workflow", {})
    if not isinstance(workflow, dict):
        workflow = {}
    agent_team_config = context.shared_state.get("agent_team_config", {})
    if not isinstance(agent_team_config, dict):
        agent_team_config = {}
    model_config = agent_team_config.get("model", {})
    if not isinstance(model_config, dict):
        model_config = {}

    retain_session_for_reuse = False
    allow_existing_session_reuse = False
    if task.task_type in TMUX_EXTERNAL_TASK_TYPES:
        board_snapshot = context.board.snapshot()
        retain_session_for_reuse = any(
            str(item.get("task_id", "")) != task.task_id
            and str(item.get("task_type", "")) in TMUX_EXTERNAL_TASK_TYPES
            and str(item.get("status", "")) in {"pending", "blocked"}
            and (
                not item.get("allowed_agent_types")
                or context.profile.agent_type in set(item.get("allowed_agent_types", []))
            )
            for item in board_snapshot.get("tasks", [])
        )
        allow_existing_session_reuse = tmux_transport._lease_allows_preferred_session_reuse(
            shared_state=context.shared_state,
            worker_name=context.profile.name,
        )

    context.logger.log(
        "tmux_worker_task_dispatched",
        worker=context.profile.name,
        task_id=task.task_id,
        task_type=task.task_type,
        retain_session_for_reuse=retain_session_for_reuse,
        allow_existing_session_reuse=allow_existing_session_reuse,
    )
    bridge_stop_event: Optional[threading.Event] = None
    bridge_thread: Optional[threading.Thread] = None
    mailbox_bridge_payload: Dict[str, Any] = {}
    if task.task_type in TMUX_MAILBOX_BRIDGE_TASK_TYPES:
        bridge_root = (
            context.output_dir
            / "_tmux_worker_mailbox"
            / f"{context.profile.name}_{task.task_id}_{uuid.uuid4().hex}"
        )
        requests_dir = bridge_root / "requests"
        responses_dir = bridge_root / "responses"
        mailbox_bridge_payload = {
            "requests_dir": str(requests_dir.resolve()),
            "responses_dir": str(responses_dir.resolve()),
        }
        bridge_stop_event = threading.Event()
        bridge_thread = threading.Thread(
            target=_serve_mailbox_bridge,
            kwargs={
                "context": context,
                "requests_dir": requests_dir,
                "responses_dir": responses_dir,
                "stop_event": bridge_stop_event,
            },
            daemon=True,
        )
        bridge_thread.start()
    execution = _run_tmux_worker_task(
        runtime_script=pathlib.Path(__file__).resolve(),
        output_dir=context.output_dir,
        runtime_config=context.runtime_config,
        payload={
            "task_id": task.task_id,
            "task_title": task.title,
            "task_type": task.task_type,
            "task_payload": dict(task.payload),
            "task_required_skills": sorted(task.required_skills),
            "task_dependencies": list(task.dependencies),
            "task_locked_paths": list(task.locked_paths),
            "task_allowed_agent_types": sorted(task.allowed_agent_types),
            "workflow_pack": str(workflow.get("pack", "markdown-audit")),
            "goal": context.goal,
            "target_dir": str(context.target_dir),
            "output_dir": str(context.output_dir),
            "shared_state": context.shared_state.snapshot(),
            "runtime_config": context.runtime_config.to_dict(),
            "profile": context.profile.to_dict(),
            "provider_config": dict(model_config),
            "task_results": _task_results_snapshot(context.board),
            "task_ids": _task_ids_snapshot(context.board),
            "mailbox_bridge": mailbox_bridge_payload,
        },
        worker_name=context.profile.name,
        logger=context.logger,
        timeout_sec=context.runtime_config.tmux_worker_timeout_sec,
        retain_session_for_reuse=retain_session_for_reuse,
        allow_existing_session_reuse=allow_existing_session_reuse,
    )
    if bridge_stop_event is not None:
        bridge_stop_event.set()
    if bridge_thread is not None:
        bridge_thread.join(timeout=2.0)
    execution_diagnostics = execution.get("diagnostics", {})
    transport = str(execution.get("transport", ""))

    if execution.get("ok"):
        retained_for_reuse = bool(execution_diagnostics.get("tmux_session_retained_for_reuse", False))
        lease_status = "retained" if retained_for_reuse else "released"
        if "subprocess" in transport:
            lease_status = "fallback_subprocess"
        tmux_transport._update_tmux_session_lease(
            lead_context=context,
            worker_name=context.profile.name,
            session_name=str(
                execution_diagnostics.get("tmux_preferred_session_name", "")
                or tmux_transport.preferred_tmux_session_name(context.profile.name)
            ),
            status=lease_status,
            task_id=task.task_id,
            task_type=task.task_type,
            transport=transport,
            cleanup_result=str(execution_diagnostics.get("tmux_cleanup_result", "")),
            retained_for_reuse=retained_for_reuse,
            reused_existing=bool(
                execution_diagnostics.get("tmux_preferred_session_reused_existing", False)
            ),
            reuse_authorized=allow_existing_session_reuse,
        )
    else:
        lease_status = "failed"
        if "subprocess" in transport:
            lease_status = "fallback_subprocess"
        tmux_transport._update_tmux_session_lease(
            lead_context=context,
            worker_name=context.profile.name,
            session_name=str(
                execution_diagnostics.get("tmux_preferred_session_name", "")
                or tmux_transport.preferred_tmux_session_name(context.profile.name)
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
            error=str(execution.get("error", "")),
        )

    event_name = "tmux_worker_task_completed" if execution.get("ok") else "tmux_worker_task_failed"
    context.logger.log(
        event_name,
        worker=context.profile.name,
        task_id=task.task_id,
        transport=transport,
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
        error=str(execution.get("error", "")) if not execution.get("ok") else "",
    )
    if not execution.get("ok"):
        return {
            "ok": False,
            "error": str(execution.get("error", "unknown worker error")),
        }

    worker_payload = execution.get("payload", {})
    result = worker_payload.get("result", {})
    if not isinstance(result, dict):
        result = {"raw_result": result}
    state_updates = worker_payload.get("state_updates", {})
    if not isinstance(state_updates, dict):
        state_updates = {}
    board_mutations = worker_payload.get("board_mutations", {})
    if not isinstance(board_mutations, dict):
        board_mutations = {}
    return {
        "ok": True,
        "result": result,
        "state_updates": state_updates,
        "board_mutations": board_mutations,
        "transport": transport,
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
        teammate_agent_factory=TeammateAgent,
        external_teammate_task_runner=run_external_teammate_task,
        run_tmux_analyst_task_once_fn=run_tmux_analyst_task_once,
        recover_tmux_analyst_sessions_fn=recover_tmux_worker_sessions,
        cleanup_tmux_analyst_sessions_fn=cleanup_tmux_worker_sessions,
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
        choices=["in-process", "tmux"],
        help="Teammate execution mode. `tmux` uses process workers for analyst tasks.",
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

        return AgentTeamConfig(
            runtime=effective_runtime,
            host=effective_host,
            model=effective_model,
            team=loaded.team,
            workflow=effective_workflow,
            policies=loaded.policies,
            source_path=loaded.source_path,
        )

    return build_agent_team_config(
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


if __name__ == "__main__":
    args = parse_args()
    if args.worker_task_file:
        raise SystemExit(run_tmux_worker_entrypoint(pathlib.Path(args.worker_task_file).resolve()))
    try:
        runtime_config = build_runtime_config_from_args(args)
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
        )
    except Exception as exc:
        print(f"[lead] startup_error: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(2)
    raise SystemExit(exit_code)
