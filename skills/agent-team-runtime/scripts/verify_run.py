#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
import pathlib
import sys
from typing import Dict, List


REQUIRED_FILES = [
    "agent_session_registry.json",
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


def load_json(path: pathlib.Path) -> Dict:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"expected JSON object in {path}")
    return payload


def load_jsonl(path: pathlib.Path) -> List[Dict]:
    payloads: List[Dict] = []
    with path.open("r", encoding="utf-8") as fh:
        for raw_line in fh:
            line = raw_line.strip()
            if not line:
                continue
            payload = json.loads(line)
            if not isinstance(payload, dict):
                raise ValueError(f"expected JSON object entries in {path}")
            payloads.append(payload)
    return payloads


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

    task_board = load_json(output_dir / "task_board.json")
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

    summary = load_json(output_dir / "run_summary.json")
    provider = summary.get("provider", {})
    runtime_config = summary.get("runtime_config", {})
    host = summary.get("host", {})
    workflow = summary.get("workflow", {})
    agent_session_registry_path = pathlib.Path(str(summary.get("agent_session_registry_path", "") or "")).resolve()
    if not agent_session_registry_path.exists():
        return fail("Missing agent session registry artifact reference in run_summary.json")
    agent_session_registry = load_json(agent_session_registry_path)
    if not agent_session_registry:
        return fail("agent_session_registry.json should not be empty")
    registry_summary = summary.get("agent_session_registry_summary", {})
    if not isinstance(registry_summary, dict):
        return fail("agent_session_registry_summary must be an object")
    if int(registry_summary.get("agent_count", 0)) != len(agent_session_registry):
        return fail("agent_session_registry_summary.agent_count does not match registry size")
    if runtime_config.get("teammate_mode") == "tmux":
        diagnostics_path = output_dir / "tmux_worker_diagnostics.jsonl"
        if not diagnostics_path.exists():
            return fail("Missing tmux worker diagnostics artifact")
        diagnostics_lines = diagnostics_path.read_text(encoding="utf-8").splitlines()
        if not diagnostics_lines:
            return fail("tmux_worker_diagnostics.jsonl is empty")

        required_tmux_summary_paths = {
            "tmux_session_recovery_summary_path": {"workers", "recovered", "missing", "inactive", "failed", "skipped"},
            "tmux_session_cleanup_summary_path": {"sessions", "cleaned", "already_exited", "failed", "skipped"},
            "tmux_session_leases_path": set(),
        }
        for summary_key, required_keys in required_tmux_summary_paths.items():
            raw_path = str(summary.get(summary_key, "") or "")
            if not raw_path:
                return fail(f"Missing tmux summary path in run_summary.json: {summary_key}")
            artifact_path = pathlib.Path(raw_path).resolve()
            if not artifact_path.exists():
                return fail(f"Referenced tmux artifact does not exist: {artifact_path}")
            payload = load_json(artifact_path)
            if required_keys and not required_keys.issubset(set(payload.keys())):
                return fail(
                    f"tmux artifact missing required keys: {artifact_path.name} "
                    f"missing={sorted(required_keys - set(payload.keys()))}"
                )
            if summary_key == "tmux_session_leases_path" and not payload:
                return fail("tmux_session_leases.json should not be empty for tmux runs")

        required_tmux_history_paths = {
            "tmux_session_recovery_history_path": {"generated_at", "kind", "resume_from", "interrupted_reason", "summary"},
            "tmux_session_cleanup_history_path": {"generated_at", "kind", "resume_from", "interrupted_reason", "summary"},
        }
        for history_key, required_keys in required_tmux_history_paths.items():
            raw_path = str(summary.get(history_key, "") or "")
            if not raw_path:
                return fail(f"Missing tmux history path in run_summary.json: {history_key}")
            artifact_path = pathlib.Path(raw_path).resolve()
            if not artifact_path.exists():
                return fail(f"Referenced tmux history artifact does not exist: {artifact_path}")
            payloads = load_jsonl(artifact_path)
            if not payloads:
                return fail(f"tmux history artifact is empty: {artifact_path.name}")
            latest_payload = payloads[-1]
            if not required_keys.issubset(set(latest_payload.keys())):
                return fail(
                    f"tmux history artifact missing required keys: {artifact_path.name} "
                    f"missing={sorted(required_keys - set(latest_payload.keys()))}"
                )
            if not isinstance(latest_payload.get("summary"), dict):
                return fail(f"tmux history entry has non-object summary: {artifact_path.name}")

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
