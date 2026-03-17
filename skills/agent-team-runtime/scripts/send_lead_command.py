#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
import pathlib
import sys


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Append a live lead command for agent-team-runtime.")
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
    return parser.parse_args()


def append_command(command_path: pathlib.Path, payload: dict) -> None:
    command_path.parent.mkdir(parents=True, exist_ok=True)
    command_path.touch(exist_ok=True)
    with command_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(payload, ensure_ascii=False) + "\n")


def main() -> int:
    args = parse_args()
    output_dir = pathlib.Path(args.output).resolve()
    command_path = output_dir / "lead_commands.jsonl"

    approve_task_ids = [str(task_id) for task_id in args.approve_plan if str(task_id)]
    approve_teammates = [str(agent) for agent in args.approve_teammate if str(agent)]
    reject_task_ids = [str(task_id) for task_id in args.reject_plan if str(task_id)]
    reject_teammates = [str(agent) for agent in args.reject_teammate if str(agent)]
    request_plan_agents = [str(agent) for agent in args.request_plan if str(agent)]
    request_status_agents = [str(agent) for agent in args.request_status if str(agent)]
    overlap = sorted(set(approve_task_ids) & set(reject_task_ids))
    if overlap:
        print(
            "[lead-command] overlapping approve/reject task ids: " + ", ".join(overlap),
            file=sys.stderr,
        )
        return 2
    agent_overlap = sorted(set(approve_teammates) & set(reject_teammates))
    if agent_overlap:
        print(
            "[lead-command] overlapping approve/reject teammate ids: " + ", ".join(agent_overlap),
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
        print("[lead-command] no command requested", file=sys.stderr)
        return 2

    print(f"[lead-command] wrote commands to {command_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
