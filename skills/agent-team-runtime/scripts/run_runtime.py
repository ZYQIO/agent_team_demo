#!/usr/bin/env python3

from __future__ import annotations

import argparse
import pathlib
import subprocess
import sys
from typing import List


PRESETS = {
    "default": [],
    "static": [
        "--no-dynamic-tasks",
    ],
    "tmux": [
        "--teammate-mode",
        "tmux",
    ],
    "challenge": [
        "--adjudication-accept-threshold",
        "95",
        "--adjudication-challenge-threshold",
        "60",
        "--peer-wait-seconds",
        "2",
        "--evidence-wait-seconds",
        "2",
    ],
    "forced-challenge": [
        "--adjudication-accept-threshold",
        "95",
        "--adjudication-challenge-threshold",
        "0",
        "--peer-wait-seconds",
        "0.01",
        "--evidence-wait-seconds",
        "1",
    ],
    "fast": [
        "--peer-wait-seconds",
        "1",
        "--evidence-wait-seconds",
        "1",
        "--no-auto-round3-on-challenge",
    ],
}


def resolve_repo_root(script_dir: pathlib.Path) -> pathlib.Path:
    direct = script_dir.parents[3]
    runtime_path = direct / "agent_team_runtime.py"
    if runtime_path.exists():
        return direct

    for candidate in [script_dir] + list(script_dir.parents):
        probe = candidate / "agent_team_demo" / "agent_team_runtime.py"
        if probe.exists():
            return candidate / "agent_team_demo"
    raise FileNotFoundError("Cannot locate agent_team_demo/agent_team_runtime.py")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run agent_team_runtime.py with reusable presets.")
    parser.add_argument(
        "--repo-root",
        default="",
        help="Path containing agent_team_runtime.py. Auto-detected when omitted.",
    )
    parser.add_argument("--target", default=".", help="Target directory for markdown analysis.")
    parser.add_argument(
        "--output",
        default="agent_team_demo/output_skill_run",
        help="Output artifact directory.",
    )
    parser.add_argument(
        "--config",
        default="",
        help="Optional JSON config file passed to runtime --config.",
    )
    parser.add_argument(
        "--provider",
        default="heuristic",
        choices=["heuristic", "openai"],
        help="Provider passed to runtime.",
    )
    parser.add_argument(
        "--model",
        default="heuristic-v1",
        help="Model passed to runtime (for OpenAI example: gpt-4.1-mini).",
    )
    parser.add_argument(
        "--host-kind",
        default="",
        help="Optional host adapter kind passed to runtime --host-kind.",
    )
    parser.add_argument(
        "--workflow-pack",
        default="",
        help="Optional workflow pack passed to runtime --workflow-pack.",
    )
    parser.add_argument(
        "--workflow-preset",
        default="",
        help="Optional workflow preset label passed to runtime --workflow-preset.",
    )
    parser.add_argument(
        "--preset",
        default="default",
        choices=sorted(PRESETS.keys()),
        help="Runtime argument preset.",
    )
    parser.add_argument(
        "--goal",
        default="",
        help="Optional custom goal string.",
    )
    parser.add_argument(
        "--resume-from",
        default="",
        help="Optional checkpoint path passed to runtime --resume-from.",
    )
    parser.add_argument(
        "--max-completed-tasks",
        type=int,
        default=0,
        help="Optional early-stop threshold passed to runtime --max-completed-tasks.",
    )
    parser.add_argument(
        "--rewind-to-history-index",
        type=int,
        default=-1,
        help="Optional history index passed to runtime --rewind-to-history-index.",
    )
    parser.add_argument(
        "--rewind-to-event-index",
        type=int,
        default=-1,
        help="Optional event index passed to runtime --rewind-to-event-index.",
    )
    parser.add_argument(
        "--rewind-branch",
        action="store_true",
        help="When rewinding, pass --rewind-branch to runtime.",
    )
    parser.add_argument(
        "--rewind-branch-output",
        default="",
        help="Optional path passed to runtime --rewind-branch-output.",
    )
    parser.add_argument(
        "--history-replay-report",
        action="store_true",
        help="Generate checkpoint replay report and exit.",
    )
    parser.add_argument(
        "--history-replay-report-path",
        default="",
        help="Optional path passed to runtime --history-replay-report-path.",
    )
    parser.add_argument(
        "--history-replay-start-index",
        type=int,
        default=-1,
        help="Optional start index passed to runtime --history-replay-start-index.",
    )
    parser.add_argument(
        "--history-replay-end-index",
        type=int,
        default=-1,
        help="Optional end index passed to runtime --history-replay-end-index.",
    )
    parser.add_argument(
        "--event-replay-report",
        action="store_true",
        help="Generate event replay report and exit.",
    )
    parser.add_argument(
        "--event-replay-report-path",
        default="",
        help="Optional path passed to runtime --event-replay-report-path.",
    )
    parser.add_argument(
        "--event-replay-max-transitions",
        type=int,
        default=0,
        help="Optional limit passed to runtime --event-replay-max-transitions (0 keeps runtime default).",
    )
    parser.add_argument(
        "--tmux-worker-timeout-sec",
        type=int,
        default=0,
        help="Optional timeout passed to runtime --tmux-worker-timeout-sec (0 keeps runtime default).",
    )
    parser.add_argument(
        "--no-tmux-fallback-on-error",
        action="store_true",
        help="Pass --no-tmux-fallback-on-error to runtime.",
    )
    parser.add_argument(
        "--extra-arg",
        action="append",
        default=[],
        help="Extra argument appended to runtime command. Repeat as needed.",
    )
    return parser.parse_args()


def build_runtime_command(args: argparse.Namespace, repo_root: pathlib.Path) -> List[str]:
    runtime_path = repo_root / "agent_team_runtime.py"
    if not runtime_path.exists():
        raise FileNotFoundError(f"Runtime not found: {runtime_path}")

    cmd: List[str] = [
        sys.executable,
        str(runtime_path),
        "--target",
        args.target,
        "--output",
        args.output,
        "--provider",
        args.provider,
    ]
    if args.config:
        cmd.extend(["--config", args.config])
    if args.model:
        cmd.extend(["--model", args.model])
    if args.host_kind:
        cmd.extend(["--host-kind", args.host_kind])
    if args.workflow_pack:
        cmd.extend(["--workflow-pack", args.workflow_pack])
    if args.workflow_preset:
        cmd.extend(["--workflow-preset", args.workflow_preset])
    if args.goal:
        cmd.extend(["--goal", args.goal])
    if args.resume_from:
        cmd.extend(["--resume-from", args.resume_from])
    if args.max_completed_tasks > 0:
        cmd.extend(["--max-completed-tasks", str(args.max_completed_tasks)])
    if args.rewind_to_history_index >= 0:
        cmd.extend(["--rewind-to-history-index", str(args.rewind_to_history_index)])
    if args.rewind_to_event_index >= 0:
        cmd.extend(["--rewind-to-event-index", str(args.rewind_to_event_index)])
    if args.rewind_branch:
        cmd.append("--rewind-branch")
    if args.rewind_branch_output:
        cmd.extend(["--rewind-branch-output", args.rewind_branch_output])
    if args.history_replay_report:
        cmd.append("--history-replay-report")
    if args.history_replay_report_path:
        cmd.extend(["--history-replay-report-path", args.history_replay_report_path])
    if args.history_replay_start_index >= 0:
        cmd.extend(["--history-replay-start-index", str(args.history_replay_start_index)])
    if args.history_replay_end_index >= 0:
        cmd.extend(["--history-replay-end-index", str(args.history_replay_end_index)])
    if args.event_replay_report:
        cmd.append("--event-replay-report")
    if args.event_replay_report_path:
        cmd.extend(["--event-replay-report-path", args.event_replay_report_path])
    if args.event_replay_max_transitions > 0:
        cmd.extend(["--event-replay-max-transitions", str(args.event_replay_max_transitions)])
    if args.tmux_worker_timeout_sec > 0:
        cmd.extend(["--tmux-worker-timeout-sec", str(args.tmux_worker_timeout_sec)])
    if args.no_tmux_fallback_on_error:
        cmd.append("--no-tmux-fallback-on-error")

    cmd.extend(PRESETS[args.preset])
    cmd.extend(args.extra_arg)
    return cmd


def main() -> int:
    args = parse_args()
    script_dir = pathlib.Path(__file__).resolve().parent
    repo_root = (
        pathlib.Path(args.repo_root).resolve()
        if args.repo_root
        else resolve_repo_root(script_dir=script_dir)
    )
    command = build_runtime_command(args=args, repo_root=repo_root)
    print("Running command:")
    print(" ".join(command))
    completed = subprocess.run(command, check=False)
    return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main())
