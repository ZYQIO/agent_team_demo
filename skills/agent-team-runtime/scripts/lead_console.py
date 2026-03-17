#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
import pathlib
import sys
import time
from typing import Any, Dict, List


LEAD_COMMANDS_FILENAME = "lead_commands.jsonl"
LEAD_INTERACTION_FILENAME = "lead_interaction.json"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Inspect live lead interaction state and send plan-approval commands."
    )
    parser.add_argument("--output", required=True, help="Runtime output directory.")
    parser.add_argument(
        "--approve-plan",
        action="append",
        default=[],
        help="Approve a pending plan by task id. Can be specified multiple times.",
    )
    parser.add_argument(
        "--approve-teammate",
        action="append",
        default=[],
        help="Approve all pending plans requested by a teammate. Can be specified multiple times.",
    )
    parser.add_argument(
        "--reject-plan",
        action="append",
        default=[],
        help="Reject a pending plan by task id. Can be specified multiple times.",
    )
    parser.add_argument(
        "--reject-teammate",
        action="append",
        default=[],
        help="Reject all pending plans requested by a teammate. Can be specified multiple times.",
    )
    parser.add_argument(
        "--approve-all-pending-plans",
        action="store_true",
        help="Approve all pending plan requests.",
    )
    parser.add_argument(
        "--request-status",
        action="append",
        default=[],
        help="Request a live status reply from a teammate by agent name. Can be specified multiple times.",
    )
    parser.add_argument(
        "--request-plan",
        action="append",
        default=[],
        help="Request a live plan/next-step reply from a teammate by agent name. Can be specified multiple times.",
    )
    parser.add_argument(
        "--watch",
        action="store_true",
        help="Continuously print the current lead interaction snapshot until interrupted.",
    )
    parser.add_argument(
        "--interactive",
        action="store_true",
        help="Open a simple prompt that can refresh status and send approve/reject commands.",
    )
    parser.add_argument(
        "--show-teammate",
        action="append",
        default=[],
        help="Print detailed live session information for a teammate by agent name. Can be specified multiple times.",
    )
    parser.add_argument(
        "--review-teammate",
        action="append",
        default=[],
        help="Print a combined teammate review with session, recent lead-visible messages, and pending approvals.",
    )
    parser.add_argument(
        "--review-pending",
        action="store_true",
        help="Print teammate-centric review summaries for all current pending approvals.",
    )
    parser.add_argument(
        "--review-next",
        action="store_true",
        help="Print the next teammate review that lead should look at based on current pending approvals.",
    )
    parser.add_argument(
        "--refresh-seconds",
        type=float,
        default=1.0,
        help="Polling interval for --watch. Must be > 0.",
    )
    parser.add_argument(
        "--max-messages",
        type=int,
        default=6,
        help="Maximum recent messages to print in status output.",
    )
    parser.add_argument(
        "--max-commands",
        type=int,
        default=6,
        help="Maximum recent commands to print in status output.",
    )
    return parser.parse_args()


def load_snapshot(output_dir: pathlib.Path) -> Dict[str, Any]:
    snapshot_path = output_dir / LEAD_INTERACTION_FILENAME
    if not snapshot_path.exists():
        return {}
    try:
        payload = json.loads(snapshot_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    if not isinstance(payload, dict):
        return {}
    return payload


def append_command(command_path: pathlib.Path, payload: Dict[str, Any]) -> None:
    command_path.parent.mkdir(parents=True, exist_ok=True)
    command_path.touch(exist_ok=True)
    with command_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(payload, ensure_ascii=False) + "\n")


def resolve_command_path(output_dir: pathlib.Path, snapshot: Dict[str, Any]) -> pathlib.Path:
    raw_path = str(snapshot.get("command_path", "") or "")
    if raw_path:
        return pathlib.Path(raw_path).resolve()
    return (output_dir / LEAD_COMMANDS_FILENAME).resolve()


def _task_preview_text(item: Dict[str, Any]) -> str:
    task_id = str(item.get("task_id", "") or "")
    task_type = str(item.get("task_type", "") or "")
    title = str(item.get("title", "") or "")
    agent_types = ",".join(item.get("allowed_agent_types", [])) if isinstance(item.get("allowed_agent_types", []), list) else ""
    dependencies = ",".join(item.get("dependencies", [])) if isinstance(item.get("dependencies", []), list) else ""
    return (
        f"{task_id}[{task_type or 'unknown'}]"
        + (f" title={title}" if title else "")
        + (f" agents={agent_types}" if agent_types else "")
        + (f" deps={dependencies}" if dependencies else "")
    )


def _dependency_preview_text(item: Dict[str, Any]) -> str:
    return f"{str(item.get('task_id', '') or '')}+={str(item.get('dependency_id', '') or '')}"


def describe_pending_request(snapshot: Dict[str, Any], task_id: str) -> List[str]:
    pending = [
        item
        for item in snapshot.get("plan_approval_requests", [])
        if isinstance(item, dict)
        and str(item.get("status", "") or "") == "pending"
        and str(item.get("task_id", "") or "") == str(task_id or "")
    ]
    if not pending:
        return [f"unknown pending approval: {task_id}"]
    item = pending[0]
    lines = [
        (
            f"task_id={item.get('task_id', '')} "
            f"task_type={item.get('task_type', '')} "
            f"requested_by={item.get('requested_by', '')} "
            f"transport={item.get('transport', '')} "
            f"status={item.get('status', '')}"
        ),
        "result_keys="
        + (
            ",".join(sorted(str(key) for key in item.get("result", {}).keys()))
            if isinstance(item.get("result", {}), dict)
            else "none"
        ),
        "state_update_keys="
        + (
            ",".join(sorted(str(key) for key in item.get("state_updates", {}).keys()))
            if isinstance(item.get("state_updates", {}), dict)
            else "none"
        ),
    ]
    task_preview = [
        _task_preview_text(preview)
        for preview in item.get("proposed_tasks_preview", [])
        if isinstance(preview, dict)
    ]
    dependency_preview = [
        _dependency_preview_text(preview)
        for preview in item.get("proposed_dependencies_preview", [])
        if isinstance(preview, dict)
    ]
    if task_preview:
        lines.append("task_preview=" + "; ".join(task_preview))
    if dependency_preview:
        lines.append("dependency_preview=" + "; ".join(dependency_preview))
    return lines


def describe_teammate_session(snapshot: Dict[str, Any], agent_name: str) -> List[str]:
    teammate_sessions = snapshot.get("teammate_sessions", [])
    if not isinstance(teammate_sessions, list):
        teammate_sessions = []
    matched = next(
        (
            item
            for item in teammate_sessions
            if isinstance(item, dict) and str(item.get("agent", "") or "") == str(agent_name or "")
        ),
        None,
    )
    if matched is None:
        return [f"unknown teammate: {agent_name}"]
    current_task = str(matched.get("current_task_id", "") or "none")
    if str(matched.get("current_task_type", "") or "") and current_task != "none":
        current_task += f"[{matched.get('current_task_type', '')}]"
    last_task = str(matched.get("last_task_id", "") or "none")
    if last_task != "none":
        last_task += f"({str(matched.get('last_task_status', '') or 'unknown')})"
    lines = [
        (
            f"agent={matched.get('agent', '')} "
            f"agent_type={matched.get('agent_type', '') or 'unknown'} "
            f"transport={matched.get('transport', '') or 'unknown'} "
            f"backend={matched.get('transport_backend', '') or 'n/a'} "
            f"status={matched.get('status', '') or 'unknown'}"
        ),
        f"current_task={current_task}",
        f"last_task={last_task}",
        (
            f"task_counts=started:{matched.get('tasks_started', 0)} "
            f"completed:{matched.get('tasks_completed', 0)} "
            f"failed:{matched.get('tasks_failed', 0)}"
        ),
        (
            f"activity=messages_seen:{matched.get('messages_seen', 0)} "
            f"provider_replies:{matched.get('provider_replies', 0)} "
            f"last_active_at={matched.get('last_active_at', '') or 'n/a'}"
        ),
    ]
    if str(matched.get("last_provider_topic", "") or ""):
        lines.append(f"last_provider_topic={matched.get('last_provider_topic', '')}")
    if str(matched.get("last_provider_reply_excerpt", "") or ""):
        lines.append(f"last_provider_reply_excerpt={matched.get('last_provider_reply_excerpt', '')}")
    recent_messages = matched.get("recent_messages", [])
    if isinstance(recent_messages, list) and recent_messages:
        lines.append(
            "recent_messages="
            + "; ".join(
                (
                    f"{str(item.get('from_agent', '') or '')}:"
                    f"{str(item.get('subject', '') or '')}"
                    f" task_id={str(item.get('task_id', '') or 'n/a')}"
                )
                for item in recent_messages
                if isinstance(item, dict)
            )
        )
    pending_task_ids = matched.get("pending_plan_request_task_ids", [])
    if isinstance(pending_task_ids, list) and pending_task_ids:
        lines.append(
            f"pending_approvals={int(matched.get('pending_plan_request_count', len(pending_task_ids)) or 0)} "
            + "task_ids="
            + ",".join(str(task_id) for task_id in pending_task_ids if str(task_id))
        )
    recent_lead_messages = matched.get("recent_lead_messages", [])
    if isinstance(recent_lead_messages, list) and recent_lead_messages:
        lines.append(
            "recent_lead_messages="
            + "; ".join(
                (
                    f"{str(item.get('sender', '') or '')}"
                    f"->{str(item.get('recipient', '') or '')}:"
                    f"{str(item.get('subject', '') or '')}"
                    f" task_id={str(item.get('task_id', '') or 'n/a')}"
                )
                + (
                    f" body={str(item.get('body_preview', '') or '')}"
                    if str(item.get("body_preview", "") or "")
                    else ""
                )
                for item in recent_lead_messages
                if isinstance(item, dict)
            )
        )
    return lines


def describe_teammate_review(snapshot: Dict[str, Any], agent_name: str) -> List[str]:
    teammate_sessions = snapshot.get("teammate_sessions", [])
    if not isinstance(teammate_sessions, list):
        teammate_sessions = []
    matched = next(
        (
            item
            for item in teammate_sessions
            if isinstance(item, dict) and str(item.get("agent", "") or "") == str(agent_name or "")
        ),
        None,
    )
    if matched is None:
        return [f"unknown teammate: {agent_name}"]

    pending_requests = [
        item
        for item in snapshot.get("plan_approval_requests", [])
        if isinstance(item, dict)
        and str(item.get("status", "") or "") == "pending"
        and str(item.get("requested_by", "") or "") == str(agent_name or "")
    ]
    pending_task_ids = [
        str(item.get("task_id", "") or "")
        for item in pending_requests
        if str(item.get("task_id", "") or "")
    ]
    lines = describe_teammate_session(snapshot=snapshot, agent_name=agent_name)
    if pending_task_ids:
        lines.append(f"review_pending_approvals={len(pending_task_ids)} task_ids={','.join(pending_task_ids)}")
        for item in pending_requests:
            proposed_task_ids = item.get("proposed_task_ids", [])
            proposed_dependency_ids = item.get("proposed_dependency_ids", [])
            lines.append(
                f"pending_request={str(item.get('task_id', '') or '')}"
                f"[{str(item.get('task_type', '') or 'unknown')}]"
                + " proposed_tasks="
                + (
                    ",".join(str(task_id) for task_id in proposed_task_ids if str(task_id))
                    if isinstance(proposed_task_ids, list) and proposed_task_ids
                    else "none"
                )
                + " proposed_dependencies="
                + (
                    ",".join(str(task_id) for task_id in proposed_dependency_ids if str(task_id))
                    if isinstance(proposed_dependency_ids, list) and proposed_dependency_ids
                    else "none"
                )
            )
    else:
        lines.append("review_pending_approvals=none")
    recent_lead_messages = matched.get("recent_lead_messages", [])
    if not isinstance(recent_lead_messages, list) or not recent_lead_messages:
        lines.append("review_recent_lead_messages=none")
    action_hints = [f"status {agent_name}", f"plan {agent_name}"]
    action_hints.extend(f"show {task_id}" for task_id in pending_task_ids[:3])
    action_hints.extend(
        [
            f"approve teammate {agent_name}",
            f"reject teammate {agent_name}",
        ]
    )
    lines.append("suggested_actions=" + " | ".join(action_hints))
    return lines


def describe_pending_teammate_reviews(snapshot: Dict[str, Any]) -> List[str]:
    pending_requests = [
        item
        for item in snapshot.get("plan_approval_requests", [])
        if isinstance(item, dict) and str(item.get("status", "") or "") == "pending"
    ]
    if not pending_requests:
        return ["pending_teammate_reviews=none"]
    grouped: Dict[str, List[Dict[str, Any]]] = {}
    for item in pending_requests:
        agent_name = str(item.get("requested_by", "") or "")
        if not agent_name:
            continue
        grouped.setdefault(agent_name, []).append(item)
    if not grouped:
        return ["pending_teammate_reviews=none"]
    lines = [
        "pending_teammate_reviews="
        + ",".join(
            f"{agent_name}:{len(grouped.get(agent_name, []))}"
            for agent_name in sorted(grouped.keys())
        )
    ]
    for agent_name in sorted(grouped.keys()):
        task_ids = [
            str(item.get("task_id", "") or "")
            for item in grouped.get(agent_name, [])
            if str(item.get("task_id", "") or "")
        ]
        lines.append(
            f"pending_teammate={agent_name} task_ids={','.join(task_ids) or 'none'} "
            f"next=review teammate {agent_name} | approve teammate {agent_name} | reject teammate {agent_name}"
        )
    return lines


def next_pending_review_agent(snapshot: Dict[str, Any]) -> str:
    pending_review_agents = snapshot.get("pending_review_agents", [])
    if isinstance(pending_review_agents, list):
        for item in pending_review_agents:
            agent_name = str(item or "").strip()
            if agent_name:
                return agent_name
    pending_requests = [
        item
        for item in snapshot.get("plan_approval_requests", [])
        if isinstance(item, dict) and str(item.get("status", "") or "") == "pending"
    ]
    for item in pending_requests:
        agent_name = str(item.get("requested_by", "") or "").strip()
        if agent_name:
            return agent_name
    return ""


def build_status_lines(
    output_dir: pathlib.Path,
    snapshot: Dict[str, Any],
    max_messages: int = 6,
    max_commands: int = 6,
) -> List[str]:
    lines: List[str] = []
    command_path = resolve_command_path(output_dir=output_dir, snapshot=snapshot)
    lines.append(f"Lead Console: {output_dir}")
    if not snapshot:
        lines.append("- Lead interaction snapshot: not available yet")
        lines.append(f"- Command channel: {command_path}")
        return lines

    pending = [
        item
        for item in snapshot.get("plan_approval_requests", [])
        if isinstance(item, dict) and str(item.get("status", "") or "") == "pending"
    ]
    lines.append(f"- Generated at: {snapshot.get('generated_at', '') or 'n/a'}")
    lines.append(f"- Lead: {snapshot.get('lead_name', 'lead')}")
    lines.append(
        f"- Pending approvals: {snapshot.get('pending_plan_approval_count', 0)} "
        f"of {snapshot.get('plan_approval_request_count', 0)}"
    )
    pending_review_agents = snapshot.get("pending_review_agents", [])
    if isinstance(pending_review_agents, list):
        lines.append(
            "- Pending review agents: "
            + (",".join(str(item) for item in pending_review_agents if str(item)) or "none")
            + f" next={snapshot.get('next_pending_review_agent', '') or 'none'}"
        )
    lines.append(
        f"- Recent messages: {snapshot.get('recent_team_message_count', 0)} "
        f"recent commands: {len(snapshot.get('recent_commands', []))}"
    )
    lines.append(
        f"- Command channel: {command_path} "
        f"(cursor={snapshot.get('command_cursor', 0)} "
        f"last_command_at={snapshot.get('last_command_at', '') or 'n/a'})"
    )
    lines.append(
        f"- Teammate sessions: {snapshot.get('teammate_session_count', 0)} "
        f"active={snapshot.get('active_teammate_session_count', 0)}"
    )

    lines.append("")
    lines.append("Teammate sessions:")
    teammate_sessions = snapshot.get("teammate_sessions", [])
    if not isinstance(teammate_sessions, list) or not teammate_sessions:
        lines.append("- none")
    else:
        for item in teammate_sessions:
            if not isinstance(item, dict):
                continue
            lines.append(
                f"- {item.get('summary', '')} "
                f"last_active_at={item.get('last_active_at', '') or 'n/a'}"
            )

    lines.append("")
    lines.append("Pending approvals:")
    if not pending:
        lines.append("- none")
    else:
        for item in pending:
            lines.append(
                f"- {item.get('task_id', '')} ({item.get('task_type', '')}) "
                f"requested_by={item.get('requested_by', '')} "
                f"proposed_tasks={','.join(item.get('proposed_task_ids', [])) or 'none'} "
                f"proposed_dependencies={','.join(item.get('proposed_dependency_ids', [])) or 'none'}"
            )
            task_preview = [
                _task_preview_text(preview)
                for preview in item.get("proposed_tasks_preview", [])
                if isinstance(preview, dict)
            ]
            dependency_preview = [
                _dependency_preview_text(preview)
                for preview in item.get("proposed_dependencies_preview", [])
                if isinstance(preview, dict)
            ]
            if task_preview:
                lines.append(f"  task_preview: {'; '.join(task_preview)}")
            if dependency_preview:
                lines.append(f"  dependency_preview: {'; '.join(dependency_preview)}")
    lines.append("")
    lines.append("Pending teammate reviews:")
    for line in describe_pending_teammate_reviews(snapshot=snapshot):
        lines.append(f"- {line}")

    lines.append("")
    lines.append("Recent team messages:")
    recent_messages = snapshot.get("recent_team_messages", [])
    if not isinstance(recent_messages, list) or not recent_messages:
        lines.append("- none")
    else:
        for item in recent_messages[-max(1, int(max_messages)) :]:
            if not isinstance(item, dict):
                continue
            lines.append(
                f"- [{item.get('event_index', '')}] {item.get('sender', '')} -> {item.get('recipient', '')}: "
                f"{item.get('subject', '')} task_id={item.get('task_id', '') or 'n/a'}"
            )
            if str(item.get("body_preview", "") or ""):
                lines.append(f"  body_preview: {item.get('body_preview', '')}")

    lines.append("")
    lines.append("Recent commands:")
    recent_commands = snapshot.get("recent_commands", [])
    if not isinstance(recent_commands, list) or not recent_commands:
        lines.append("- none")
    else:
        for item in recent_commands[-max(1, int(max_commands)) :]:
            if not isinstance(item, dict):
                continue
            lines.append(
                f"- [{item.get('line_index', '')}] source={item.get('source', 'unknown')} "
                f"{item.get('command', '') or 'invalid'} "
                f"agent={item.get('agent', '') or 'n/a'} "
                f"task_ids={','.join(item.get('task_ids', [])) or 'none'} "
                f"valid={item.get('valid', False)}"
            )
    return lines


def send_requested_commands(args: argparse.Namespace, output_dir: pathlib.Path) -> int:
    snapshot = load_snapshot(output_dir=output_dir)
    command_path = resolve_command_path(output_dir=output_dir, snapshot=snapshot)
    approve_task_ids = [str(task_id) for task_id in args.approve_plan if str(task_id)]
    approve_teammates = [str(agent) for agent in args.approve_teammate if str(agent)]
    reject_task_ids = [str(task_id) for task_id in args.reject_plan if str(task_id)]
    reject_teammates = [str(agent) for agent in args.reject_teammate if str(agent)]
    request_plan_agents = [str(agent) for agent in args.request_plan if str(agent)]
    request_status_agents = [str(agent) for agent in args.request_status if str(agent)]
    overlap = sorted(set(approve_task_ids) & set(reject_task_ids))
    if overlap:
        print(
            "[lead-console] overlapping approve/reject task ids: " + ", ".join(overlap),
            file=sys.stderr,
        )
        return 2
    agent_overlap = sorted(set(approve_teammates) & set(reject_teammates))
    if agent_overlap:
        print(
            "[lead-console] overlapping approve/reject teammate ids: " + ", ".join(agent_overlap),
            file=sys.stderr,
        )
        return 2

    wrote_any = False
    for task_id in approve_task_ids:
        append_command(command_path, {"command": "approve_plan", "task_id": task_id})
        wrote_any = True
    for agent in approve_teammates:
        append_command(command_path, {"command": "approve_teammate_plans", "agent": agent})
        wrote_any = True
    for task_id in reject_task_ids:
        append_command(command_path, {"command": "reject_plan", "task_id": task_id})
        wrote_any = True
    for agent in reject_teammates:
        append_command(command_path, {"command": "reject_teammate_plans", "agent": agent})
        wrote_any = True
    if args.approve_all_pending_plans:
        append_command(command_path, {"command": "approve_all_pending_plans"})
        wrote_any = True
    for agent in request_status_agents:
        append_command(command_path, {"command": "request_teammate_status", "agent": agent})
        wrote_any = True
    for agent in request_plan_agents:
        append_command(command_path, {"command": "request_teammate_plan", "agent": agent})
        wrote_any = True
    if not wrote_any:
        return 0
    print(f"[lead-console] wrote commands to {command_path}")
    return 0


def interactive_loop(args: argparse.Namespace, output_dir: pathlib.Path) -> int:
    current_review_agent = ""
    help_text = (
        "Commands: refresh | show <task_id> | teammate <agent> | show teammate <agent> | review pending | review next | review teammate <agent> | status <agent> | plan <agent> | approve <task_id> | approve teammate <agent> | approve current | reject <task_id> | reject teammate <agent> | reject current | approve-all | quit"
    )
    while True:
        snapshot = load_snapshot(output_dir=output_dir)
        print("\n".join(build_status_lines(output_dir=output_dir, snapshot=snapshot)))
        if current_review_agent:
            print(f"Current review focus: {current_review_agent}")
        print(help_text)
        try:
            raw = input("lead-console> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("")
            return 0
        if not raw or raw == "refresh":
            continue
        if raw in {"q", "quit", "exit"}:
            return 0
        if raw == "approve-all":
            append_command(
                resolve_command_path(output_dir=output_dir, snapshot=snapshot),
                {"command": "approve_all_pending_plans"},
            )
            continue
        if raw.startswith("show "):
            if raw.startswith("show teammate "):
                agent = raw.split(" ", 2)[2].strip()
                if agent:
                    for line in describe_teammate_session(snapshot=snapshot, agent_name=agent):
                        print(f"[lead-console] {line}")
                    continue
            task_id = raw.split(" ", 1)[1].strip()
            if task_id:
                for line in describe_pending_request(snapshot=snapshot, task_id=task_id):
                    print(f"[lead-console] {line}")
                continue
        if raw.startswith("teammate "):
            agent = raw.split(" ", 1)[1].strip()
            if agent:
                for line in describe_teammate_session(snapshot=snapshot, agent_name=agent):
                    print(f"[lead-console] {line}")
                continue
        if raw.startswith("review teammate "):
            agent = raw.split(" ", 2)[2].strip()
            if agent:
                current_review_agent = agent
                for line in describe_teammate_review(snapshot=snapshot, agent_name=agent):
                    print(f"[lead-console] {line}")
                continue
        if raw == "review pending":
            for line in describe_pending_teammate_reviews(snapshot=snapshot):
                print(f"[lead-console] {line}")
            continue
        if raw == "review next":
            next_agent = next_pending_review_agent(snapshot=snapshot)
            if not next_agent:
                print("[lead-console] no pending teammate review available")
                continue
            current_review_agent = next_agent
            for line in describe_teammate_review(snapshot=snapshot, agent_name=next_agent):
                print(f"[lead-console] {line}")
            continue
        if raw.startswith("status "):
            agent = raw.split(" ", 1)[1].strip()
            if agent:
                append_command(
                    resolve_command_path(output_dir=output_dir, snapshot=snapshot),
                    {"command": "request_teammate_status", "agent": agent},
                )
                continue
        if raw.startswith("plan "):
            agent = raw.split(" ", 1)[1].strip()
            if agent:
                append_command(
                    resolve_command_path(output_dir=output_dir, snapshot=snapshot),
                    {"command": "request_teammate_plan", "agent": agent},
                )
                continue
        if raw.startswith("approve "):
            if raw == "approve current":
                if not current_review_agent:
                    print("[lead-console] no current review focus", file=sys.stderr)
                    continue
                append_command(
                    resolve_command_path(output_dir=output_dir, snapshot=snapshot),
                    {"command": "approve_teammate_plans", "agent": current_review_agent},
                )
                continue
            if raw.startswith("approve teammate "):
                agent = raw.split(" ", 2)[2].strip()
                if agent:
                    append_command(
                        resolve_command_path(output_dir=output_dir, snapshot=snapshot),
                        {"command": "approve_teammate_plans", "agent": agent},
                    )
                    continue
            task_id = raw.split(" ", 1)[1].strip()
            if task_id:
                append_command(
                    resolve_command_path(output_dir=output_dir, snapshot=snapshot),
                    {"command": "approve_plan", "task_id": task_id},
                )
                continue
        if raw.startswith("reject "):
            if raw == "reject current":
                if not current_review_agent:
                    print("[lead-console] no current review focus", file=sys.stderr)
                    continue
                append_command(
                    resolve_command_path(output_dir=output_dir, snapshot=snapshot),
                    {"command": "reject_teammate_plans", "agent": current_review_agent},
                )
                continue
            if raw.startswith("reject teammate "):
                agent = raw.split(" ", 2)[2].strip()
                if agent:
                    append_command(
                        resolve_command_path(output_dir=output_dir, snapshot=snapshot),
                        {"command": "reject_teammate_plans", "agent": agent},
                    )
                    continue
            task_id = raw.split(" ", 1)[1].strip()
            if task_id:
                append_command(
                    resolve_command_path(output_dir=output_dir, snapshot=snapshot),
                    {"command": "reject_plan", "task_id": task_id},
                )
                continue
        print("[lead-console] unsupported command", file=sys.stderr)


def watch_loop(args: argparse.Namespace, output_dir: pathlib.Path) -> int:
    while True:
        snapshot = load_snapshot(output_dir=output_dir)
        print("\n".join(build_status_lines(output_dir=output_dir, snapshot=snapshot)))
        print("")
        try:
            time.sleep(float(args.refresh_seconds))
        except KeyboardInterrupt:
            return 0


def main() -> int:
    args = parse_args()
    if args.refresh_seconds <= 0:
        print("[lead-console] --refresh-seconds must be > 0", file=sys.stderr)
        return 2
    output_dir = pathlib.Path(args.output).resolve()

    send_result = send_requested_commands(args=args, output_dir=output_dir)
    if send_result != 0:
        return send_result
    if args.interactive:
        return interactive_loop(args=args, output_dir=output_dir)
    if args.watch:
        return watch_loop(args=args, output_dir=output_dir)

    snapshot = load_snapshot(output_dir=output_dir)
    lines = build_status_lines(
        output_dir=output_dir,
        snapshot=snapshot,
        max_messages=args.max_messages,
        max_commands=args.max_commands,
    )
    requested_teammates = [str(agent) for agent in args.show_teammate if str(agent)]
    if requested_teammates:
        lines.append("")
        lines.append("Requested teammate details:")
        for agent in requested_teammates:
            for line in describe_teammate_session(snapshot=snapshot, agent_name=agent):
                lines.append(f"- {line}")
    reviewed_teammates = [str(agent) for agent in args.review_teammate if str(agent)]
    if reviewed_teammates:
        lines.append("")
        lines.append("Requested teammate reviews:")
        for agent in reviewed_teammates:
            for line in describe_teammate_review(snapshot=snapshot, agent_name=agent):
                lines.append(f"- {line}")
    if args.review_pending:
        lines.append("")
        lines.append("Requested pending teammate reviews:")
        for line in describe_pending_teammate_reviews(snapshot=snapshot):
            lines.append(f"- {line}")
    if args.review_next:
        lines.append("")
        lines.append("Requested next teammate review:")
        next_agent = next_pending_review_agent(snapshot=snapshot)
        if not next_agent:
            lines.append("- no pending teammate review available")
        else:
            for line in describe_teammate_review(snapshot=snapshot, agent_name=next_agent):
                lines.append(f"- {line}")
    print(
        "\n".join(
            lines
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
