from __future__ import annotations

import datetime as dt
import json
import pathlib
from typing import Any, Dict, List, Mapping, Optional, Sequence

from ..config import RuntimeConfig
from ..host import build_host_enforcement_snapshot
from ..core import (
    EventLogger,
    FileLockRegistry,
    HOOK_EVENT_TEAMMATE_IDLE,
    Mailbox,
    SharedState,
    Task,
    TaskBoard,
    task_from_dict,
    utc_now,
)
from ..models import ProviderMetadata
from .lead_interaction import (
    LEAD_INTERACTION_STATE_KEY,
    LEAD_PLAN_REPLY_SUBJECT,
    LEAD_PLAN_REQUEST_SUBJECT,
    LEAD_STATUS_REPLY_SUBJECT,
    LEAD_STATUS_REQUEST_SUBJECT,
    PLAN_APPROVAL_STATUS_PENDING,
)
from .session_state import (
    SESSION_BOUNDARY_FILENAME,
    TEAMMATE_SESSIONS_FILENAME,
    build_session_boundary_snapshot,
    build_teammate_sessions_snapshot,
)


CHECKPOINT_VERSION = 1
CHECKPOINT_FILENAME = "run_checkpoint.json"
CHECKPOINT_HISTORY_DIRNAME = "_checkpoint_history"
CONTEXT_BOUNDARY_FILENAME = "context_boundaries.json"
HOST_ENFORCEMENT_FILENAME = "host_enforcement.json"
LEAD_INTERACTION_FILENAME = "lead_interaction.json"
LEAD_INTERACTION_REPORT_FILENAME = "lead_interaction.md"
TEAM_PROGRESS_FILENAME = "team_progress.json"
TEAM_PROGRESS_REPORT_FILENAME = "team_progress.md"


def _lead_message_body_preview(subject: str, body: Any) -> str:
    text = str(body or "").strip()
    if not text:
        return ""
    if str(subject or "") == LEAD_STATUS_REPLY_SUBJECT:
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            return text[:200]
        if isinstance(payload, Mapping):
            summary = str(payload.get("summary", "") or "").strip()
            if summary:
                return summary[:200]
        return text[:200]
    if str(subject or "") == LEAD_PLAN_REPLY_SUBJECT:
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            return text[:200]
        if isinstance(payload, Mapping):
            summary = str(payload.get("summary", "") or "").strip()
            if summary:
                return summary[:200]
        return text[:200]
    if str(subject or "") == LEAD_PLAN_REQUEST_SUBJECT:
        return "plan requested"
    if str(subject or "") == LEAD_STATUS_REQUEST_SUBJECT:
        return "status requested"
    return ""


def _build_lead_teammate_session_summary(session: Mapping[str, Any]) -> Dict[str, Any]:
    task_history = session.get("task_history", [])
    if not isinstance(task_history, list):
        task_history = []
    last_task = task_history[-1] if task_history else {}
    if not isinstance(last_task, Mapping):
        last_task = {}
    provider_memory = session.get("provider_memory", [])
    if not isinstance(provider_memory, list):
        provider_memory = []
    last_memory = provider_memory[-1] if provider_memory else {}
    if not isinstance(last_memory, Mapping):
        last_memory = {}
    recent_messages = session.get("recent_messages", [])
    if not isinstance(recent_messages, list):
        recent_messages = []
    current_task_id = str(session.get("current_task_id", "") or "")
    current_task_type = str(session.get("current_task_type", "") or "")
    current_task_label = current_task_id or "none"
    if current_task_id and current_task_type:
        current_task_label = f"{current_task_id}[{current_task_type}]"
    last_task_id = str(last_task.get("task_id", "") or "")
    last_task_status = str(last_task.get("status", "") or "")
    last_task_label = "none"
    if last_task_id:
        last_task_label = f"{last_task_id}({last_task_status or 'unknown'})"
    transport = str(session.get("transport", "") or "unknown")
    transport_backend = str(session.get("transport_backend", "") or "")
    last_reply_excerpt = str(last_memory.get("reply", "") or "").strip().replace("\n", " ")
    if len(last_reply_excerpt) > 160:
        last_reply_excerpt = last_reply_excerpt[:157] + "..."
    summary = (
        f"{str(session.get('agent', '') or '')} "
        f"status={str(session.get('status', '') or 'unknown')} "
        f"current={current_task_label} "
        f"last={last_task_label} "
        f"transport={transport}"
    )
    if transport_backend:
        summary += f" backend={transport_backend}"
    return {
        "agent": str(session.get("agent", "") or ""),
        "agent_type": str(session.get("agent_type", "") or ""),
        "transport": transport,
        "transport_backend": transport_backend,
        "status": str(session.get("status", "") or ""),
        "current_task_id": current_task_id,
        "current_task_type": current_task_type,
        "last_task_id": last_task_id,
        "last_task_type": str(last_task.get("task_type", "") or ""),
        "last_task_status": last_task_status,
        "tasks_started": int(session.get("tasks_started", 0) or 0),
        "tasks_completed": int(session.get("tasks_completed", 0) or 0),
        "tasks_failed": int(session.get("tasks_failed", 0) or 0),
        "messages_seen": int(session.get("messages_seen", 0) or 0),
        "provider_replies": int(session.get("provider_replies", 0) or 0),
        "last_provider_topic": str(last_memory.get("topic", "") or ""),
        "last_provider_reply_excerpt": last_reply_excerpt,
        "last_active_at": str(session.get("last_active_at", "") or ""),
        "recent_messages": [
            {
                "from_agent": str(item.get("from_agent", "") or ""),
                "subject": str(item.get("subject", "") or ""),
                "task_id": str(item.get("task_id", "") or ""),
                "recorded_at": str(item.get("recorded_at", "") or ""),
            }
            for item in recent_messages
            if isinstance(item, Mapping)
        ],
        "summary": summary,
    }


def _build_lead_teammate_sessions_snapshot(shared_state: SharedState) -> Dict[str, Any]:
    sessions_snapshot = build_teammate_sessions_snapshot(shared_state=shared_state)
    summaries = [
        _build_lead_teammate_session_summary(session)
        for session in sessions_snapshot.get("sessions", [])
        if isinstance(session, Mapping)
    ]
    active_count = sum(
        1
        for item in summaries
        if str(item.get("status", "") or "") == "running" or str(item.get("current_task_id", "") or "")
    )
    return {
        "teammate_session_count": len(summaries),
        "active_teammate_session_count": active_count,
        "teammate_session_status_counts": dict(sessions_snapshot.get("status_counts", {}))
        if isinstance(sessions_snapshot.get("status_counts", {}), Mapping)
        else {},
        "teammate_session_transport_counts": dict(sessions_snapshot.get("transport_counts", {}))
        if isinstance(sessions_snapshot.get("transport_counts", {}), Mapping)
        else {},
        "teammate_sessions": summaries,
    }


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
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return source_output_dir / "branches" / f"rewind_{history_index:06d}_{stamp}"


def default_event_rewind_branch_output_dir(source_output_dir: pathlib.Path, event_index: int) -> pathlib.Path:
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
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


def _team_profiles_from_state_snapshot(state_snapshot: Dict[str, Any]) -> List[Dict[str, Any]]:
    lead_name = str(state_snapshot.get("lead_name", "lead") or "lead")
    profiles: List[Dict[str, Any]] = [
        {
            "name": lead_name,
            "agent_type": "lead",
            "skills": ["lead"],
        }
    ]
    seen = {lead_name}
    raw_profiles = state_snapshot.get("team_profiles", [])
    if not isinstance(raw_profiles, list):
        return profiles
    for item in raw_profiles:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name", "") or "")
        if not name or name in seen:
            continue
        raw_skills = item.get("skills", [])
        skills = [str(skill) for skill in raw_skills] if isinstance(raw_skills, list) else []
        profiles.append(
            {
                "name": name,
                "agent_type": str(item.get("agent_type", "general") or "general"),
                "skills": skills,
            }
        )
        seen.add(name)
    return profiles


def build_context_boundary_summary(
    logger: EventLogger,
) -> Dict[str, Any]:
    summary = {
        "generated_at": utc_now(),
        "context_count": 0,
        "agents": {},
        "records": [],
    }
    if not logger.path.exists():
        return summary
    with logger.path.open("r", encoding="utf-8") as fh:
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
            if str(payload.get("event", "") or "") != "task_context_prepared":
                continue
            agent = str(payload.get("agent", "") or "")
            transport = str(payload.get("transport", "") or "")
            task_type = str(payload.get("task_type", "") or "")
            visible_keys = payload.get("visible_shared_state_keys", [])
            if not isinstance(visible_keys, list):
                visible_keys = []
            record = {
                "event_index": int(payload.get("event_index", -1)),
                "task_id": str(payload.get("task_id", "") or ""),
                "task_type": task_type,
                "agent": agent,
                "transport": transport,
                "scope": str(payload.get("scope", "") or ""),
                "visible_shared_state_keys": [str(item) for item in visible_keys],
                "visible_shared_state_key_count": int(payload.get("visible_shared_state_key_count", 0) or 0),
                "omitted_shared_state_key_count": int(payload.get("omitted_shared_state_key_count", 0) or 0),
                "dependency_task_ids": [str(item) for item in payload.get("dependency_task_ids", [])],
            }
            summary["records"].append(record)
            summary["context_count"] += 1
            bucket = summary["agents"].setdefault(
                agent,
                {
                    "agent": agent,
                    "context_count": 0,
                    "task_types": [],
                    "transports": [],
                    "max_visible_shared_state_key_count": 0,
                },
            )
            bucket["context_count"] += 1
            if task_type and task_type not in bucket["task_types"]:
                bucket["task_types"].append(task_type)
            if transport and transport not in bucket["transports"]:
                bucket["transports"].append(transport)
            bucket["max_visible_shared_state_key_count"] = max(
                int(bucket.get("max_visible_shared_state_key_count", 0)),
                record["visible_shared_state_key_count"],
            )
    summary["agents"] = {
        name: {
            **data,
            "task_types": sorted(data.get("task_types", [])),
            "transports": sorted(data.get("transports", [])),
        }
        for name, data in sorted(summary["agents"].items())
    }
    return summary


def _task_matches_profile(task_payload: Dict[str, Any], profile: Dict[str, Any]) -> bool:
    required_skills = {str(skill) for skill in task_payload.get("required_skills", [])}
    allowed_agent_types = {str(item) for item in task_payload.get("allowed_agent_types", [])}
    profile_skills = {str(skill) for skill in profile.get("skills", [])}
    profile_agent_type = str(profile.get("agent_type", "general") or "general")
    if required_skills and not required_skills.issubset(profile_skills):
        return False
    if allowed_agent_types and profile_agent_type not in allowed_agent_types:
        return False
    return True


def _empty_team_progress_rollup() -> Dict[str, Any]:
    return {
        "messages_sent": 0,
        "messages_received": 0,
        "tasks_claimed": 0,
        "tasks_completed": 0,
        "tasks_failed": 0,
        "tasks_deferred": 0,
        "idle_ticks": 0,
        "last_event_at": "",
    }


def _update_last_event_at(rollup: Dict[str, Any], payload: Dict[str, Any]) -> None:
    ts = str(payload.get("ts", "") or "")
    if ts:
        rollup["last_event_at"] = ts


def _load_team_progress_event_rollups(
    logger_path: Optional[pathlib.Path],
    team_names: Sequence[str],
) -> Dict[str, Dict[str, Any]]:
    rollups = {name: _empty_team_progress_rollup() for name in team_names}
    if logger_path is None or not logger_path.exists() or not logger_path.is_file():
        return rollups
    with logger_path.open("r", encoding="utf-8") as fh:
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
            event_name = str(payload.get("event", "") or "")
            sender = str(payload.get("sender", "") or "")
            recipient = str(payload.get("recipient", "") or "")
            agent = str(payload.get("agent", "") or "")
            owner = str(payload.get("owner", "") or "")
            from_agent = str(payload.get("from_agent", "") or "")

            if sender in rollups:
                if event_name == "mail_sent":
                    rollups[sender]["messages_sent"] += 1
                _update_last_event_at(rollups[sender], payload)
            if recipient in rollups:
                if event_name == "mail_sent":
                    rollups[recipient]["messages_received"] += 1
                _update_last_event_at(rollups[recipient], payload)
            if agent in rollups:
                if event_name == "task_claimed":
                    rollups[agent]["tasks_claimed"] += 1
                elif event_name == "task_deferred":
                    rollups[agent]["tasks_deferred"] += 1
                elif event_name == HOOK_EVENT_TEAMMATE_IDLE:
                    rollups[agent]["idle_ticks"] += 1
                _update_last_event_at(rollups[agent], payload)
            if owner in rollups:
                if event_name == "task_completed":
                    rollups[owner]["tasks_completed"] += 1
                elif event_name == "task_failed":
                    rollups[owner]["tasks_failed"] += 1
                _update_last_event_at(rollups[owner], payload)
            if from_agent in rollups:
                _update_last_event_at(rollups[from_agent], payload)
    return rollups


def build_team_progress_snapshot(
    board: TaskBoard,
    shared_state: SharedState,
    logger: Optional[EventLogger] = None,
) -> Dict[str, Any]:
    board_snapshot = board.snapshot()
    state_snapshot = shared_state.snapshot()
    team_snapshot = state_snapshot.get("team", {})
    if not isinstance(team_snapshot, dict):
        team_snapshot = {}
    profiles = _team_profiles_from_state_snapshot(state_snapshot)
    tasks = [
        item for item in board_snapshot.get("tasks", [])
        if isinstance(item, dict)
    ]
    status_counts = {
        "pending": 0,
        "blocked": 0,
        "in_progress": 0,
        "completed": 0,
        "failed": 0,
        "other": 0,
    }
    for task in tasks:
        status = str(task.get("status", "other") or "other")
        if status not in status_counts:
            status_counts["other"] += 1
        else:
            status_counts[status] += 1

    team_names = [str(profile.get("name", "") or "") for profile in profiles]
    rollups = _load_team_progress_event_rollups(
        logger_path=(logger.path if logger is not None else None),
        team_names=[name for name in team_names if name],
    )

    agent_rows: List[Dict[str, Any]] = []
    for profile in profiles:
        name = str(profile.get("name", "") or "")
        if not name:
            continue
        active_task_ids: List[str] = []
        completed_task_ids: List[str] = []
        failed_task_ids: List[str] = []
        available_task_ids: List[str] = []
        blocked_task_ids: List[str] = []
        for task in tasks:
            task_id = str(task.get("task_id", "") or "")
            status = str(task.get("status", "") or "")
            owner = str(task.get("owner", "") or "")
            if owner == name:
                if status == "in_progress":
                    active_task_ids.append(task_id)
                elif status == "completed":
                    completed_task_ids.append(task_id)
                elif status == "failed":
                    failed_task_ids.append(task_id)
            if status == "pending" and _task_matches_profile(task, profile):
                available_task_ids.append(task_id)
            elif status == "blocked" and _task_matches_profile(task, profile):
                blocked_task_ids.append(task_id)

        rollup = rollups.get(name, _empty_team_progress_rollup())
        closed_count = int(rollup.get("tasks_completed", 0)) + int(rollup.get("tasks_failed", 0))
        success_rate = round(
            float(rollup.get("tasks_completed", 0)) / max(1, closed_count),
            3,
        )
        if active_task_ids:
            activity_status = "active"
        elif available_task_ids:
            activity_status = "ready"
        elif blocked_task_ids:
            activity_status = "blocked"
        else:
            activity_status = "idle"
        agent_rows.append(
            {
                "name": name,
                "agent_type": str(profile.get("agent_type", "general") or "general"),
                "skills": sorted({str(skill) for skill in profile.get("skills", [])}),
                "activity_status": activity_status,
                "tasks_claimed": int(rollup.get("tasks_claimed", 0)),
                "tasks_completed": int(rollup.get("tasks_completed", 0)),
                "tasks_failed": int(rollup.get("tasks_failed", 0)),
                "tasks_deferred": int(rollup.get("tasks_deferred", 0)),
                "active_tasks": len(active_task_ids),
                "available_tasks": len(available_task_ids),
                "blocked_tasks": len(blocked_task_ids),
                "messages_sent": int(rollup.get("messages_sent", 0)),
                "messages_received": int(rollup.get("messages_received", 0)),
                "idle_ticks": int(rollup.get("idle_ticks", 0)),
                "last_event_at": str(rollup.get("last_event_at", "") or ""),
                "success_rate": success_rate,
                "task_ids": {
                    "active": active_task_ids,
                    "completed_owned": completed_task_ids,
                    "failed_owned": failed_task_ids,
                    "available": available_task_ids,
                    "blocked": blocked_task_ids,
                },
            }
        )

    total_tasks = len(tasks)
    completed_ratio = round(status_counts["completed"] / max(1, total_tasks), 3)
    return {
        "generated_at": utc_now(),
        "lead_name": str(state_snapshot.get("lead_name", "lead") or "lead"),
        "mailbox_model": team_snapshot.get(
            "mailbox_model",
            "asynchronous pull-based inbox",
        ),
        "task_status_counts": status_counts,
        "task_count": total_tasks,
        "completed_ratio": completed_ratio,
        "agents": agent_rows,
    }


def write_team_progress_report(report_path: pathlib.Path, snapshot: Dict[str, Any]) -> Dict[str, Any]:
    lines: List[str] = []
    lines.append("# Team Progress")
    lines.append("")
    lines.append(f"- Generated at: {snapshot.get('generated_at', utc_now())}")
    lines.append(f"- Lead: {snapshot.get('lead_name', 'lead')}")
    lines.append(f"- Task count: {snapshot.get('task_count', 0)}")
    status_counts = snapshot.get("task_status_counts", {})
    lines.append(
        "- Task status counts: "
        f"completed={status_counts.get('completed', 0)} "
        f"failed={status_counts.get('failed', 0)} "
        f"in_progress={status_counts.get('in_progress', 0)} "
        f"pending={status_counts.get('pending', 0)} "
        f"blocked={status_counts.get('blocked', 0)}"
    )
    lines.append(f"- Completed ratio: {snapshot.get('completed_ratio', 0)}")
    lines.append("")
    lines.append("## Agent Summary")
    lines.append("")
    lines.append("| Agent | Type | Claimed | Completed | Failed | Active | Ready | Blocked | Sent | Received | Status |")
    lines.append("|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---|")
    for agent in snapshot.get("agents", []):
        if not isinstance(agent, dict):
            continue
        lines.append(
            f"| {agent.get('name', '')} | {agent.get('agent_type', '')} | "
            f"{agent.get('tasks_claimed', 0)} | {agent.get('tasks_completed', 0)} | "
            f"{agent.get('tasks_failed', 0)} | {agent.get('active_tasks', 0)} | "
            f"{agent.get('available_tasks', 0)} | {agent.get('blocked_tasks', 0)} | "
            f"{agent.get('messages_sent', 0)} | {agent.get('messages_received', 0)} | "
            f"{agent.get('activity_status', 'idle')} |"
        )
    lines.append("")
    lines.append("## Current Work")
    lines.append("")
    for agent in snapshot.get("agents", []):
        if not isinstance(agent, dict):
            continue
        lines.append(
            f"- {agent.get('name', '')}: active={', '.join(agent.get('task_ids', {}).get('active', [])) or 'none'}; "
            f"ready={', '.join(agent.get('task_ids', {}).get('available', [])) or 'none'}; "
            f"blocked={', '.join(agent.get('task_ids', {}).get('blocked', [])) or 'none'}; "
            f"last_event_at={agent.get('last_event_at', '') or 'n/a'}"
        )
    lines.append("")
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text("\n".join(lines), encoding="utf-8")
    return {
        "report_path": str(report_path),
        "agent_count": len(snapshot.get("agents", [])),
    }


def append_team_progress_to_final_report(report_path: pathlib.Path, snapshot: Dict[str, Any]) -> bool:
    if not report_path.exists():
        return False
    existing = report_path.read_text(encoding="utf-8")
    if "## Team Progress" in existing:
        return False
    lines: List[str] = []
    lines.append("")
    lines.append("## Team Progress")
    lines.append("")
    status_counts = snapshot.get("task_status_counts", {})
    lines.append(
        "- Team task states: "
        f"completed={status_counts.get('completed', 0)} "
        f"failed={status_counts.get('failed', 0)} "
        f"in_progress={status_counts.get('in_progress', 0)} "
        f"pending={status_counts.get('pending', 0)} "
        f"blocked={status_counts.get('blocked', 0)}"
    )
    for agent in snapshot.get("agents", []):
        if not isinstance(agent, dict):
            continue
        lines.append(
            f"- {agent.get('name', '')} ({agent.get('agent_type', '')}): "
            f"completed={agent.get('tasks_completed', 0)} "
            f"failed={agent.get('tasks_failed', 0)} "
            f"active={agent.get('active_tasks', 0)} "
            f"ready={agent.get('available_tasks', 0)} "
            f"blocked={agent.get('blocked_tasks', 0)} "
            f"messages={agent.get('messages_sent', 0)}/{agent.get('messages_received', 0)} "
            f"status={agent.get('activity_status', 'idle')}"
        )
    report_path.write_text(existing.rstrip() + "\n" + "\n".join(lines) + "\n", encoding="utf-8")
    return True


def build_lead_interaction_snapshot(
    shared_state: SharedState,
    logger: EventLogger,
    recent_message_limit: int = 24,
) -> Dict[str, Any]:
    state_snapshot = shared_state.snapshot()
    lead_name = str(state_snapshot.get("lead_name", "lead") or "lead")
    raw_interaction = state_snapshot.get(LEAD_INTERACTION_STATE_KEY, {})
    if not isinstance(raw_interaction, dict):
        raw_interaction = {}
    raw_requests = raw_interaction.get("plan_approval_requests", {})
    if not isinstance(raw_requests, dict):
        raw_requests = {}
    requests = [
        dict(item)
        for item in raw_requests.values()
        if isinstance(item, dict)
    ]
    requests.sort(key=lambda item: (str(item.get("requested_at", "") or ""), str(item.get("task_id", "") or "")))
    pending_requests = [
        item for item in requests if str(item.get("status", "") or "") == PLAN_APPROVAL_STATUS_PENDING
    ]

    recent_team_messages: List[Dict[str, Any]] = []
    if logger.path.exists():
        with logger.path.open("r", encoding="utf-8") as fh:
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
                if str(payload.get("event", "") or "") != "mail_sent":
                    continue
                sender = str(payload.get("sender", "") or "")
                recipient = str(payload.get("recipient", "") or "")
                if lead_name not in {sender, recipient}:
                    continue
                recent_team_messages.append(
                    {
                        "ts": str(payload.get("ts", "") or ""),
                        "event_index": int(payload.get("event_index", 0) or 0),
                        "sender": sender,
                        "recipient": recipient,
                        "subject": str(payload.get("subject", "") or ""),
                        "task_id": str(payload.get("task_id", "") or ""),
                        "body_preview": _lead_message_body_preview(
                            subject=str(payload.get("subject", "") or ""),
                            body=payload.get("body", ""),
                        ),
                    }
                )
    if recent_message_limit > 0:
        recent_team_messages = recent_team_messages[-recent_message_limit:]

    controls = state_snapshot.get("plan_approval_controls", {})
    if not isinstance(controls, dict):
        controls = {}
    teammate_session_snapshot = _build_lead_teammate_sessions_snapshot(shared_state=shared_state)
    return {
        "generated_at": utc_now(),
        "lead_name": lead_name,
        "command_path": str(raw_interaction.get("command_path", "") or ""),
        "command_cursor": int(raw_interaction.get("command_cursor", 0) or 0),
        "last_command_at": str(raw_interaction.get("last_command_at", "") or ""),
        "recent_commands": [
            dict(item)
            for item in raw_interaction.get("recent_commands", [])
            if isinstance(item, dict)
        ],
        "plan_approval_request_count": len(requests),
        "pending_plan_approval_count": len(pending_requests),
        "plan_approval_requests": requests,
        "pending_plan_approval_task_ids": [
            str(item.get("task_id", "") or "")
            for item in pending_requests
            if str(item.get("task_id", "") or "")
        ],
        "recent_team_messages": recent_team_messages,
        "recent_team_message_count": len(recent_team_messages),
        "controls": controls,
        **teammate_session_snapshot,
    }


def write_lead_interaction_report(report_path: pathlib.Path, snapshot: Dict[str, Any]) -> Dict[str, Any]:
    def _task_preview_text(item: Mapping[str, Any]) -> str:
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

    def _dependency_preview_text(item: Mapping[str, Any]) -> str:
        return (
            f"{str(item.get('task_id', '') or '')}+={str(item.get('dependency_id', '') or '')}"
        )

    lines: List[str] = []
    lines.append("# Lead Interaction")
    lines.append("")
    lines.append(f"- Generated at: {snapshot.get('generated_at', utc_now())}")
    lines.append(f"- Lead: {snapshot.get('lead_name', 'lead')}")
    lines.append(f"- Plan approval requests: {snapshot.get('plan_approval_request_count', 0)}")
    lines.append(f"- Pending approvals: {snapshot.get('pending_plan_approval_count', 0)}")
    controls = snapshot.get("controls", {})
    if isinstance(controls, dict):
        lines.append(
            "- Controls: "
            f"approve_all_pending={controls.get('approve_all_pending', False)} "
            f"approve_task_ids={','.join(controls.get('approve_task_ids', [])) or 'none'} "
            f"reject_task_ids={','.join(controls.get('reject_task_ids', [])) or 'none'} "
            f"lead_command_wait_seconds={controls.get('lead_command_wait_seconds', 0)} "
            f"lead_interactive={controls.get('lead_interactive', False)}"
        )
    lines.append(
        f"- Command channel: path={snapshot.get('command_path', '') or 'n/a'} "
        f"cursor={snapshot.get('command_cursor', 0)} "
        f"last_command_at={snapshot.get('last_command_at', '') or 'n/a'}"
    )
    lines.append(
        f"- Teammate sessions: {snapshot.get('teammate_session_count', 0)} "
        f"active={snapshot.get('active_teammate_session_count', 0)}"
    )
    status_counts = snapshot.get("teammate_session_status_counts", {})
    if isinstance(status_counts, dict) and status_counts:
        lines.append(
            "- Teammate states: "
            + " ".join(f"{status}={count}" for status, count in sorted(status_counts.items()))
        )
    transport_counts = snapshot.get("teammate_session_transport_counts", {})
    if isinstance(transport_counts, dict) and transport_counts:
        lines.append(
            "- Teammate transports: "
            + " ".join(f"{transport}={count}" for transport, count in sorted(transport_counts.items()))
        )
    lines.append("")
    lines.append("## Teammate Sessions")
    lines.append("")
    teammate_sessions = snapshot.get("teammate_sessions", [])
    if not isinstance(teammate_sessions, list) or not teammate_sessions:
        lines.append("- none")
    else:
        for item in teammate_sessions:
            if not isinstance(item, Mapping):
                continue
            line = (
                f"- {item.get('summary', '')} "
                f"tasks={item.get('tasks_started', 0)}/{item.get('tasks_completed', 0)}/{item.get('tasks_failed', 0)} "
                f"messages_seen={item.get('messages_seen', 0)} "
                f"provider_replies={item.get('provider_replies', 0)} "
                f"last_active_at={item.get('last_active_at', '') or 'n/a'}"
            )
            if str(item.get("last_provider_topic", "") or ""):
                line += f" last_provider_topic={item.get('last_provider_topic', '')}"
            lines.append(line)
            recent_messages = item.get("recent_messages", [])
            if isinstance(recent_messages, list) and recent_messages:
                lines.append(
                    "  recent_messages: "
                    + "; ".join(
                        (
                            f"{str(message.get('from_agent', '') or '')}:"
                            f"{str(message.get('subject', '') or '')}"
                            f" task_id={str(message.get('task_id', '') or 'n/a')}"
                        )
                        for message in recent_messages
                        if isinstance(message, Mapping)
                    )
                )
    lines.append("")
    lines.append("## Pending Approvals")
    lines.append("")
    pending = [
        item
        for item in snapshot.get("plan_approval_requests", [])
        if isinstance(item, dict) and str(item.get("status", "") or "") == PLAN_APPROVAL_STATUS_PENDING
    ]
    if not pending:
        lines.append("- none")
    for item in pending:
        lines.append(
            f"- {item.get('task_id', '')} ({item.get('task_type', '')}) "
            f"requested_by={item.get('requested_by', '')} "
            f"transport={item.get('transport', '')} "
            f"proposed_tasks={','.join(item.get('proposed_task_ids', [])) or 'none'} "
            f"proposed_dependencies={','.join(item.get('proposed_dependency_ids', [])) or 'none'}"
        )
        task_preview = [
            _task_preview_text(preview)
            for preview in item.get("proposed_tasks_preview", [])
            if isinstance(preview, Mapping)
        ]
        dependency_preview = [
            _dependency_preview_text(preview)
            for preview in item.get("proposed_dependencies_preview", [])
            if isinstance(preview, Mapping)
        ]
        if task_preview:
            lines.append(f"  task_preview: {'; '.join(task_preview)}")
        if dependency_preview:
            lines.append(f"  dependency_preview: {'; '.join(dependency_preview)}")
    lines.append("")
    lines.append("## Recent Team Messages")
    lines.append("")
    recent_messages = snapshot.get("recent_team_messages", [])
    if not isinstance(recent_messages, list) or not recent_messages:
        lines.append("- none")
    else:
        for item in recent_messages:
            if not isinstance(item, dict):
                continue
            lines.append(
                f"- [{item.get('event_index', '')}] {item.get('sender', '')} -> {item.get('recipient', '')}: "
                f"{item.get('subject', '')} task_id={item.get('task_id', '') or 'n/a'} at {item.get('ts', '')}"
            )
            if str(item.get("body_preview", "") or ""):
                lines.append(f"  body_preview: {item.get('body_preview', '')}")
    lines.append("")
    lines.append("## Recent Commands")
    lines.append("")
    recent_commands = snapshot.get("recent_commands", [])
    if not isinstance(recent_commands, list) or not recent_commands:
        lines.append("- none")
    else:
        for item in recent_commands:
            if not isinstance(item, dict):
                continue
            lines.append(
                f"- [{item.get('line_index', '')}] source={item.get('source', 'unknown')} "
                f"command={item.get('command', '') or 'invalid'} "
                f"agent={item.get('agent', '') or 'n/a'} "
                f"task_ids={','.join(item.get('task_ids', [])) or 'none'} "
                f"valid={item.get('valid', False)} "
                f"received_at={item.get('received_at', '') or 'n/a'}"
            )
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text("\n".join(lines), encoding="utf-8")
    return {
        "report_path": str(report_path),
        "pending_plan_approval_count": int(snapshot.get("pending_plan_approval_count", 0) or 0),
    }


def write_live_lead_interaction_artifacts(
    output_dir: pathlib.Path,
    shared_state: SharedState,
    logger: EventLogger,
) -> Dict[str, Any]:
    snapshot = build_lead_interaction_snapshot(
        shared_state=shared_state,
        logger=logger,
    )
    interaction_path = pathlib.Path(output_dir) / LEAD_INTERACTION_FILENAME
    interaction_path.parent.mkdir(parents=True, exist_ok=True)
    interaction_path.write_text(
        json.dumps(snapshot, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    interaction_report_path = pathlib.Path(output_dir) / LEAD_INTERACTION_REPORT_FILENAME
    report_summary = write_lead_interaction_report(
        report_path=interaction_report_path,
        snapshot=snapshot,
    )
    return {
        "snapshot": snapshot,
        "lead_interaction_path": str(interaction_path),
        "lead_interaction_report_path": str(interaction_report_path),
        **report_summary,
    }


def append_lead_interaction_to_final_report(report_path: pathlib.Path, snapshot: Dict[str, Any]) -> bool:
    if not report_path.exists():
        return False
    existing = report_path.read_text(encoding="utf-8")
    if "## Lead Interaction" in existing:
        return False
    lines: List[str] = []
    lines.append("")
    lines.append("## Lead Interaction")
    lines.append("")
    lines.append(
        f"- Pending plan approvals: {snapshot.get('pending_plan_approval_count', 0)} "
        f"of {snapshot.get('plan_approval_request_count', 0)} requests"
    )
    pending_task_ids = snapshot.get("pending_plan_approval_task_ids", [])
    if isinstance(pending_task_ids, list) and pending_task_ids:
        lines.append("- Pending approval task ids: " + ", ".join(str(item) for item in pending_task_ids))
    recent_messages = snapshot.get("recent_team_messages", [])
    if isinstance(recent_messages, list) and recent_messages:
        latest = recent_messages[-1]
        if isinstance(latest, dict):
            lines.append(
                f"- Latest lead-visible message: {latest.get('sender', '')} -> {latest.get('recipient', '')} "
                f"{latest.get('subject', '')} task_id={latest.get('task_id', '') or 'n/a'}"
            )
    if snapshot.get("command_path", ""):
        lines.append(
            f"- Lead command channel: {snapshot.get('command_path', '')} "
            f"(cursor={snapshot.get('command_cursor', 0)})"
        )
    report_path.write_text(existing.rstrip() + "\n" + "\n".join(lines) + "\n", encoding="utf-8")
    return True


def append_teammate_sessions_to_final_report(report_path: pathlib.Path, snapshot: Dict[str, Any]) -> bool:
    if not report_path.exists():
        return False
    existing = report_path.read_text(encoding="utf-8")
    if "## Teammate Sessions" in existing:
        return False
    lines: List[str] = []
    lines.append("")
    lines.append("## Teammate Sessions")
    lines.append("")
    lines.append(f"- Session count: {snapshot.get('session_count', 0)}")
    transport_counts = snapshot.get("transport_counts", {})
    if isinstance(transport_counts, dict) and transport_counts:
        lines.append(
            "- Session transports: "
            + " ".join(
                f"{transport}={count}" for transport, count in sorted(transport_counts.items())
            )
        )
    lifecycle_counts = snapshot.get("lifecycle_counts", {})
    if isinstance(lifecycle_counts, dict) and lifecycle_counts:
        lines.append(
            "- Session lifecycle: "
            f"activations={lifecycle_counts.get('run_activations', 0)} "
            f"initializations={lifecycle_counts.get('initializations', 0)} "
            f"resumes={lifecycle_counts.get('resumes', 0)}"
        )
    status_counts = snapshot.get("status_counts", {})
    if isinstance(status_counts, dict) and status_counts:
        lines.append(
            "- Session states: "
            + " ".join(
                f"{status}={count}" for status, count in sorted(status_counts.items())
            )
        )
    for session in snapshot.get("sessions", []):
        if not isinstance(session, dict):
            continue
        lines.append(
            f"- {session.get('agent', '')}: "
            f"session_id={session.get('session_id', '')} "
            f"transport={session.get('transport', '')} "
            f"transport_session={session.get('transport_session_name', '')} "
            f"status={session.get('status', '')} "
            f"started={session.get('tasks_started', 0)} "
            f"completed={session.get('tasks_completed', 0)} "
            f"failed={session.get('tasks_failed', 0)} "
            f"messages_seen={session.get('messages_seen', 0)} "
            f"provider_memory={len(session.get('provider_memory', []))} "
            f"initializations={session.get('initialization_count', 0)} "
            f"resumes={session.get('resume_count', 0)}"
        )
        if session.get("workspace_scope", ""):
            lines[-1] += (
                f" workspace_scope={session.get('workspace_scope', '')} "
                f"workspace_isolated={session.get('workspace_isolation_active', False)}"
            )
        workspace_workdir = str(session.get("workspace_workdir", "") or "")
        if workspace_workdir:
            lines[-1] += f" workspace_workdir={workspace_workdir}"
        workspace_home_dir = str(session.get("workspace_home_dir", "") or "")
        if workspace_home_dir:
            lines[-1] += f" workspace_home_dir={workspace_home_dir}"
        workspace_target_dir = str(session.get("workspace_target_dir", "") or "")
        if workspace_target_dir:
            lines[-1] += f" workspace_target_dir={workspace_target_dir}"
        last_resume_from = str(session.get("last_resume_from", "") or "")
        if last_resume_from:
            lines[-1] += f" last_resume_from={last_resume_from}"
    report_path.write_text(existing.rstrip() + "\n" + "\n".join(lines) + "\n", encoding="utf-8")
    return True


def append_host_enforcement_to_final_report(report_path: pathlib.Path, snapshot: Dict[str, Any]) -> bool:
    if not report_path.exists():
        return False
    existing = report_path.read_text(encoding="utf-8")
    if "## Host Enforcement" in existing:
        return False
    lines: List[str] = []
    lines.append("")
    lines.append("## Host Enforcement")
    lines.append("")
    host = snapshot.get("host", {})
    if isinstance(host, dict):
        lines.append(
            f"- Host: {host.get('kind', '')} configured_transport={host.get('session_transport', '')} "
            f"independent_sessions={host.get('capabilities', {}).get('independent_sessions', False)} "
            f"workspace_isolation={host.get('capabilities', {}).get('workspace_isolation', False)}"
        )
        environment = host.get("environment", {})
        if isinstance(environment, dict) and environment:
            lines.append(
                f"- Host environment: cli_installed={environment.get('cli_installed', False)} "
                f"relay={environment.get('relay_host', '')} "
                f"official_relay_active={environment.get('official_relay_active', False)} "
                f"relay_source={environment.get('relay_source', '')} "
                f"subscription_available={environment.get('subscription_available', '')} "
                f"native_prerequisites_ready={environment.get('native_session_prerequisites_ready', False)} "
                f"prerequisite_reason={environment.get('native_session_prerequisite_reason', '')}"
            )
    lines.append(
        f"- Enforcement: requested_teammate_mode={snapshot.get('requested_teammate_mode', '')} "
        f"session={snapshot.get('session_enforcement', '')} "
        f"workspace={snapshot.get('workspace_enforcement', '')} "
        f"boundary_source={snapshot.get('effective_boundary_source', '')} "
        f"boundary_strength={snapshot.get('effective_boundary_strength', '')}"
    )
    lines.append(
        f"- Host-native: session_active={snapshot.get('host_native_session_active', False)} "
        f"workspace_active={snapshot.get('host_native_workspace_active', False)} "
        f"managed_context_requested={snapshot.get('host_managed_context_requested', False)} "
        f"managed_context_active={snapshot.get('host_managed_context_active', False)}"
    )
    backend = str(snapshot.get("host_session_backend", "") or "")
    if backend:
        lines.append(
            f"- Host session backend: backend={backend} "
            f"source={snapshot.get('host_session_backend_source', '')} "
            f"session_isolated={snapshot.get('host_session_backend_session_isolation_active', False)} "
            f"workspace_isolated={snapshot.get('host_session_backend_workspace_isolation_active', False)}"
        )
    limits = snapshot.get("limits", [])
    if isinstance(limits, list) and limits:
        lines.append("- Limits: " + ", ".join(str(item) for item in limits))
    notes = snapshot.get("notes", [])
    if isinstance(notes, list) and notes:
        lines.append("- Notes: " + ", ".join(str(item) for item in notes))
    report_path.write_text(existing.rstrip() + "\n" + "\n".join(lines) + "\n", encoding="utf-8")
    return True


def append_session_boundaries_to_final_report(report_path: pathlib.Path, snapshot: Dict[str, Any]) -> bool:
    if not report_path.exists():
        return False
    existing = report_path.read_text(encoding="utf-8")
    if "## Session Boundaries" in existing:
        return False
    lines: List[str] = []
    lines.append("")
    lines.append("## Session Boundaries")
    lines.append("")
    host = snapshot.get("host", {})
    if isinstance(host, dict):
        lines.append(
            f"- Host: {host.get('kind', '')} transport={host.get('session_transport', '')} "
            f"independent_sessions={host.get('capabilities', {}).get('independent_sessions', False)} "
            f"workspace_isolation={host.get('capabilities', {}).get('workspace_isolation', False)}"
        )
    mode_counts = snapshot.get("boundary_mode_counts", {})
    if isinstance(mode_counts, dict) and mode_counts:
        lines.append(
            "- Boundary modes: "
            + " ".join(f"{mode}={count}" for mode, count in sorted(mode_counts.items()))
        )
    strength_counts = snapshot.get("boundary_strength_counts", {})
    if isinstance(strength_counts, dict) and strength_counts:
        lines.append(
            "- Boundary strength: "
            + " ".join(f"{strength}={count}" for strength, count in sorted(strength_counts.items()))
        )
    for session in snapshot.get("sessions", []):
        if not isinstance(session, dict):
            continue
        notes = session.get("notes", [])
        if not isinstance(notes, list):
            notes = []
        lines.append(
            f"- {session.get('agent', '')}: "
            f"mode={session.get('boundary_mode', '')} "
            f"strength={session.get('boundary_strength', '')} "
            f"transport={session.get('transport', '')} "
            f"transport_backend={session.get('transport_backend', '') or 'n/a'} "
            f"transport_session={session.get('transport_session_name', '')} "
            f"status={session.get('status', '')} "
            f"workspace_scope={session.get('workspace_scope', '')} "
            f"workspace_isolated={session.get('workspace_isolation_active', False)} "
            f"workspace_workdir={session.get('workspace_workdir', '') or 'n/a'} "
            f"workspace_home_dir={session.get('workspace_home_dir', '') or 'n/a'} "
            f"workspace_target_dir={session.get('workspace_target_dir', '') or 'n/a'} "
            f"notes={', '.join(str(item) for item in notes) or 'none'}"
        )
    report_path.write_text(existing.rstrip() + "\n" + "\n".join(lines) + "\n", encoding="utf-8")
    return True


def build_teammate_transport_summary(
    output_dir: pathlib.Path,
    runtime_config: RuntimeConfig,
) -> Dict[str, Any]:
    diagnostics_path = output_dir / "tmux_worker_diagnostics.jsonl"
    requested_mode = str(runtime_config.teammate_mode or "in-process")
    payloads: List[Dict[str, Any]] = []
    if diagnostics_path.exists():
        with diagnostics_path.open("r", encoding="utf-8") as fh:
            for raw_line in fh:
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(payload, dict):
                    payloads.append(payload)
    workers = sorted(
        {
            str(payload.get("worker", "") or "")
            for payload in payloads
            if str(payload.get("worker", "") or "")
        }
    )
    task_types = sorted(
        {
            str(payload.get("task_type", "") or "")
            for payload in payloads
            if str(payload.get("task_type", "") or "")
        }
    )
    transports_seen = sorted(
        {
            str(payload.get("transport_used", payload.get("transport_requested", "")) or "")
            for payload in payloads
            if str(payload.get("transport_used", payload.get("transport_requested", "")) or "")
        }
    )
    fallback_reasons = sorted(
        {
            str(payload.get("fallback_reason", "") or "")
            for payload in payloads
            if payload.get("fallback_used") and str(payload.get("fallback_reason", "") or "")
        }
    )
    fallback_used = any(bool(payload.get("fallback_used", False)) for payload in payloads)
    degraded = requested_mode == "tmux" and fallback_used and any(
        "subprocess" in transport for transport in transports_seen
    )
    effective_mode = "tmux_degraded_subprocess" if degraded else requested_mode
    return {
        "requested_mode": requested_mode,
        "effective_mode": effective_mode,
        "degraded": degraded,
        "worker_task_count": len(payloads),
        "workers": workers,
        "task_types": task_types,
        "transports_seen": transports_seen,
        "fallback_used": fallback_used,
        "fallback_reasons": fallback_reasons,
        "diagnostics_path": str(diagnostics_path) if diagnostics_path.exists() else "",
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
    team_snapshot = state_snapshot.get("team", {})
    if not isinstance(team_snapshot, dict):
        team_snapshot = {}

    def append_history_entry(path: pathlib.Path, summary: Dict[str, Any], kind: str) -> str:
        entry = {
            "generated_at": utc_now(),
            "kind": kind,
            "resume_from": str(resume_from) if resume_from else "",
            "interrupted_reason": interrupted_reason,
            "summary": summary,
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        return str(path)

    tmux_cleanup_summary_path = output_dir / "tmux_session_cleanup_summary.json"
    tmux_cleanup_history_path = output_dir / "tmux_session_cleanup_history.jsonl"
    tmux_cleanup_summary = state_snapshot.get("tmux_session_cleanup_summary", {})
    tmux_cleanup_summary_path_str = ""
    tmux_cleanup_history_path_str = ""
    if isinstance(tmux_cleanup_summary, dict) and tmux_cleanup_summary:
        tmux_cleanup_summary_path.write_text(
            json.dumps(tmux_cleanup_summary, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        tmux_cleanup_summary_path_str = str(tmux_cleanup_summary_path)
        tmux_cleanup_history_path_str = append_history_entry(
            path=tmux_cleanup_history_path,
            summary=tmux_cleanup_summary,
            kind="cleanup",
        )

    tmux_recovery_summary_path = output_dir / "tmux_session_recovery_summary.json"
    tmux_recovery_history_path = output_dir / "tmux_session_recovery_history.jsonl"
    tmux_recovery_summary = state_snapshot.get("tmux_session_recovery_summary", {})
    tmux_recovery_summary_path_str = ""
    tmux_recovery_history_path_str = ""
    if isinstance(tmux_recovery_summary, dict) and tmux_recovery_summary:
        tmux_recovery_summary_path.write_text(
            json.dumps(tmux_recovery_summary, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        tmux_recovery_summary_path_str = str(tmux_recovery_summary_path)
        tmux_recovery_history_path_str = append_history_entry(
            path=tmux_recovery_history_path,
            summary=tmux_recovery_summary,
            kind="recovery",
        )

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

    team_progress_snapshot = build_team_progress_snapshot(
        board=board,
        shared_state=shared_state,
        logger=logger,
    )
    team_progress_path = output_dir / TEAM_PROGRESS_FILENAME
    team_progress_path.write_text(
        json.dumps(team_progress_snapshot, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    team_progress_report_path = output_dir / TEAM_PROGRESS_REPORT_FILENAME
    write_team_progress_report(
        report_path=team_progress_report_path,
        snapshot=team_progress_snapshot,
    )
    append_team_progress_to_final_report(
        report_path=output_dir / "final_report.md",
        snapshot=team_progress_snapshot,
    )
    lead_interaction_written = write_live_lead_interaction_artifacts(
        output_dir=output_dir,
        shared_state=shared_state,
        logger=logger,
    )
    lead_interaction_snapshot = dict(lead_interaction_written.get("snapshot", {}))
    lead_interaction_path = pathlib.Path(
        str(lead_interaction_written.get("lead_interaction_path", output_dir / LEAD_INTERACTION_FILENAME))
    )
    lead_interaction_report_path = pathlib.Path(
        str(
            lead_interaction_written.get(
                "lead_interaction_report_path",
                output_dir / LEAD_INTERACTION_REPORT_FILENAME,
            )
        )
    )
    append_lead_interaction_to_final_report(
        report_path=output_dir / "final_report.md",
        snapshot=lead_interaction_snapshot,
    )
    teammate_sessions_snapshot = build_teammate_sessions_snapshot(shared_state=shared_state)
    teammate_sessions_path = output_dir / TEAMMATE_SESSIONS_FILENAME
    teammate_sessions_path.write_text(
        json.dumps(teammate_sessions_snapshot, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    append_teammate_sessions_to_final_report(
        report_path=output_dir / "final_report.md",
        snapshot=teammate_sessions_snapshot,
    )
    host_enforcement_snapshot = build_host_enforcement_snapshot(shared_state=shared_state)
    host_enforcement_path = output_dir / HOST_ENFORCEMENT_FILENAME
    host_enforcement_path.write_text(
        json.dumps(host_enforcement_snapshot, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    append_host_enforcement_to_final_report(
        report_path=output_dir / "final_report.md",
        snapshot=host_enforcement_snapshot,
    )
    session_boundary_snapshot = build_session_boundary_snapshot(shared_state=shared_state)
    session_boundary_path = output_dir / SESSION_BOUNDARY_FILENAME
    session_boundary_path.write_text(
        json.dumps(session_boundary_snapshot, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    append_session_boundaries_to_final_report(
        report_path=output_dir / "final_report.md",
        snapshot=session_boundary_snapshot,
    )

    context_boundary_summary = build_context_boundary_summary(logger=logger)
    context_boundary_path = output_dir / CONTEXT_BOUNDARY_FILENAME
    context_boundary_path.write_text(
        json.dumps(context_boundary_summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    teammate_transport_summary = build_teammate_transport_summary(
        output_dir=output_dir,
        runtime_config=runtime_config,
    )

    summary_path = output_dir / "run_summary.json"
    summary = {
        "generated_at": utc_now(),
        "events_path": str(logger.path),
        "task_board_path": str(board_path),
        "shared_state_path": str(state_path),
        "lock_state_path": str(lock_path),
        "final_report_path": str(output_dir / "final_report.md"),
        "context_boundary_path": str(context_boundary_path),
        "host_enforcement_path": str(host_enforcement_path),
        "lead_interaction_path": str(lead_interaction_path),
        "lead_interaction_report_path": str(lead_interaction_report_path),
        "lead_command_path": str(lead_interaction_snapshot.get("command_path", "") or ""),
        "session_boundary_path": str(session_boundary_path),
        "teammate_sessions_path": str(teammate_sessions_path),
        "team_progress_path": str(team_progress_path),
        "team_progress_report_path": str(team_progress_report_path),
        "tmux_session_cleanup_summary_path": tmux_cleanup_summary_path_str,
        "tmux_session_cleanup_history_path": tmux_cleanup_history_path_str,
        "tmux_session_recovery_summary_path": tmux_recovery_summary_path_str,
        "tmux_session_recovery_history_path": tmux_recovery_history_path_str,
        "tmux_session_leases_path": tmux_session_leases_path_str,
        "mailbox_model": mailbox.model_name(),
        "mailbox_storage_dir": str(mailbox.storage_dir) if mailbox.storage_dir else "",
        "provider": provider_meta.to_dict(),
        "runtime_config": runtime_config.to_dict(),
        "teammate_mode_requested": str(runtime_config.teammate_mode or ""),
        "teammate_mode_effective": teammate_transport_summary.get("effective_mode", ""),
        "teammate_transport_degraded": bool(teammate_transport_summary.get("degraded", False)),
        "teammate_transport_summary": teammate_transport_summary,
        "pending_plan_approval_count": int(lead_interaction_snapshot.get("pending_plan_approval_count", 0) or 0),
        "pending_plan_approval_task_ids": list(lead_interaction_snapshot.get("pending_plan_approval_task_ids", [])),
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
