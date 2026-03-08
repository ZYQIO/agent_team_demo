from __future__ import annotations

import json
import pathlib
import shlex
import shutil
import subprocess
import sys
import time
import uuid
from typing import Any, Callable, Collection, Dict, List, Sequence

from ..config import RuntimeConfig
from ..core import AgentProfile, EventLogger


TMUX_ANALYST_TASK_TYPES = {
    "discover_markdown",
    "heading_audit",
    "length_audit",
    "heading_structure_followup",
    "length_risk_followup",
}


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


def run_tmux_worker_entrypoint(
    task_file: pathlib.Path,
    run_payload_fn: Callable[[Dict[str, Any]], Dict[str, Any]] = run_tmux_worker_payload,
) -> int:
    try:
        payload = json.loads(task_file.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("worker payload must be a JSON object")
        result = run_payload_fn(payload)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except Exception as exc:
        print(f"worker_error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


def execute_worker_subprocess(command: List[str], timeout_sec: int) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
        timeout=timeout_sec,
    )


def execute_worker_tmux(
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


def run_tmux_worker_task(
    runtime_script: pathlib.Path,
    output_dir: pathlib.Path,
    runtime_config: RuntimeConfig,
    payload: Dict[str, Any],
    worker_name: str,
    logger: EventLogger,
    timeout_sec: int = 120,
    execute_worker_tmux_fn: Callable[
        [List[str], pathlib.Path, str, int], subprocess.CompletedProcess[str]
    ] = execute_worker_tmux,
    execute_worker_subprocess_fn: Callable[
        [List[str], int], subprocess.CompletedProcess[str]
    ] = execute_worker_subprocess,
    which_fn: Callable[[str], str | None] = shutil.which,
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
        fallback_completed = execute_worker_subprocess_fn(command=command, timeout_sec=timeout_sec)
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
            if which_fn("tmux"):
                transport = "tmux"
                completed = execute_worker_tmux_fn(
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
                completed = execute_worker_subprocess_fn(command=command, timeout_sec=timeout_sec)
        else:
            completed = execute_worker_subprocess_fn(command=command, timeout_sec=timeout_sec)

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
    lead_context: Any,
    analyst_profiles: Sequence[AgentProfile],
    runtime_script: pathlib.Path,
    run_worker_task_fn: Callable[..., Dict[str, Any]],
    supported_task_types: Collection[str],
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
        if task.task_type not in supported_task_types:
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
        execution = run_worker_task_fn(
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
