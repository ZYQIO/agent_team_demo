#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
import pathlib
import sys
from typing import Dict, List


REQUIRED_FILES = [
    "final_report.md",
    "task_board.json",
    "events.jsonl",
    "shared_state.json",
    "file_locks.json",
    "run_summary.json",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Verify runtime artifact integrity.")
    parser.add_argument("--output", required=True, help="Runtime output directory path.")
    parser.add_argument(
        "--require-evidence-events",
        action="store_true",
        help="Require evidence loop events (challenge flow).",
    )
    return parser.parse_args()


def load_events(path: pathlib.Path) -> List[Dict]:
    events: List[Dict] = []
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                events.append(json.loads(line))
    return events


def fail(message: str) -> int:
    print(f"[verify] FAIL: {message}")
    return 1


def main() -> int:
    args = parse_args()
    output_dir = pathlib.Path(args.output).resolve()
    if not output_dir.exists():
        return fail(f"Output directory does not exist: {output_dir}")

    missing = [name for name in REQUIRED_FILES if not (output_dir / name).exists()]
    if missing:
        return fail(f"Missing required files: {missing}")

    task_board = json.loads((output_dir / "task_board.json").read_text(encoding="utf-8"))
    tasks = task_board.get("tasks", [])
    failed_tasks = [task for task in tasks if task.get("status") == "failed"]
    incomplete = [task for task in tasks if task.get("status") != "completed"]
    if failed_tasks:
        return fail(f"Found failed tasks: {[task.get('task_id') for task in failed_tasks]}")
    if incomplete:
        return fail(f"Found non-completed tasks: {[task.get('task_id') for task in incomplete]}")

    events = load_events(output_dir / "events.jsonl")
    event_indices = [item.get("event_index") for item in events]
    if not all(isinstance(idx, int) for idx in event_indices):
        return fail("events.jsonl is missing integer event_index fields")
    for offset, idx in enumerate(event_indices):
        if idx != offset:
            return fail(f"event_index sequence is not contiguous at offset={offset}, value={idx}")
    event_names = {item.get("event") for item in events}
    must_have = {"run_started", "lead_adjudication_published", "run_finished"}
    missing_events = sorted(must_have - event_names)
    if missing_events:
        return fail(f"Missing required events: {missing_events}")

    if args.require_evidence_events:
        evidence_events = {"evidence_round_started", "lead_re_adjudication_published"}
        missing_evidence = sorted(evidence_events - event_names)
        if missing_evidence:
            return fail(
                "Missing evidence-loop events. Run with a challenge-oriented preset. "
                f"Missing={missing_evidence}"
            )

    report = (output_dir / "final_report.md").read_text(encoding="utf-8")
    for section in ["## Peer Challenge Round", "## Evidence Pack", "## Lead Adjudication"]:
        if section not in report:
            return fail(f"Missing report section: {section}")

    summary = json.loads((output_dir / "run_summary.json").read_text(encoding="utf-8"))
    provider = summary.get("provider", {})
    runtime_config = summary.get("runtime_config", {})
    host = summary.get("host", {})
    workflow = summary.get("workflow", {})
    print("[verify] PASS")
    print(f"[verify] output={output_dir}")
    print(f"[verify] tasks={len(tasks)}")
    print(
        f"[verify] provider={provider.get('provider')} model={provider.get('model')} "
        f"mode={provider.get('mode')}"
    )
    if host:
        print(
            f"[verify] host={host.get('kind')} transport={host.get('session_transport')}"
        )
    if workflow:
        print(
            f"[verify] workflow={workflow.get('pack')} preset={workflow.get('preset')}"
        )
    print(
        f"[verify] thresholds: accept={runtime_config.get('adjudication_accept_threshold')} "
        f"challenge={runtime_config.get('adjudication_challenge_threshold')}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
