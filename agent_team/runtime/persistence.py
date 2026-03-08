from __future__ import annotations

import datetime as dt
import json
import pathlib
from typing import Any, Dict, List, Optional, Sequence

from ..config import RuntimeConfig
from ..core import (
    EventLogger,
    FileLockRegistry,
    Mailbox,
    SharedState,
    Task,
    TaskBoard,
    task_from_dict,
    utc_now,
)
from ..models import ProviderMetadata


CHECKPOINT_VERSION = 1
CHECKPOINT_FILENAME = "run_checkpoint.json"
CHECKPOINT_HISTORY_DIRNAME = "_checkpoint_history"


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
    del mailbox
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

    tmux_cleanup_summary_path = output_dir / "tmux_session_cleanup_summary.json"
    tmux_cleanup_summary = state_snapshot.get("tmux_session_cleanup_summary", {})
    tmux_cleanup_summary_path_str = ""
    if isinstance(tmux_cleanup_summary, dict) and tmux_cleanup_summary:
        tmux_cleanup_summary_path.write_text(
            json.dumps(tmux_cleanup_summary, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        tmux_cleanup_summary_path_str = str(tmux_cleanup_summary_path)

    tmux_recovery_summary_path = output_dir / "tmux_session_recovery_summary.json"
    tmux_recovery_summary = state_snapshot.get("tmux_session_recovery_summary", {})
    tmux_recovery_summary_path_str = ""
    if isinstance(tmux_recovery_summary, dict) and tmux_recovery_summary:
        tmux_recovery_summary_path.write_text(
            json.dumps(tmux_recovery_summary, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        tmux_recovery_summary_path_str = str(tmux_recovery_summary_path)

    tmux_session_leases_path = output_dir / "tmux_session_leases.json"
    tmux_session_leases = state_snapshot.get("tmux_session_leases", {})
    tmux_session_leases_path_str = ""
    if isinstance(tmux_session_leases, dict) and tmux_session_leases:
        tmux_session_leases_path.write_text(
            json.dumps(tmux_session_leases, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        tmux_session_leases_path_str = str(tmux_session_leases_path)

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
        "tmux_session_cleanup_summary_path": tmux_cleanup_summary_path_str,
        "tmux_session_recovery_summary_path": tmux_recovery_summary_path_str,
        "tmux_session_leases_path": tmux_session_leases_path_str,
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
