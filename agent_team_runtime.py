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
import datetime as dt
import json
import pathlib
import shlex
import shutil
import subprocess
import sys
import threading
import time
import traceback
import uuid
from typing import Any, Callable, Dict, List, Optional, Sequence

from agent_team.config import (
    AgentTeamConfig,
    RuntimeConfig,
    TeamAgentConfig,
    TeamConfig,
    build_agent_team_config,
    default_host_config,
    default_team_config,
    load_agent_team_config,
)
from agent_team.core import (
    ACTIVE_TASK_STATES,
    HOOK_EVENT_TASK_COMPLETED,
    HOOK_EVENT_TEAMMATE_IDLE,
    TEAMMATE_IDLE_HOOK_INTERVAL_SEC,
    TERMINAL_TASK_STATES,
    AgentProfile,
    EventLogger,
    FileLockRegistry,
    Mailbox,
    Message,
    SharedState,
    Task,
    TaskBoard,
    task_from_dict,
    utc_now,
)
from agent_team.host import build_host_adapter
from agent_team.models import LLMProvider, ProviderMetadata, build_provider
from agent_team.workflows import build_workflow_tasks


TMUX_ANALYST_TASK_TYPES = {
    "discover_markdown",
    "heading_audit",
    "length_audit",
    "heading_structure_followup",
    "length_risk_followup",
}
CHECKPOINT_VERSION = 1
CHECKPOINT_FILENAME = "run_checkpoint.json"
CHECKPOINT_HISTORY_DIRNAME = "_checkpoint_history"


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


TaskHandler = Callable[[AgentContext, Task], Dict[str, Any]]


def compute_adjudication(peer_challenge: Dict[str, Any], config: RuntimeConfig) -> Dict[str, Any]:
    targets = list(peer_challenge.get("targets", []))
    target_count = max(1, len(targets))
    round1 = peer_challenge.get("round1", {})
    round2 = peer_challenge.get("round2", {})
    round3 = peer_challenge.get("round3", {})

    round1_replies = round1.get("received_replies", {})
    round2_replies = round2.get("received_replies", {})
    round3_replies = round3.get("received_replies", {})

    completeness = len(round1_replies) / target_count
    round2_coverage = len(round2_replies) / target_count
    round3_coverage = len(round3_replies) / target_count if round3 else 0.0
    rebuttal_coverage = (
        (round2_coverage + round3_coverage) / 2.0 if round3 else round2_coverage
    )

    depth_pool: List[str] = list(round2_replies.values())
    if round3:
        depth_pool.extend(round3_replies.values())
    avg_rebuttal_len = sum(len(reply) for reply in depth_pool) / max(1, len(depth_pool))
    argument_depth = min(avg_rebuttal_len / 180.0, 1.0)

    w1 = max(0.0, float(config.adjudication_weight_completeness))
    w2 = max(0.0, float(config.adjudication_weight_rebuttal_coverage))
    w3 = max(0.0, float(config.adjudication_weight_argument_depth))
    weight_sum = max(1e-9, w1 + w2 + w3)
    nw1 = w1 / weight_sum
    nw2 = w2 / weight_sum
    nw3 = w3 / weight_sum

    score = int(round((nw1 * completeness + nw2 * rebuttal_coverage + nw3 * argument_depth) * 100))
    accept_threshold = int(config.adjudication_accept_threshold)
    challenge_threshold = min(int(config.adjudication_challenge_threshold), accept_threshold - 1)

    if score >= accept_threshold:
        verdict = "accept"
        rationale = "Debate evidence and rebuttal quality meet the configured acceptance bar."
    elif score >= challenge_threshold:
        verdict = "challenge"
        rationale = "Debate is partially sufficient; run an additional iteration before accepting."
    else:
        verdict = "reject"
        rationale = "Debate quality is below threshold; recommendations are not ready."

    return {
        "verdict": verdict,
        "score": score,
        "rubric": {
            "completeness": round(completeness, 3),
            "rebuttal_coverage": round(rebuttal_coverage, 3),
            "argument_depth": round(argument_depth, 3),
            "round2_coverage": round(round2_coverage, 3),
            "round3_coverage": round(round3_coverage, 3),
        },
        "thresholds": {
            "accept": accept_threshold,
            "challenge": challenge_threshold,
        },
        "weights": {
            "completeness": round(nw1, 3),
            "rebuttal_coverage": round(nw2, 3),
            "argument_depth": round(nw3, 3),
        },
        "rationale": rationale,
        "targets": targets,
    }


def compute_evidence_bonus(evidence_pack: Dict[str, Any], config: RuntimeConfig) -> Dict[str, Any]:
    targets = list(evidence_pack.get("targets", []))
    replies = evidence_pack.get("received_replies", {})
    target_count = max(1, len(targets))
    coverage = len(replies) / target_count
    avg_len = sum(len(reply) for reply in replies.values()) / max(1, len(replies))
    depth = min(avg_len / 220.0, 1.0)

    w_cov = max(0.0, float(config.re_adjudication_weight_coverage))
    w_depth = max(0.0, float(config.re_adjudication_weight_depth))
    weight_sum = max(1e-9, w_cov + w_depth)
    nw_cov = w_cov / weight_sum
    nw_depth = w_depth / weight_sum

    max_bonus = max(0, int(config.re_adjudication_max_bonus))
    bonus_ratio = nw_cov * coverage + nw_depth * depth
    bonus = int(round(bonus_ratio * max_bonus))
    return {
        "bonus": bonus,
        "metrics": {
            "coverage": round(coverage, 3),
            "depth": round(depth, 3),
            "avg_length": round(avg_len, 1),
        },
        "weights": {
            "coverage": round(nw_cov, 3),
            "depth": round(nw_depth, 3),
        },
        "max_bonus": max_bonus,
    }


def derive_evidence_focus_areas(adjudication: Dict[str, Any]) -> List[str]:
    rubric = adjudication.get("rubric", {})
    completeness = float(rubric.get("completeness", 0.0))
    rebuttal_coverage = float(rubric.get("rebuttal_coverage", 0.0))
    argument_depth = float(rubric.get("argument_depth", 0.0))

    focus: List[str] = []
    if completeness < 1.0:
        focus.append("coverage")
    if rebuttal_coverage < 0.85:
        focus.append("rebuttal")
    if argument_depth < 0.75:
        focus.append("depth")
    return focus or ["depth"]


def get_latest_agent_reply(peer_challenge: Dict[str, Any], agent_name: str) -> str:
    for round_key in ("round3", "round2", "round1"):
        round_data = peer_challenge.get(round_key, {})
        received = round_data.get("received_replies", {})
        if agent_name in received:
            return str(received[agent_name])
    return ""


def build_targeted_evidence_question(
    focus_areas: List[str],
    peer_name: str,
    peer_objection: str,
) -> str:
    ask_lines: List[str] = []
    ask_lines.append("Lead supplemental evidence request.")
    ask_lines.append(f"Focus areas: {', '.join(focus_areas)}")
    ask_lines.append("Deliverables:")
    if "coverage" in focus_areas:
        ask_lines.append("- Provide explicit acceptance checks with measurable thresholds.")
    if "rebuttal" in focus_areas:
        ask_lines.append("- Address the peer objection directly and state why your approach still holds.")
    if "depth" in focus_areas:
        ask_lines.append("- Provide phased rollout + rollback criteria with concrete metrics.")
    if peer_name:
        ask_lines.append(f"Peer to address: {peer_name}")
    if peer_objection:
        ask_lines.append(f"Peer objection snippet: {peer_objection[:220]}")
    return "\n".join(ask_lines)


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
    lead_name = str(context.shared_state.get("lead_name", "lead") or "lead")
    return lead_name


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

    inventory = shared_state.get("markdown_inventory", [])
    if not isinstance(inventory, list):
        inventory = []

    if task_type == "heading_audit":
        return _worker_heading_audit(inventory=inventory)
    if task_type == "length_audit":
        threshold = int(task_payload.get("line_threshold", 200))
        return _worker_length_audit(inventory=inventory, threshold=threshold)
    if task_type == "heading_structure_followup":
        top_n = int(task_payload.get("top_n", 8))
        return _worker_heading_followup(inventory=inventory, top_n=top_n)
    if task_type == "length_risk_followup":
        top_n = int(task_payload.get("top_n", 8))
        threshold = int(task_payload.get("line_threshold", 180))
        return _worker_length_followup(inventory=inventory, threshold=threshold, top_n=top_n)

    raise ValueError(f"unsupported tmux worker task type: {task_type}")


def run_tmux_worker_entrypoint(task_file: pathlib.Path) -> int:
    try:
        payload = json.loads(task_file.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("worker payload must be a JSON object")
        result = run_tmux_worker_payload(payload)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except Exception as exc:
        print(f"worker_error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


def handle_discover_markdown(context: AgentContext, _task: Task) -> Dict[str, Any]:
    time.sleep(0.2)
    inventory: List[Dict[str, Any]] = []
    ignore_prefix = str(context.output_dir.resolve())
    for path in sorted(p for p in context.target_dir.rglob("*.md") if p.is_file()):
        absolute = str(path.resolve())
        if absolute.startswith(ignore_prefix):
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        lines = text.splitlines()
        headings = sum(1 for line in lines if line.lstrip().startswith("#"))
        inventory.append(
            {
                "path": str(path.relative_to(context.target_dir)),
                "line_count": len(lines),
                "heading_count": headings,
            }
        )
    context.shared_state.set("markdown_inventory", inventory)
    return {"markdown_files": len(inventory), "sample": inventory[:3]}


def handle_heading_audit(context: AgentContext, _task: Task) -> Dict[str, Any]:
    time.sleep(0.5)
    inventory = context.shared_state.get("markdown_inventory", [])
    missing = [item for item in inventory if item["heading_count"] == 0]
    context.shared_state.set("heading_issues", missing)
    return {
        "files_without_headings": len(missing),
        "examples": [item["path"] for item in missing[:10]],
    }


def handle_length_audit(context: AgentContext, task: Task) -> Dict[str, Any]:
    time.sleep(0.5)
    inventory = context.shared_state.get("markdown_inventory", [])
    threshold = int(task.payload.get("line_threshold", 200))
    long_files = [item for item in inventory if item["line_count"] >= threshold]
    context.shared_state.set("length_issues", long_files)
    return {
        "line_threshold": threshold,
        "long_files": len(long_files),
        "examples": [item["path"] for item in long_files[:10]],
    }


def handle_dynamic_planning(context: AgentContext, _task: Task) -> Dict[str, Any]:
    heading_issues = context.shared_state.get("heading_issues", [])
    length_issues = context.shared_state.get("length_issues", [])

    if not context.runtime_config.enable_dynamic_tasks:
        result = {
            "enabled": False,
            "reason": "dynamic task insertion disabled by runtime config",
            "inserted_tasks": [],
            "peer_challenge_dependencies_added": [],
        }
        context.shared_state.set("dynamic_plan", result)
        return result

    candidate_tasks: List[Task] = []
    if heading_issues and not context.board.has_task("heading_structure_followup"):
        candidate_tasks.append(
            Task(
                task_id="heading_structure_followup",
                title="Run heading structure follow-up audit",
                task_type="heading_structure_followup",
                required_skills={"analysis"},
                dependencies=["dynamic_planning"],
                payload={"top_n": 8},
                locked_paths=[],
                allowed_agent_types={"analyst"},
            )
        )
    if length_issues and not context.board.has_task("length_risk_followup"):
        candidate_tasks.append(
            Task(
                task_id="length_risk_followup",
                title="Run length risk follow-up audit",
                task_type="length_risk_followup",
                required_skills={"analysis"},
                dependencies=["dynamic_planning"],
                payload={"line_threshold": 180, "top_n": 8},
                locked_paths=[],
                allowed_agent_types={"analyst"},
            )
        )

    inserted_tasks = context.board.add_tasks(tasks=candidate_tasks, inserted_by=context.profile.name)
    peer_gate_dependencies: List[str] = []
    for inserted_id in inserted_tasks:
        if context.board.add_dependency(
            task_id="peer_challenge",
            dependency_id=inserted_id,
            updated_by=context.profile.name,
        ):
            peer_gate_dependencies.append(inserted_id)

    result = {
        "enabled": True,
        "inserted_tasks": inserted_tasks,
        "peer_challenge_dependencies_added": peer_gate_dependencies,
        "heading_issue_count": len(heading_issues),
        "length_issue_count": len(length_issues),
    }
    context.shared_state.set("dynamic_plan", result)
    return result


def handle_heading_structure_followup(context: AgentContext, task: Task) -> Dict[str, Any]:
    time.sleep(0.3)
    top_n = int(task.payload.get("top_n", 8))
    inventory = context.shared_state.get("markdown_inventory", [])

    scored: List[Dict[str, Any]] = []
    for item in inventory:
        line_count = max(1, int(item.get("line_count", 0)))
        heading_count = int(item.get("heading_count", 0))
        density = round(heading_count / line_count, 4)
        scored.append(
            {
                "path": item.get("path", ""),
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
    context.shared_state.set("heading_followup", result)
    return result


def handle_length_risk_followup(context: AgentContext, task: Task) -> Dict[str, Any]:
    time.sleep(0.3)
    top_n = int(task.payload.get("top_n", 8))
    threshold = int(task.payload.get("line_threshold", 180))
    inventory = context.shared_state.get("markdown_inventory", [])

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
                "path": item.get("path", ""),
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
    context.shared_state.set("length_followup", result)
    return result


def handle_peer_challenge(context: AgentContext, task: Task) -> Dict[str, Any]:
    question_round1 = (
        "Identify one weak assumption in the current markdown audits and propose one concrete fix."
    )
    question_round2 = "Critique the other analyst's proposal and suggest one improvement."
    request_targets = get_team_member_names(context=context, agent_type="analyst")
    if not request_targets:
        request_targets = get_team_member_names(
            context=context,
            exclude=[context.profile.name],
        )
    timeout_sec = float(task.payload.get("wait_seconds", context.runtime_config.peer_wait_seconds))

    def collect_replies(expected_subject: str, max_wait_sec: float) -> Dict[str, str]:
        deadline = time.time() + max_wait_sec
        replies: Dict[str, str] = {}
        while time.time() < deadline and len(replies) < len(request_targets):
            incoming = context.mailbox.pull_matching(
                context.profile.name,
                lambda m: m.subject == expected_subject and m.task_id == task.task_id,
            )
            for message in incoming:
                replies[message.sender] = message.body
                context.logger.log(
                    "peer_challenge_reply_received",
                    requester=context.profile.name,
                    from_agent=message.sender,
                    task_id=task.task_id,
                    round_subject=expected_subject,
                )
            if len(replies) == len(request_targets):
                break
            time.sleep(0.1)
        return replies

    round1_payload = {
        "round": 1,
        "question": question_round1,
        "requested_by": context.profile.name,
        "requested_at": utc_now(),
    }
    for recipient in request_targets:
        context.mailbox.send(
            sender=context.profile.name,
            recipient=recipient,
            subject="peer_challenge_round1_request",
            body=json.dumps(round1_payload, ensure_ascii=False),
            task_id=task.task_id,
        )
    context.logger.log(
        "peer_challenge_round_started",
        requester=context.profile.name,
        targets=request_targets,
        task_id=task.task_id,
        round=1,
    )
    round1_replies = collect_replies("peer_challenge_round1_reply", timeout_sec)

    round2_payload_template = {
        "round": 2,
        "question": question_round2,
        "requested_by": context.profile.name,
        "requested_at": utc_now(),
    }
    for recipient in request_targets:
        peers = [name for name in request_targets if name != recipient]
        peer_name = peers[0] if peers else ""
        payload = dict(round2_payload_template)
        payload["peer_name"] = peer_name
        payload["peer_round1_reply"] = round1_replies.get(peer_name, "")
        context.mailbox.send(
            sender=context.profile.name,
            recipient=recipient,
            subject="peer_challenge_round2_request",
            body=json.dumps(payload, ensure_ascii=False),
            task_id=task.task_id,
        )
    context.logger.log(
        "peer_challenge_round_started",
        requester=context.profile.name,
        targets=request_targets,
        task_id=task.task_id,
        round=2,
    )
    round2_replies = collect_replies("peer_challenge_round2_reply", timeout_sec)

    missing_round1 = [name for name in request_targets if name not in round1_replies]
    missing_round2 = [name for name in request_targets if name not in round2_replies]
    record: Dict[str, Any] = {
        "targets": request_targets,
        "round1": {
            "question": question_round1,
            "received_replies": round1_replies,
            "missing_replies": missing_round1,
        },
        "round2": {
            "question": question_round2,
            "received_replies": round2_replies,
            "missing_replies": missing_round2,
        },
        "timeout_sec": timeout_sec,
    }
    provisional = compute_adjudication(record, context.runtime_config)
    record["provisional_adjudication"] = provisional

    auto_round3 = bool(
        task.payload.get("auto_round3_on_challenge", context.runtime_config.auto_round3_on_challenge)
    )
    if auto_round3 and provisional.get("verdict") in {"challenge", "reject"}:
        question_round3 = "Provide a revised final proposal with measurable checks and rollout order."
        round3_payload_template = {
            "round": 3,
            "question": question_round3,
            "requested_by": context.profile.name,
            "requested_at": utc_now(),
        }
        context.logger.log(
            "peer_challenge_round3_triggered",
            requester=context.profile.name,
            task_id=task.task_id,
            provisional_verdict=provisional.get("verdict"),
            provisional_score=provisional.get("score"),
        )
        for recipient in request_targets:
            peers = [name for name in request_targets if name != recipient]
            peer_name = peers[0] if peers else ""
            payload = dict(round3_payload_template)
            payload["peer_name"] = peer_name
            payload["peer_round2_reply"] = round2_replies.get(peer_name, "")
            context.mailbox.send(
                sender=context.profile.name,
                recipient=recipient,
                subject="peer_challenge_round3_request",
                body=json.dumps(payload, ensure_ascii=False),
                task_id=task.task_id,
            )
        context.logger.log(
            "peer_challenge_round_started",
            requester=context.profile.name,
            targets=request_targets,
            task_id=task.task_id,
            round=3,
        )
        round3_replies = collect_replies("peer_challenge_round3_reply", timeout_sec)
        missing_round3 = [name for name in request_targets if name not in round3_replies]
        record["round3"] = {
            "question": question_round3,
            "received_replies": round3_replies,
            "missing_replies": missing_round3,
        }
        record["post_round3_adjudication"] = compute_adjudication(record, context.runtime_config)

    context.shared_state.set("peer_challenge", record)
    return record


def handle_lead_adjudication(context: AgentContext, _task: Task) -> Dict[str, Any]:
    peer_challenge = context.board.get_task_result("peer_challenge") or {}
    result = compute_adjudication(peer_challenge=peer_challenge, config=context.runtime_config)
    context.shared_state.set("lead_adjudication", result)
    context.mailbox.broadcast(
        sender=context.profile.name,
        subject="lead_verdict",
        body=json.dumps(result, ensure_ascii=False),
    )
    context.logger.log("lead_adjudication_published", **result)
    return result


def handle_evidence_pack(context: AgentContext, task: Task) -> Dict[str, Any]:
    initial_adjudication = context.board.get_task_result("lead_adjudication") or {}
    peer_challenge = context.board.get_task_result("peer_challenge") or {}
    initial_verdict = str(initial_adjudication.get("verdict", ""))
    default_targets = get_team_member_names(context=context, agent_type="analyst")
    targets = list(initial_adjudication.get("targets", default_targets))
    focus_areas = derive_evidence_focus_areas(initial_adjudication)
    if initial_verdict != "challenge":
        result = {
            "triggered": False,
            "reason": f"initial verdict is {initial_verdict or 'unknown'}",
            "targets": targets,
            "focus_areas": focus_areas,
            "received_replies": {},
            "missing_replies": targets,
        }
        context.shared_state.set("evidence_pack", result)
        return result

    timeout_sec = float(task.payload.get("wait_seconds", context.runtime_config.evidence_wait_seconds))
    per_target_questions: Dict[str, str] = {}
    for recipient in targets:
        peers = [name for name in targets if name != recipient]
        peer_name = peers[0] if peers else ""
        peer_objection = get_latest_agent_reply(peer_challenge, peer_name)
        target_previous_reply = get_latest_agent_reply(peer_challenge, recipient)
        question = build_targeted_evidence_question(
            focus_areas=focus_areas,
            peer_name=peer_name,
            peer_objection=peer_objection,
        )
        request_payload = {
            "question": question,
            "focus_areas": focus_areas,
            "requested_by": context.profile.name,
            "requested_at": utc_now(),
            "source_verdict": initial_verdict,
            "source_score": initial_adjudication.get("score"),
            "peer_name": peer_name,
            "peer_objection": peer_objection,
            "target_previous_reply": target_previous_reply,
        }
        context.mailbox.send(
            sender=context.profile.name,
            recipient=recipient,
            subject="evidence_request",
            body=json.dumps(request_payload, ensure_ascii=False),
            task_id=task.task_id,
        )
        per_target_questions[recipient] = question
    context.logger.log(
        "evidence_round_started",
        requester=context.profile.name,
        task_id=task.task_id,
        targets=targets,
        focus_areas=focus_areas,
    )

    deadline = time.time() + timeout_sec
    replies: Dict[str, str] = {}
    while time.time() < deadline and len(replies) < len(targets):
        incoming = context.mailbox.pull_matching(
            context.profile.name,
            lambda m: m.subject == "evidence_reply" and m.task_id == task.task_id,
        )
        for message in incoming:
            replies[message.sender] = message.body
            context.logger.log(
                "evidence_reply_received",
                requester=context.profile.name,
                from_agent=message.sender,
                task_id=task.task_id,
            )
        if len(replies) == len(targets):
            break
        time.sleep(0.1)

    missing = [name for name in targets if name not in replies]
    result = {
        "triggered": True,
        "question": "Targeted evidence requests were sent per agent.",
        "focus_areas": focus_areas,
        "per_target_questions": per_target_questions,
        "targets": targets,
        "received_replies": replies,
        "missing_replies": missing,
        "timeout_sec": timeout_sec,
        "source_verdict": initial_verdict,
        "source_score": initial_adjudication.get("score"),
    }
    context.shared_state.set("evidence_pack", result)
    return result


def handle_lead_re_adjudication(context: AgentContext, _task: Task) -> Dict[str, Any]:
    initial = context.board.get_task_result("lead_adjudication") or {}
    evidence_pack = context.board.get_task_result("evidence_pack") or {}
    initial_verdict = str(initial.get("verdict", ""))
    initial_score = int(initial.get("score", 0))
    thresholds = initial.get(
        "thresholds",
        {
            "accept": int(context.runtime_config.adjudication_accept_threshold),
            "challenge": int(context.runtime_config.adjudication_challenge_threshold),
        },
    )
    accept_threshold = int(thresholds.get("accept", context.runtime_config.adjudication_accept_threshold))
    challenge_threshold = int(
        thresholds.get("challenge", context.runtime_config.adjudication_challenge_threshold)
    )

    if initial_verdict != "challenge" or not evidence_pack.get("triggered"):
        result = dict(initial)
        result.update(
            {
                "re_adjudicated": False,
                "initial_verdict": initial_verdict,
                "initial_score": initial_score,
                "evidence_bonus": 0,
                "final_verdict": initial_verdict,
                "final_score": initial_score,
            }
        )
        context.shared_state.set("lead_re_adjudication", result)
        context.logger.log("lead_re_adjudication_skipped", verdict=initial_verdict, score=initial_score)
        return result

    bonus_eval = compute_evidence_bonus(evidence_pack=evidence_pack, config=context.runtime_config)
    bonus = int(bonus_eval.get("bonus", 0))
    final_score = min(100, initial_score + bonus)
    if final_score >= accept_threshold:
        final_verdict = "accept"
    elif final_score >= challenge_threshold:
        final_verdict = "challenge"
    else:
        final_verdict = "reject"

    result = {
        "re_adjudicated": True,
        "initial_verdict": initial_verdict,
        "initial_score": initial_score,
        "evidence_bonus": bonus,
        "evidence_evaluation": bonus_eval,
        "final_verdict": final_verdict,
        "final_score": final_score,
        "verdict": final_verdict,
        "score": final_score,
        "thresholds": {"accept": accept_threshold, "challenge": challenge_threshold},
        "weights": initial.get("weights", {}),
        "rationale": (
            f"Re-adjudicated after supplemental evidence. Bonus={bonus} "
            f"(initial={initial_score}, final={final_score})."
        ),
        "targets": initial.get("targets", []),
    }
    context.shared_state.set("lead_re_adjudication", result)
    context.mailbox.broadcast(
        sender=context.profile.name,
        subject="lead_re_verdict",
        body=json.dumps(result, ensure_ascii=False),
    )
    context.logger.log("lead_re_adjudication_published", **result)
    return result


def handle_llm_synthesis(context: AgentContext, _task: Task) -> Dict[str, Any]:
    heading_result = context.board.get_task_result("heading_audit") or {}
    length_result = context.board.get_task_result("length_audit") or {}
    dynamic_plan = context.board.get_task_result("dynamic_planning") or {}
    heading_followup = context.board.get_task_result("heading_structure_followup") or {}
    length_followup = context.board.get_task_result("length_risk_followup") or {}
    peer_challenge_result = context.board.get_task_result("peer_challenge") or {}
    lead_adjudication = context.board.get_task_result("lead_adjudication") or {}
    lead_re_adjudication = context.board.get_task_result("lead_re_adjudication") or lead_adjudication
    evidence_pack = context.board.get_task_result("evidence_pack") or {}
    heading_issues = context.shared_state.get("heading_issues", [])
    length_issues = context.shared_state.get("length_issues", [])

    synthesis_input = {
        "goal": context.goal,
        "heading_result": heading_result,
        "length_result": length_result,
        "dynamic_plan": dynamic_plan,
        "heading_followup": heading_followup,
        "length_followup": length_followup,
        "peer_challenge_result": peer_challenge_result,
        "lead_adjudication_initial": lead_adjudication,
        "evidence_pack": evidence_pack,
        "lead_adjudication_final": lead_re_adjudication,
        "heading_issue_paths": [item.get("path", "") for item in heading_issues[:20]],
        "length_issue_paths": [item.get("path", "") for item in length_issues[:20]],
    }

    system_prompt = (
        "You are a principal technical documentation reviewer. "
        "Return concise actionable recommendations. "
        "Use markdown bullet points and prioritize by impact."
    )
    user_prompt = (
        "Analyze the repository markdown quality findings below and provide:\n"
        "1) Top 3 priorities\n"
        "2) Short-term fixes (this week)\n"
        "3) Long-term guardrails\n\n"
        f"Findings JSON:\n{json.dumps(synthesis_input, ensure_ascii=False, indent=2)}"
    )

    llm_text = context.provider.complete(system_prompt=system_prompt, user_prompt=user_prompt)
    synthesis = {
        "provider": context.provider.metadata.to_dict(),
        "content": llm_text,
    }
    context.shared_state.set("llm_synthesis", synthesis)
    return {
        "provider": context.provider.metadata.to_dict(),
        "preview": llm_text[:500],
    }


def handle_recommendation_pack(context: AgentContext, _task: Task) -> Dict[str, Any]:
    heading_result = context.board.get_task_result("heading_audit") or {}
    length_result = context.board.get_task_result("length_audit") or {}
    dynamic_plan = context.shared_state.get("dynamic_plan", {})
    heading_followup = context.shared_state.get("heading_followup", {})
    length_followup = context.shared_state.get("length_followup", {})
    inventory = context.shared_state.get("markdown_inventory", [])
    heading_issues = context.shared_state.get("heading_issues", [])
    length_issues = context.shared_state.get("length_issues", [])
    peer_challenge = context.shared_state.get("peer_challenge", {})
    peer_round1 = peer_challenge.get("round1", {})
    peer_round2 = peer_challenge.get("round2", {})
    peer_round3 = peer_challenge.get("round3", {})
    peer_round1_replies = peer_round1.get("received_replies", {})
    peer_round2_replies = peer_round2.get("received_replies", {})
    peer_round3_replies = peer_round3.get("received_replies", {})
    peer_round1_missing = peer_round1.get("missing_replies", [])
    peer_round2_missing = peer_round2.get("missing_replies", [])
    peer_round3_missing = peer_round3.get("missing_replies", [])
    provisional_adjudication = peer_challenge.get("provisional_adjudication", {})
    post_round3_adjudication = peer_challenge.get("post_round3_adjudication", {})
    lead_adjudication = context.shared_state.get("lead_adjudication", {})
    evidence_pack = context.shared_state.get("evidence_pack", {})
    lead_re_adjudication = context.shared_state.get("lead_re_adjudication", lead_adjudication)
    llm_synthesis = context.shared_state.get("llm_synthesis", {})
    llm_text = llm_synthesis.get("content", "").strip()
    llm_provider = llm_synthesis.get("provider", {})

    report_path = context.output_dir / "final_report.md"
    lines: List[str] = []
    lines.append("# Agent Team Report")
    lines.append("")
    lines.append(f"- Generated at: {utc_now()}")
    lines.append(f"- Goal: {context.goal}")
    lines.append(f"- Markdown files scanned: {len(inventory)}")
    lines.append("")
    lines.append("## Team Findings")
    lines.append("")
    lines.append(f"- Files without headings: {heading_result.get('files_without_headings', 0)}")
    lines.append(f"- Long files: {length_result.get('long_files', 0)}")
    lines.append("")
    lines.append("## Dynamic Tasking")
    lines.append("")
    lines.append(f"- Enabled: {dynamic_plan.get('enabled', False)}")
    inserted_tasks = dynamic_plan.get("inserted_tasks", [])
    if inserted_tasks:
        lines.append(f"- Inserted tasks: {', '.join(inserted_tasks)}")
    else:
        lines.append("- Inserted tasks: none")
    if dynamic_plan.get("peer_challenge_dependencies_added"):
        lines.append(
            "- Added peer challenge dependencies: "
            f"{', '.join(dynamic_plan.get('peer_challenge_dependencies_added', []))}"
        )
    if heading_followup:
        low_density = heading_followup.get("lowest_heading_density", [])
        lines.append(f"- Heading follow-up rows: {len(low_density)}")
        for row in low_density[:3]:
            lines.append(
                f"- Heading density {row.get('path')}: {row.get('heading_density')} "
                f"(lines={row.get('line_count')} headings={row.get('heading_count')})"
            )
    if length_followup:
        high_risk = length_followup.get("high_risk_long_files", [])
        lines.append(f"- Length follow-up rows: {len(high_risk)}")
        for row in high_risk[:3]:
            lines.append(
                f"- Length risk {row.get('path')}: score={row.get('risk_score')} "
                f"(lines={row.get('line_count')} density={row.get('heading_density')})"
            )
    lines.append("")
    lines.append("## Peer Challenge Round")
    lines.append("")
    lines.append(f"- Round 1 question: {peer_round1.get('question', 'N/A')}")
    lines.append(f"- Round 1 replies: {len(peer_round1_replies)}")
    for sender, reply in peer_round1_replies.items():
        lines.append(f"- R1 {sender}: {reply}")
    if peer_round1_missing:
        lines.append(f"- Round 1 missing replies: {', '.join(peer_round1_missing)}")
    lines.append(f"- Round 2 question: {peer_round2.get('question', 'N/A')}")
    lines.append(f"- Round 2 replies: {len(peer_round2_replies)}")
    for sender, reply in peer_round2_replies.items():
        lines.append(f"- R2 {sender}: {reply}")
    if peer_round2_missing:
        lines.append(f"- Round 2 missing replies: {', '.join(peer_round2_missing)}")
    if peer_round3:
        lines.append(f"- Round 3 question: {peer_round3.get('question', 'N/A')}")
        lines.append(f"- Round 3 replies: {len(peer_round3_replies)}")
        for sender, reply in peer_round3_replies.items():
            lines.append(f"- R3 {sender}: {reply}")
        if peer_round3_missing:
            lines.append(f"- Round 3 missing replies: {', '.join(peer_round3_missing)}")
    if provisional_adjudication:
        lines.append(
            f"- Provisional adjudication: {provisional_adjudication.get('verdict')} "
            f"(score={provisional_adjudication.get('score')})"
        )
    if post_round3_adjudication:
        lines.append(
            f"- Post-round3 adjudication: {post_round3_adjudication.get('verdict')} "
            f"(score={post_round3_adjudication.get('score')})"
        )
    lines.append("")
    lines.append("## Evidence Pack")
    lines.append("")
    lines.append(f"- Triggered: {evidence_pack.get('triggered', False)}")
    evidence_focus_areas = evidence_pack.get("focus_areas", [])
    if evidence_focus_areas:
        lines.append(f"- Focus areas: {', '.join(evidence_focus_areas)}")
    if evidence_pack.get("triggered"):
        lines.append(f"- Question: {evidence_pack.get('question', 'N/A')}")
        per_target_questions = evidence_pack.get("per_target_questions", {})
        for target, question in per_target_questions.items():
            compact = " ".join(str(question).splitlines())
            lines.append(f"- Prompt {target}: {compact[:180]}")
        evidence_replies = evidence_pack.get("received_replies", {})
        lines.append(f"- Replies received: {len(evidence_replies)}")
        for sender, reply in evidence_replies.items():
            lines.append(f"- Evidence {sender}: {reply}")
        evidence_missing = evidence_pack.get("missing_replies", [])
        if evidence_missing:
            lines.append(f"- Missing evidence replies: {', '.join(evidence_missing)}")
    else:
        lines.append(f"- Reason: {evidence_pack.get('reason', 'not required')}")
    lines.append("")
    lines.append("## Lead Adjudication")
    lines.append("")
    lines.append(f"- Initial Verdict: {lead_adjudication.get('verdict', 'N/A')}")
    lines.append(f"- Initial Score: {lead_adjudication.get('score', 'N/A')}")
    lines.append(f"- Final Verdict: {lead_re_adjudication.get('verdict', 'N/A')}")
    lines.append(f"- Final Score: {lead_re_adjudication.get('score', 'N/A')}")
    lines.append(f"- Rationale: {lead_re_adjudication.get('rationale', 'N/A')}")
    if "evidence_bonus" in lead_re_adjudication:
        lines.append(f"- Evidence Bonus: {lead_re_adjudication.get('evidence_bonus')}")
    lead_thresholds = lead_re_adjudication.get("thresholds", {})
    lead_weights = lead_re_adjudication.get("weights", {})
    if lead_thresholds:
        lines.append(
            f"- Thresholds: accept>={lead_thresholds.get('accept')} / "
            f"challenge>={lead_thresholds.get('challenge')}"
        )
    if lead_weights:
        lines.append(
            f"- Weights: completeness={lead_weights.get('completeness')} "
            f"rebuttal={lead_weights.get('rebuttal_coverage')} depth={lead_weights.get('argument_depth')}"
        )
    lines.append("")
    lines.append("## LLM Synthesis")
    lines.append("")
    lines.append(
        f"- Provider: {llm_provider.get('provider', 'unknown')} / "
        f"model={llm_provider.get('model', 'unknown')} / mode={llm_provider.get('mode', 'unknown')}"
    )
    lines.append("")
    if llm_text:
        lines.extend(llm_text.splitlines())
    else:
        lines.append("- No LLM synthesis content generated.")
    lines.append("")
    lines.append("## Recommended Actions")
    lines.append("")
    if heading_issues:
        lines.append("1. Add at least one top-level heading to these files:")
        for item in heading_issues[:10]:
            lines.append(f"- {item['path']}")
    else:
        lines.append("1. Heading structure is acceptable in all scanned markdown files.")
    lines.append("")
    if length_issues:
        lines.append("2. Split large files into topic-focused sections:")
        for item in length_issues[:10]:
            lines.append(f"- {item['path']} ({item['line_count']} lines)")
    else:
        lines.append("2. No oversized files were detected for the current threshold.")
    lines.append("")
    lines.append("3. Enforce heading lint checks in CI for markdown consistency.")
    lines.append("")

    report_path.write_text("\n".join(lines), encoding="utf-8")
    return {
        "report_path": str(report_path),
        "heading_issues": len(heading_issues),
        "length_issues": len(length_issues),
    }


HANDLERS: Dict[str, TaskHandler] = {
    "discover_markdown": handle_discover_markdown,
    "heading_audit": handle_heading_audit,
    "length_audit": handle_length_audit,
    "dynamic_planning": handle_dynamic_planning,
    "heading_structure_followup": handle_heading_structure_followup,
    "length_risk_followup": handle_length_risk_followup,
    "peer_challenge": handle_peer_challenge,
    "lead_adjudication": handle_lead_adjudication,
    "evidence_pack": handle_evidence_pack,
    "lead_re_adjudication": handle_lead_re_adjudication,
    "llm_synthesis": handle_llm_synthesis,
    "recommendation_pack": handle_recommendation_pack,
}


class TeammateAgent(threading.Thread):
    def __init__(
        self,
        context: AgentContext,
        stop_event: threading.Event,
        claim_tasks: bool = True,
    ) -> None:
        super().__init__(name=context.profile.name, daemon=True)
        self.context = context
        self.stop_event = stop_event
        self.claim_tasks = claim_tasks
        self._local_memory: List[Dict[str, str]] = []

    def _reply_with_provider(
        self,
        topic: str,
        prompt: str,
        fallback_reply: str,
    ) -> str:
        if not self.context.runtime_config.teammate_provider_replies:
            return fallback_reply

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
            self._local_memory.append({"topic": topic, "reply": generated})
            self.context.logger.log(
                "teammate_provider_reply_generated",
                agent=self.context.profile.name,
                topic=topic,
                provider=self.context.provider.metadata.provider,
                model=self.context.provider.metadata.model,
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

    def _run_task(self, task: Task) -> None:
        lock_paths = [str(pathlib.Path(path).resolve()) for path in task.locked_paths]
        if lock_paths and not self.context.file_locks.acquire(self.context.profile.name, lock_paths):
            self.context.board.defer(
                task_id=task.task_id,
                owner=self.context.profile.name,
                reason="file lock unavailable",
            )
            time.sleep(0.1)
            return

        self.context.logger.log(
            "task_started",
            task_id=task.task_id,
            agent=self.context.profile.name,
            task_type=task.task_type,
        )
        self.context.mailbox.send(
            sender=self.context.profile.name,
            recipient=get_lead_name(self.context),
            subject="task_started",
            body=f"{self.context.profile.name} started {task.task_id}",
            task_id=task.task_id,
        )
        handler = HANDLERS.get(task.task_type)
        if handler is None:
            error = f"no handler registered for task_type={task.task_type}"
            self.context.board.fail(task_id=task.task_id, owner=self.context.profile.name, error=error)
            self.context.mailbox.send(
                sender=self.context.profile.name,
                recipient=get_lead_name(self.context),
                subject="task_failed",
                body=error,
                task_id=task.task_id,
            )
            if lock_paths:
                self.context.file_locks.release(self.context.profile.name, lock_paths)
            return

        try:
            result = handler(self.context, task)
            self.context.board.complete(task_id=task.task_id, owner=self.context.profile.name, result=result)
            self.context.mailbox.send(
                sender=self.context.profile.name,
                recipient=get_lead_name(self.context),
                subject="task_completed",
                body=f"{task.task_id} done",
                task_id=task.task_id,
            )
        except Exception as exc:  # pragma: no cover - defensive path
            error = f"{type(exc).__name__}: {exc}"
            self.context.board.fail(task_id=task.task_id, owner=self.context.profile.name, error=error)
            self.context.mailbox.send(
                sender=self.context.profile.name,
                recipient=get_lead_name(self.context),
                subject="task_failed",
                body=error,
                task_id=task.task_id,
            )
            self.context.logger.log(
                "task_exception",
                task_id=task.task_id,
                agent=self.context.profile.name,
                traceback=traceback.format_exc(),
            )
        finally:
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
        is_heading_specialist = profile_has_skill(self.context.profile, "inventory")
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
        is_heading_specialist = profile_has_skill(self.context.profile, "inventory")
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
        self.context.mailbox.send(
            sender=self.context.profile.name,
            recipient=get_lead_name(self.context),
            subject="agent_ready",
            body=f"{self.context.profile.name} online with skills {sorted(self.context.profile.skills)}",
        )
        last_idle_hook_emit_ts = 0.0
        while not self.stop_event.is_set():
            messages = self.context.mailbox.pull(self.context.profile.name)
            for message in messages:
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
        self.context.mailbox.send(
            sender=self.context.profile.name,
            recipient=get_lead_name(self.context),
            subject="agent_stopped",
            body=f"{self.context.profile.name} stopped",
        )


def _execute_worker_subprocess(command: List[str], timeout_sec: int) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
        timeout=timeout_sec,
    )


def _execute_worker_tmux(
    command: List[str],
    workdir: pathlib.Path,
    session_prefix: str,
    timeout_sec: int,
) -> subprocess.CompletedProcess[str]:
    ipc_dir = workdir / "_tmux_worker_ipc"
    ipc_dir.mkdir(parents=True, exist_ok=True)
    nonce = uuid.uuid4().hex
    session_name = f"{session_prefix}_{nonce[:8]}"
    stdout_file = ipc_dir / f"{session_name}.stdout.txt"
    stderr_file = ipc_dir / f"{session_name}.stderr.txt"
    status_file = ipc_dir / f"{session_name}.status.txt"

    shell_cmd = (
        f"{shlex.join(command)} > {shlex.quote(str(stdout_file))} "
        f"2> {shlex.quote(str(stderr_file))}; "
        f"echo $? > {shlex.quote(str(status_file))}"
    )
    spawn = subprocess.run(
        ["tmux", "new-session", "-d", "-s", session_name, shell_cmd],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if spawn.returncode != 0:
        return subprocess.CompletedProcess(
            args=command,
            returncode=spawn.returncode,
            stdout="",
            stderr=f"tmux spawn failed: {spawn.stderr.strip()}",
        )

    deadline = time.time() + timeout_sec
    while time.time() < deadline and not status_file.exists():
        time.sleep(0.1)

    subprocess.run(
        ["tmux", "kill-session", "-t", session_name],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    if not status_file.exists():
        return subprocess.CompletedProcess(
            args=command,
            returncode=124,
            stdout=stdout_file.read_text(encoding="utf-8", errors="ignore")
            if stdout_file.exists()
            else "",
            stderr=stderr_file.read_text(encoding="utf-8", errors="ignore")
            if stderr_file.exists()
            else "tmux worker timed out",
        )

    try:
        returncode = int(status_file.read_text(encoding="utf-8").strip() or "1")
    except ValueError:
        returncode = 1
    stdout = stdout_file.read_text(encoding="utf-8", errors="ignore") if stdout_file.exists() else ""
    stderr = stderr_file.read_text(encoding="utf-8", errors="ignore") if stderr_file.exists() else ""
    return subprocess.CompletedProcess(args=command, returncode=returncode, stdout=stdout, stderr=stderr)


def _run_tmux_worker_task(
    runtime_script: pathlib.Path,
    output_dir: pathlib.Path,
    runtime_config: RuntimeConfig,
    payload: Dict[str, Any],
    worker_name: str,
    logger: EventLogger,
    timeout_sec: int = 120,
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

    def _run_subprocess_fallback(reason: str) -> subprocess.CompletedProcess[str]:
        logger.log(
            "tmux_worker_fallback_attempt",
            worker=worker_name,
            reason=reason,
        )
        fallback_started = time.time()
        fallback_completed = _execute_worker_subprocess(command=command, timeout_sec=timeout_sec)
        logger.log(
            "tmux_worker_fallback_result",
            worker=worker_name,
            returncode=fallback_completed.returncode,
            duration_ms=int((time.time() - fallback_started) * 1000),
            stdout_len=len(fallback_completed.stdout or ""),
            stderr_len=len(fallback_completed.stderr or ""),
        )
        return fallback_completed

    try:
        if runtime_config.teammate_mode == "tmux":
            if shutil.which("tmux"):
                transport = "tmux"
                completed = _execute_worker_tmux(
                    command=command,
                    workdir=output_dir,
                    session_prefix=f"agent_{worker_name}",
                    timeout_sec=timeout_sec,
                )
            else:
                logger.log(
                    "tmux_unavailable_fallback_subprocess",
                    worker=worker_name,
                    reason="tmux binary not found",
                )
                completed = _execute_worker_subprocess(command=command, timeout_sec=timeout_sec)
        else:
            completed = _execute_worker_subprocess(command=command, timeout_sec=timeout_sec)

        logger.log(
            "tmux_worker_transport_result",
            worker=worker_name,
            transport=transport,
            returncode=completed.returncode,
            duration_ms=int((time.time() - started_at) * 1000),
            stdout_len=len(completed.stdout or ""),
            stderr_len=len(completed.stderr or ""),
        )

        if completed.returncode != 0:
            if (
                transport == "tmux"
                and runtime_config.tmux_fallback_on_error
                and runtime_config.teammate_mode == "tmux"
            ):
                completed = _run_subprocess_fallback(reason=f"tmux_returncode={completed.returncode}")
                transport = "tmux->subprocess_fallback"
            else:
                stderr = (completed.stderr or "").strip()
                stdout = (completed.stdout or "").strip()
                detail = stderr or stdout or f"worker exited with code {completed.returncode}"
                return {
                    "ok": False,
                    "error": f"worker execution failed via {transport}: {detail[:400]}",
                    "transport": transport,
                }

        if completed.returncode != 0:
            stderr = (completed.stderr or "").strip()
            stdout = (completed.stdout or "").strip()
            detail = stderr or stdout or f"worker exited with code {completed.returncode}"
            return {
                "ok": False,
                "error": f"worker execution failed via {transport}: {detail[:400]}",
                "transport": transport,
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
                if completed.returncode != 0:
                    stderr = (completed.stderr or "").strip()
                    stdout = (completed.stdout or "").strip()
                    detail = stderr or stdout or f"worker exited with code {completed.returncode}"
                    return {
                        "ok": False,
                        "error": f"worker execution failed via {transport}: {detail[:400]}",
                        "transport": transport,
                    }
                try:
                    parsed = json.loads((completed.stdout or "").strip())
                except json.JSONDecodeError as exc2:
                    return {
                        "ok": False,
                        "error": f"worker returned invalid JSON via {transport}: {exc2}",
                        "transport": transport,
                    }
            else:
                return {
                    "ok": False,
                    "error": f"worker returned invalid JSON via {transport}: {exc}",
                    "transport": transport,
                }
        if not isinstance(parsed, dict):
            return {
                "ok": False,
                "error": f"worker returned non-object payload via {transport}",
                "transport": transport,
            }
        return {
            "ok": True,
            "payload": parsed,
            "transport": transport,
        }
    finally:
        try:
            payload_file.unlink(missing_ok=True)
        except OSError:
            pass


def run_tmux_analyst_task_once(
    lead_context: AgentContext,
    analyst_profiles: Sequence[AgentProfile],
    runtime_script: pathlib.Path,
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
        if task.task_type not in TMUX_ANALYST_TASK_TYPES:
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
        lead_context.logger.log(
            "tmux_worker_task_dispatched",
            worker=profile.name,
            task_id=task.task_id,
            task_type=task.task_type,
        )
        execution = _run_tmux_worker_task(
            runtime_script=runtime_script,
            output_dir=lead_context.output_dir,
            runtime_config=lead_context.runtime_config,
            payload=payload,
            worker_name=profile.name,
            logger=lead_context.logger,
            timeout_sec=worker_timeout_sec,
        )
        if not execution.get("ok"):
            error = str(execution.get("error", "unknown worker error"))
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
            )
            if lock_paths:
                lead_context.file_locks.release(profile.name, lock_paths)
            return True

        worker_payload = execution.get("payload", {})
        result = worker_payload.get("result", {})
        state_updates = worker_payload.get("state_updates", {})
        if isinstance(state_updates, dict):
            for key, value in state_updates.items():
                lead_context.shared_state.set(str(key), value)
        if not isinstance(result, dict):
            result = {"raw_result": result}
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
        )
        if lock_paths:
            lead_context.file_locks.release(profile.name, lock_paths)
        return True
    return False


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


def build_profiles(team_config: Optional[TeamConfig] = None) -> List[AgentProfile]:
    effective_team = team_config or default_team_config()
    return effective_team.to_profiles()


def checkpoint_history_dir(output_dir: pathlib.Path) -> pathlib.Path:
    return output_dir / CHECKPOINT_HISTORY_DIRNAME


def checkpoint_history_file(output_dir: pathlib.Path, history_index: int) -> pathlib.Path:
    return checkpoint_history_dir(output_dir) / f"checkpoint_{history_index:06d}.json"


def list_checkpoint_history_files(output_dir: pathlib.Path) -> List[pathlib.Path]:
    history_dir = checkpoint_history_dir(output_dir)
    if not history_dir.exists():
        return []
    files = sorted(path for path in history_dir.glob("checkpoint_*.json") if path.is_file())
    return files


def checkpoint_history_index_from_path(path: pathlib.Path) -> int:
    stem = path.stem
    suffix = stem.split("_")[-1]
    return int(suffix)


def resolve_checkpoint_by_history_index(output_dir: pathlib.Path, history_index: int) -> pathlib.Path:
    if history_index < 0:
        raise ValueError("--rewind-to-history-index must be >= 0")
    candidate = checkpoint_history_file(output_dir=output_dir, history_index=history_index)
    if candidate.exists():
        return candidate
    available = list_checkpoint_history_files(output_dir=output_dir)
    available_indices: List[int] = []
    for path in available:
        stem = path.stem
        try:
            available_indices.append(int(stem.split("_")[-1]))
        except ValueError:
            continue
    if available_indices:
        raise ValueError(
            f"history index {history_index} not found. available={available_indices[:20]}"
        )
    raise ValueError(
        f"history index {history_index} not found and no checkpoint history exists in {checkpoint_history_dir(output_dir)}"
    )


def resolve_checkpoint_by_event_index(output_dir: pathlib.Path, event_index: int) -> Dict[str, Any]:
    if event_index < 0:
        raise ValueError("--rewind-to-event-index must be >= 0")

    entries: List[Dict[str, Any]] = []
    for path in list_checkpoint_history_files(output_dir=output_dir):
        try:
            payload = load_checkpoint(path)
        except Exception:
            continue
        raw_history_index = payload.get("history_index", "")
        try:
            history_index = int(raw_history_index)
        except (TypeError, ValueError):
            try:
                history_index = checkpoint_history_index_from_path(path)
            except Exception:
                continue
        raw_event_count = payload.get("event_count", "")
        try:
            checkpoint_event_count = int(raw_event_count)
        except (TypeError, ValueError):
            checkpoint_event_count = -1
        entries.append(
            {
                "history_index": history_index,
                "checkpoint_path": path,
                "checkpoint_event_count": checkpoint_event_count,
            }
        )

    if not entries:
        raise ValueError(
            f"event index {event_index} not found and no checkpoint history exists in {checkpoint_history_dir(output_dir)}"
        )
    entries.sort(key=lambda item: int(item["history_index"]))
    with_event_count = [item for item in entries if int(item.get("checkpoint_event_count", -1)) >= 0]
    if not with_event_count:
        raise ValueError(
            "checkpoint history does not contain event_count metadata; "
            "create a fresh run with this runtime version before using --rewind-to-event-index"
        )

    requested_event_count = event_index + 1
    eligible = [
        item for item in with_event_count if int(item.get("checkpoint_event_count", -1)) <= requested_event_count
    ]
    if eligible:
        chosen = eligible[-1]
        resolution = "at_or_before"
    else:
        chosen = with_event_count[0]
        resolution = "closest_after"

    return {
        "requested_event_index": event_index,
        "resolved_history_index": int(chosen["history_index"]),
        "resolved_checkpoint_event_count": int(chosen["checkpoint_event_count"]),
        "resolved_checkpoint": str(pathlib.Path(chosen["checkpoint_path"]).resolve()),
        "resolution": resolution,
    }


def default_rewind_branch_output_dir(source_output_dir: pathlib.Path, history_index: int) -> pathlib.Path:
    stamp = dt.datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    return source_output_dir / "branches" / f"rewind_{history_index:06d}_{stamp}"


def default_event_rewind_branch_output_dir(source_output_dir: pathlib.Path, event_index: int) -> pathlib.Path:
    stamp = dt.datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    return source_output_dir / "branches" / f"rewind_event_{event_index:08d}_{stamp}"


def default_history_replay_report_path(output_dir: pathlib.Path) -> pathlib.Path:
    return output_dir / "checkpoint_replay.md"


def events_file(output_dir: pathlib.Path) -> pathlib.Path:
    return output_dir / "events.jsonl"


def seed_branch_events_from_source(
    source_output_dir: pathlib.Path,
    target_output_dir: pathlib.Path,
    max_event_index: int,
) -> Dict[str, Any]:
    source_path = events_file(source_output_dir)
    target_path = events_file(target_output_dir)
    if max_event_index < 0:
        return {
            "seeded": False,
            "reason": "invalid_max_event_index",
            "seeded_count": 0,
            "seed_event_index": max_event_index,
        }
    if not source_path.exists():
        return {
            "seeded": False,
            "reason": "source_events_missing",
            "seeded_count": 0,
            "seed_event_index": max_event_index,
            "source_events_path": str(source_path),
            "target_events_path": str(target_path),
        }

    seeded_events: List[Dict[str, Any]] = []
    next_fallback_index = 0
    with source_path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(payload, dict):
                continue
            raw_idx = payload.get("event_index")
            if isinstance(raw_idx, int):
                event_index = raw_idx
                next_fallback_index = max(next_fallback_index, event_index + 1)
            else:
                event_index = next_fallback_index
                next_fallback_index += 1
            if event_index > max_event_index:
                break
            payload["event_index"] = event_index
            seeded_events.append(payload)

    target_path.parent.mkdir(parents=True, exist_ok=True)
    with target_path.open("w", encoding="utf-8") as fh:
        for payload in seeded_events:
            fh.write(json.dumps(payload, ensure_ascii=False) + "\n")
    return {
        "seeded": True,
        "seeded_count": len(seeded_events),
        "seed_event_index": max_event_index,
        "source_events_path": str(source_path),
        "target_events_path": str(target_path),
    }


def _task_status_counts_from_checkpoint_payload(payload: Dict[str, Any]) -> Dict[str, int]:
    board_payload = payload.get("task_board", {})
    tasks = board_payload.get("tasks", []) if isinstance(board_payload, dict) else []
    counts = {"completed": 0, "failed": 0, "pending": 0, "blocked": 0, "in_progress": 0, "other": 0}
    for task in tasks:
        if not isinstance(task, dict):
            counts["other"] += 1
            continue
        status = str(task.get("status", "other"))
        if status not in counts:
            counts["other"] += 1
            continue
        counts[status] += 1
    return counts


def write_history_replay_report(
    output_dir: pathlib.Path,
    report_path: pathlib.Path,
    start_index: int = -1,
    end_index: int = -1,
) -> Dict[str, Any]:
    history_files = list_checkpoint_history_files(output_dir=output_dir)
    if not history_files:
        raise ValueError(f"no checkpoint history found in {checkpoint_history_dir(output_dir)}")

    indexed_files: List[tuple[int, pathlib.Path]] = []
    for path in history_files:
        try:
            idx = checkpoint_history_index_from_path(path)
        except ValueError:
            continue
        indexed_files.append((idx, path))
    if not indexed_files:
        raise ValueError("no valid checkpoint history files found")
    indexed_files.sort(key=lambda item: item[0])

    min_idx = indexed_files[0][0]
    max_idx = indexed_files[-1][0]
    if start_index < 0:
        start_index = min_idx
    if end_index < 0:
        end_index = max_idx
    if start_index > end_index:
        raise ValueError(
            f"invalid replay range: start_index({start_index}) > end_index({end_index})"
        )

    selected = [(idx, path) for idx, path in indexed_files if start_index <= idx <= end_index]
    if not selected:
        raise ValueError(
            f"no checkpoint history in range [{start_index}, {end_index}] "
            f"(available=[{min_idx}, {max_idx}])"
        )

    lines: List[str] = []
    lines.append("# Checkpoint History Replay")
    lines.append("")
    lines.append(f"- Generated at: {utc_now()}")
    lines.append(f"- Output dir: {output_dir}")
    lines.append(f"- Replay range: [{start_index}, {end_index}]")
    lines.append(f"- Snapshots in report: {len(selected)}")
    lines.append("")
    lines.append("## Timeline")
    lines.append("")

    previous_task_states: Dict[str, str] = {}
    for idx, path in selected:
        payload = load_checkpoint(path)
        counts = _task_status_counts_from_checkpoint_payload(payload)
        raw_event_count = payload.get("event_count", "")
        try:
            checkpoint_event_count = int(raw_event_count)
        except (TypeError, ValueError):
            checkpoint_event_count = -1
        lines.append(f"### Snapshot {idx}")
        lines.append("")
        lines.append(f"- Saved at: {payload.get('saved_at', '')}")
        lines.append(f"- Interrupted reason: {payload.get('interrupted_reason', '') or 'none'}")
        lines.append(f"- Resume from: {payload.get('resume_from', '') or 'none'}")
        if checkpoint_event_count >= 0:
            lines.append(
                f"- Event coverage: event_index <= {max(0, checkpoint_event_count - 1)} "
                f"(event_count={checkpoint_event_count})"
            )
        lines.append(
            "- Task states: "
            f"completed={counts.get('completed', 0)} "
            f"failed={counts.get('failed', 0)} "
            f"pending={counts.get('pending', 0)} "
            f"blocked={counts.get('blocked', 0)} "
            f"in_progress={counts.get('in_progress', 0)}"
        )

        board_payload = payload.get("task_board", {})
        tasks = board_payload.get("tasks", []) if isinstance(board_payload, dict) else []
        current_states: Dict[str, str] = {}
        for task in tasks:
            if not isinstance(task, dict):
                continue
            task_id = str(task.get("task_id", ""))
            status = str(task.get("status", ""))
            if not task_id:
                continue
            current_states[task_id] = status

        changed: List[str] = []
        if previous_task_states:
            for task_id, status in sorted(current_states.items()):
                prev = previous_task_states.get(task_id)
                if prev is not None and prev != status:
                    changed.append(f"{task_id}: {prev} -> {status}")
        if changed:
            lines.append("- Status transitions since previous snapshot:")
            for row in changed[:20]:
                lines.append(f"  - {row}")
        else:
            lines.append("- Status transitions since previous snapshot: none")
        lines.append("")
        previous_task_states = current_states

    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text("\n".join(lines), encoding="utf-8")
    return {
        "report_path": str(report_path),
        "start_index": start_index,
        "end_index": end_index,
        "snapshot_count": len(selected),
    }


def load_events_for_replay(output_dir: pathlib.Path) -> List[Dict[str, Any]]:
    path = events_file(output_dir)
    if not path.exists():
        raise ValueError(f"events file does not exist: {path}")
    events: List[Dict[str, Any]] = []
    next_fallback_index = 0
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(payload, dict):
                continue
            raw_idx = payload.get("event_index")
            if isinstance(raw_idx, int):
                event_index = raw_idx
                next_fallback_index = max(next_fallback_index, event_index + 1)
            else:
                event_index = next_fallback_index
                next_fallback_index += 1
            payload["event_index"] = event_index
            events.append(payload)
    events.sort(key=lambda item: int(item.get("event_index", 0)))
    return events


def replay_task_states_from_events(
    events: Sequence[Dict[str, Any]],
    max_transitions: int = 200,
) -> Dict[str, Any]:
    tasks: Dict[str, Dict[str, Any]] = {}
    transitions: List[str] = []

    def ensure_task(task_id: str) -> Dict[str, Any]:
        task = tasks.get(task_id)
        if task is None:
            task = {
                "task_id": task_id,
                "title": "",
                "status": "unknown",
                "owner": "",
                "dependencies": [],
            }
            tasks[task_id] = task
        return task

    for event in events:
        event_name = str(event.get("event", ""))
        event_index = int(event.get("event_index", -1))
        if event_name == "task_inserted":
            task_id = str(event.get("task_id", ""))
            if not task_id:
                continue
            task = ensure_task(task_id)
            task["title"] = str(event.get("title", task.get("title", "")))
            task["status"] = "pending"
            deps = event.get("dependencies", [])
            if isinstance(deps, list):
                task["dependencies"] = [str(dep) for dep in deps]
            if len(transitions) < max_transitions:
                transitions.append(f"[{event_index}] {task_id}: inserted -> pending")
            continue
        if event_name == "task_dependency_added":
            task_id = str(event.get("task_id", ""))
            dep_id = str(event.get("dependency_id", ""))
            if not task_id or not dep_id:
                continue
            task = ensure_task(task_id)
            deps = list(task.get("dependencies", []))
            if dep_id not in deps:
                deps.append(dep_id)
                task["dependencies"] = deps
            if len(transitions) < max_transitions:
                transitions.append(f"[{event_index}] {task_id}: +dependency {dep_id}")
            continue
        if event_name in {"task_claimed", "task_deferred", "task_completed", "task_failed"}:
            task_id = str(event.get("task_id", ""))
            if not task_id:
                continue
            task = ensure_task(task_id)
            prev = str(task.get("status", "unknown"))
            if event_name == "task_claimed":
                task["status"] = "in_progress"
                task["owner"] = str(event.get("agent", ""))
            elif event_name == "task_deferred":
                task["status"] = "pending"
                task["owner"] = ""
            elif event_name == "task_completed":
                task["status"] = "completed"
                task["owner"] = str(event.get("owner", task.get("owner", "")))
            elif event_name == "task_failed":
                task["status"] = "failed"
                task["owner"] = str(event.get("owner", task.get("owner", "")))
            if len(transitions) < max_transitions:
                transitions.append(
                    f"[{event_index}] {task_id}: {prev} -> {task['status']}"
                )

    status_counts: Dict[str, int] = {}
    for task in tasks.values():
        status = str(task.get("status", "unknown"))
        status_counts[status] = status_counts.get(status, 0) + 1
    return {
        "task_count": len(tasks),
        "status_counts": status_counts,
        "tasks": tasks,
        "transitions": transitions,
        "transition_total": len(transitions),
    }


def write_event_replay_report(
    output_dir: pathlib.Path,
    report_path: pathlib.Path,
    max_transitions: int = 200,
) -> Dict[str, Any]:
    if max_transitions <= 0:
        raise ValueError("max_transitions must be > 0")
    events = load_events_for_replay(output_dir=output_dir)
    replay = replay_task_states_from_events(events=events, max_transitions=max_transitions)

    board_path = output_dir / "task_board.json"
    board_statuses: Dict[str, str] = {}
    if board_path.exists():
        board_payload = json.loads(board_path.read_text(encoding="utf-8"))
        for task in board_payload.get("tasks", []):
            if not isinstance(task, dict):
                continue
            task_id = str(task.get("task_id", ""))
            status = str(task.get("status", ""))
            if task_id:
                board_statuses[task_id] = status

    replay_statuses = {
        task_id: str(task.get("status", ""))
        for task_id, task in replay.get("tasks", {}).items()
    }
    mismatches: List[str] = []
    for task_id, board_status in board_statuses.items():
        replay_status = replay_statuses.get(task_id, "missing")
        if replay_status != board_status:
            mismatches.append(f"{task_id}: replay={replay_status} board={board_status}")

    lines: List[str] = []
    lines.append("# Event Replay Report")
    lines.append("")
    lines.append(f"- Generated at: {utc_now()}")
    lines.append(f"- Output dir: {output_dir}")
    lines.append(f"- Event count: {len(events)}")
    lines.append(f"- Replayed task count: {replay.get('task_count', 0)}")
    lines.append("")
    lines.append("## Status Counts")
    lines.append("")
    status_counts = replay.get("status_counts", {})
    for status, count in sorted(status_counts.items()):
        lines.append(f"- {status}: {count}")
    lines.append("")
    lines.append("## Task Board Consistency")
    lines.append("")
    if mismatches:
        lines.append(f"- Mismatches: {len(mismatches)}")
        for row in mismatches[:50]:
            lines.append(f"- {row}")
    else:
        lines.append("- Mismatches: 0")
    lines.append("")
    lines.append("## Transitions")
    lines.append("")
    transitions = replay.get("transitions", [])
    if transitions:
        for row in transitions:
            lines.append(f"- {row}")
    else:
        lines.append("- none")
    lines.append("")

    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text("\n".join(lines), encoding="utf-8")
    return {
        "report_path": str(report_path),
        "event_count": len(events),
        "task_count": int(replay.get("task_count", 0)),
        "mismatch_count": len(mismatches),
    }


def write_artifacts(
    output_dir: pathlib.Path,
    board: TaskBoard,
    mailbox: Mailbox,
    shared_state: SharedState,
    file_locks: FileLockRegistry,
    logger: EventLogger,
    provider_meta: ProviderMetadata,
    runtime_config: RuntimeConfig,
    checkpoint_path: Optional[pathlib.Path] = None,
    resume_from: Optional[pathlib.Path] = None,
    interrupted_reason: str = "",
    rewind_history_index: Optional[int] = None,
    rewind_event_index: Optional[int] = None,
    rewind_event_resolution: Optional[Dict[str, Any]] = None,
    rewind_source_output_dir: Optional[pathlib.Path] = None,
    rewind_source_checkpoint: Optional[pathlib.Path] = None,
    branch_run_id: str = "",
    rewind_seed_event_index: Optional[int] = None,
    rewind_seed_event_count: int = 0,
) -> None:
    board_path = output_dir / "task_board.json"
    board_snapshot = board.snapshot()
    board_path.write_text(
        json.dumps(board_snapshot, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    state_path = output_dir / "shared_state.json"
    state_snapshot = shared_state.snapshot()
    state_path.write_text(
        json.dumps(state_snapshot, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    lock_path = output_dir / "file_locks.json"
    lock_path.write_text(
        json.dumps(file_locks.snapshot(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    summary_path = output_dir / "run_summary.json"
    summary = {
        "generated_at": utc_now(),
        "events_path": str(logger.path),
        "task_board_path": str(board_path),
        "shared_state_path": str(state_path),
        "lock_state_path": str(lock_path),
        "final_report_path": str(output_dir / "final_report.md"),
        "mailbox_model": state_snapshot.get("team", {}).get(
            "mailbox_model",
            "asynchronous pull-based inbox",
        ),
        "provider": provider_meta.to_dict(),
        "runtime_config": runtime_config.to_dict(),
        "host": state_snapshot.get("host", {}),
        "team": state_snapshot.get("team", {}),
        "workflow": state_snapshot.get("workflow", {}),
        "policies": state_snapshot.get("policies", {}),
        "agent_team_config": state_snapshot.get("agent_team_config", {}),
        "config_source": state_snapshot.get("agent_team_config", {}).get("source_path", ""),
        "task_count": len(board_snapshot.get("tasks", [])),
        "checkpoint_path": str(checkpoint_path) if checkpoint_path else "",
        "checkpoint_history_dir": str(checkpoint_history_dir(output_dir)),
        "resume_from": str(resume_from) if resume_from else "",
        "interrupted_reason": interrupted_reason,
        "rewind_history_index": rewind_history_index if rewind_history_index is not None else "",
        "rewind_event_index": rewind_event_index if rewind_event_index is not None else "",
        "rewind_event_resolution": rewind_event_resolution or {},
        "rewind_source_output_dir": (
            str(rewind_source_output_dir) if rewind_source_output_dir else ""
        ),
        "rewind_source_checkpoint": str(rewind_source_checkpoint) if rewind_source_checkpoint else "",
        "branch_run_id": branch_run_id,
        "rewind_seed_event_index": (
            rewind_seed_event_index if rewind_seed_event_index is not None else ""
        ),
        "rewind_seed_event_count": max(0, int(rewind_seed_event_count)),
    }
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")


def write_checkpoint(
    checkpoint_path: pathlib.Path,
    goal: str,
    target_dir: pathlib.Path,
    output_dir: pathlib.Path,
    board: TaskBoard,
    shared_state: SharedState,
    runtime_config: RuntimeConfig,
    provider_meta: ProviderMetadata,
    resume_from: Optional[pathlib.Path] = None,
    interrupted_reason: str = "",
    rewind_history_index: Optional[int] = None,
    rewind_event_index: Optional[int] = None,
    rewind_event_resolution: Optional[Dict[str, Any]] = None,
    rewind_source_output_dir: Optional[pathlib.Path] = None,
    rewind_source_checkpoint: Optional[pathlib.Path] = None,
    branch_run_id: str = "",
    event_count: int = 0,
    rewind_seed_event_index: Optional[int] = None,
    rewind_seed_event_count: int = 0,
) -> None:
    current_history_index = int(shared_state.get("_checkpoint_history_last_index", -1))
    next_history_index = current_history_index + 1
    shared_state.set("_checkpoint_history_last_index", next_history_index)
    shared_snapshot = shared_state.snapshot()
    payload = {
        "version": CHECKPOINT_VERSION,
        "saved_at": utc_now(),
        "goal": goal,
        "target_dir": str(target_dir),
        "output_dir": str(output_dir),
        "runtime_config": runtime_config.to_dict(),
        "provider": provider_meta.to_dict(),
        "task_board": board.snapshot(),
        "shared_state": shared_snapshot,
        "resume_from": str(resume_from) if resume_from else "",
        "interrupted_reason": interrupted_reason,
        "history_index": next_history_index,
        "event_count": max(0, int(event_count)),
        "rewind_history_index": rewind_history_index if rewind_history_index is not None else "",
        "rewind_event_index": rewind_event_index if rewind_event_index is not None else "",
        "rewind_event_resolution": rewind_event_resolution or {},
        "rewind_source_output_dir": (
            str(rewind_source_output_dir) if rewind_source_output_dir else ""
        ),
        "rewind_source_checkpoint": str(rewind_source_checkpoint) if rewind_source_checkpoint else "",
        "branch_run_id": branch_run_id,
        "rewind_seed_event_index": (
            rewind_seed_event_index if rewind_seed_event_index is not None else ""
        ),
        "rewind_seed_event_count": max(0, int(rewind_seed_event_count)),
    }
    checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
    checkpoint_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    history_file = checkpoint_history_file(output_dir=output_dir, history_index=next_history_index)
    history_file.parent.mkdir(parents=True, exist_ok=True)
    history_file.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def load_checkpoint(checkpoint_path: pathlib.Path) -> Dict[str, Any]:
    payload = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("checkpoint payload must be an object")
    version = int(payload.get("version", 0))
    if version != CHECKPOINT_VERSION:
        raise ValueError(
            f"unsupported checkpoint version: got={version} expected={CHECKPOINT_VERSION}"
        )
    return payload


def restore_tasks_from_checkpoint_payload(checkpoint_payload: Dict[str, Any]) -> List[Task]:
    task_board_payload = checkpoint_payload.get("task_board", {})
    if not isinstance(task_board_payload, dict):
        raise ValueError("checkpoint.task_board must be an object")
    task_dicts = task_board_payload.get("tasks", [])
    if not isinstance(task_dicts, list):
        raise ValueError("checkpoint.task_board.tasks must be a list")
    tasks = [task_from_dict(task_payload) for task_payload in task_dicts if isinstance(task_payload, dict)]
    if not tasks:
        raise ValueError("checkpoint contains no tasks")
    return tasks


def restore_shared_state_from_checkpoint_payload(
    shared_state: SharedState,
    checkpoint_payload: Dict[str, Any],
) -> None:
    snapshot = checkpoint_payload.get("shared_state", {})
    if not isinstance(snapshot, dict):
        return
    for key, value in snapshot.items():
        shared_state.set(str(key), value)


def run_lead_task_once(lead_context: AgentContext, task_id: str) -> bool:
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
    handler = HANDLERS.get(task.task_type)
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
            traceback=traceback.format_exc(),
        )
    return True


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
        else build_tasks(
            output_dir=output_dir,
            runtime_config=runtime_config,
            workflow_pack=effective_agent_team_config.workflow.pack,
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
    )

    runtime_script = pathlib.Path(__file__).resolve()
    stop_event = threading.Event()
    workers = [
        TeammateAgent(
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
            ),
            stop_event=stop_event,
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
                while run_tmux_analyst_task_once(
                    lead_context=lead_context,
                    analyst_profiles=analyst_profiles,
                    runtime_script=runtime_script,
                    worker_timeout_sec=runtime_config.tmux_worker_timeout_sec,
                ):
                    ran_tmux_task = True
            ran_lead_initial = run_lead_task_once(lead_context=lead_context, task_id="lead_adjudication")
            ran_lead_re = run_lead_task_once(lead_context=lead_context, task_id="lead_re_adjudication")
            ran_lead_task = ran_lead_initial or ran_lead_re or ran_tmux_task
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


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run local agent team runtime (MVP).")
    parser.add_argument(
        "--goal",
        default="Audit markdown quality with a lead and teammate workflow.",
        help="Natural language goal statement.",
    )
    parser.add_argument(
        "--target",
        default=".",
        help="Target directory for markdown analysis.",
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
        help="Workflow pack to run. Current built-in option: markdown-audit.",
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
        agent_team_config = build_agent_team_config_from_args(args=args, runtime_config=runtime_config)

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
