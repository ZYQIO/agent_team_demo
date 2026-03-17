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
        "--reject-plan",
        action="append",
        default=[],
        help="Reject a pending plan by task id. Can be specified multiple times.",
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
    reject_task_ids = [str(task_id) for task_id in args.reject_plan if str(task_id)]
    request_plan_agents = [str(agent) for agent in args.request_plan if str(agent)]
    request_status_agents = [str(agent) for agent in args.request_status if str(agent)]
    overlap = sorted(set(approve_task_ids) & set(reject_task_ids))
    if overlap:
        print(
            "[lead-console] overlapping approve/reject task ids: " + ", ".join(overlap),
            file=sys.stderr,
        )
        return 2

    wrote_any = False
    for task_id in approve_task_ids:
        append_command(command_path, {"command": "approve_plan", "task_id": task_id})
        wrote_any = True
    for task_id in reject_task_ids:
        append_command(command_path, {"command": "reject_plan", "task_id": task_id})
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
    help_text = (
        "Commands: refresh | show <task_id> | status <agent> | plan <agent> | approve <task_id> | reject <task_id> | approve-all | quit"
    )
    while True:
        snapshot = load_snapshot(output_dir=output_dir)
        print("\n".join(build_status_lines(output_dir=output_dir, snapshot=snapshot)))
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
            task_id = raw.split(" ", 1)[1].strip()
            if task_id:
                for line in describe_pending_request(snapshot=snapshot, task_id=task_id):
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
            task_id = raw.split(" ", 1)[1].strip()
            if task_id:
                append_command(
                    resolve_command_path(output_dir=output_dir, snapshot=snapshot),
                    {"command": "approve_plan", "task_id": task_id},
                )
                continue
        if raw.startswith("reject "):
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
    print(
        "\n".join(
            build_status_lines(
                output_dir=output_dir,
                snapshot=snapshot,
                max_messages=args.max_messages,
                max_commands=args.max_commands,
            )
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
