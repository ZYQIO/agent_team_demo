from __future__ import annotations

import json
import os
import pathlib
import shlex
import shutil
import subprocess
import sys
import time
import uuid
from typing import Any, Callable, Collection, Dict, List, Optional, Sequence, Tuple

from ..config import RuntimeConfig
from ..core import AgentProfile, EventLogger, utc_now
from ..runtime.task_context import build_task_context_snapshot


TMUX_ANALYST_TASK_TYPES = {
    "discover_markdown",
    "discover_repository",
    "heading_audit",
    "length_audit",
    "extension_audit",
    "large_file_audit",
    "heading_structure_followup",
    "length_risk_followup",
    "extension_hotspot_followup",
    "directory_hotspot_followup",
}
TMUX_WORKER_DIAGNOSTICS_FILENAME = "tmux_worker_diagnostics.jsonl"
TMUX_WORKER_OUTPUT_PREVIEW_LIMIT = 240
TMUX_SESSION_POLL_INTERVAL_SEC = 0.1
TMUX_SESSION_SPAWN_MAX_ATTEMPTS = 3
TMUX_SESSION_LEASES_KEY = "tmux_session_leases"
TMUX_SESSION_WORKSPACE_DIRNAME = "_tmux_session_workspaces"
TMUX_SESSION_TARGET_SNAPSHOT_DIRNAME = "target_snapshot"
TMUX_SESSION_TARGET_METADATA_FILENAME = "target_snapshot.json"


def preferred_tmux_session_name(worker_name: str) -> str:
    return f"agent_{worker_name}"


def _load_tmux_session_leases(shared_state: Any) -> Dict[str, Dict[str, Any]]:
    raw = shared_state.get(TMUX_SESSION_LEASES_KEY, {})
    if not isinstance(raw, dict):
        return {}
    leases: Dict[str, Dict[str, Any]] = {}
    for worker_name, entry in raw.items():
        if isinstance(entry, dict):
            leases[str(worker_name)] = dict(entry)
    return leases


def _save_tmux_session_leases(shared_state: Any, leases: Dict[str, Dict[str, Any]]) -> None:
    shared_state.set(TMUX_SESSION_LEASES_KEY, dict(leases))


def _get_tmux_session_lease(shared_state: Any, worker_name: str) -> Dict[str, Any]:
    return dict(_load_tmux_session_leases(shared_state).get(str(worker_name), {}))


def _path_is_within(candidate: pathlib.Path, root: pathlib.Path) -> bool:
    try:
        candidate_value = str(candidate.resolve())
        root_value = str(root.resolve())
    except OSError:
        candidate_value = str(candidate)
        root_value = str(root)
    try:
        return os.path.commonpath([candidate_value, root_value]) == root_value
    except ValueError:
        return False


def _lease_allows_preferred_session_reuse(shared_state: Any, worker_name: str) -> bool:
    lease = _get_tmux_session_lease(shared_state=shared_state, worker_name=worker_name)
    return (
        str(lease.get("status", "")) in {"retained", "recovered_available"}
        and str(lease.get("session_name", "")) == preferred_tmux_session_name(worker_name)
    )


def _update_tmux_session_lease(
    lead_context: Any,
    worker_name: str,
    status: str,
    task_id: str = "",
    task_type: str = "",
    transport: str = "",
    cleanup_result: str = "",
    retained_for_reuse: bool = False,
    reused_existing: bool = False,
    reuse_authorized: bool = False,
    error: str = "",
    session_name: str = "",
    recovery_result: str = "",
    workspace_root: str = "",
    workspace_workdir: str = "",
    workspace_home_dir: str = "",
    workspace_target_dir: str = "",
    workspace_tmp_dir: str = "",
    workspace_scope: str = "",
    workspace_isolation_active: bool = False,
) -> Dict[str, Any]:
    leases = _load_tmux_session_leases(lead_context.shared_state)
    current = dict(leases.get(worker_name, {}))
    next_session_name = (
        str(session_name or "")
        or str(current.get("session_name", ""))
        or preferred_tmux_session_name(worker_name)
    )
    reuse_count = int(current.get("reuse_count", 0) or 0)
    if reused_existing:
        reuse_count += 1
    retained_at = str(current.get("retained_at", "") or "")
    if retained_for_reuse:
        retained_at = utc_now()
    entry = {
        "worker": worker_name,
        "session_name": next_session_name,
        "status": status,
        "last_task_id": str(task_id or current.get("last_task_id", "")),
        "last_task_type": str(task_type or current.get("last_task_type", "")),
        "last_transport": str(transport or current.get("last_transport", "")),
        "last_cleanup_result": str(cleanup_result or current.get("last_cleanup_result", "")),
        "retained_for_reuse": bool(retained_for_reuse),
        "reuse_authorized": bool(reuse_authorized),
        "reused_existing": bool(reused_existing),
        "reuse_count": reuse_count,
        "retained_at": retained_at,
        "recovery_result": str(recovery_result or current.get("recovery_result", "")),
        "recovered_at": utc_now() if recovery_result else str(current.get("recovered_at", "")),
        "workspace_root": str(workspace_root or current.get("workspace_root", "")),
        "workspace_workdir": str(workspace_workdir or current.get("workspace_workdir", "")),
        "workspace_home_dir": str(workspace_home_dir or current.get("workspace_home_dir", "")),
        "workspace_target_dir": str(workspace_target_dir or current.get("workspace_target_dir", "")),
        "workspace_tmp_dir": str(workspace_tmp_dir or current.get("workspace_tmp_dir", "")),
        "workspace_scope": str(workspace_scope or current.get("workspace_scope", "")),
        "workspace_isolation_active": bool(
            workspace_isolation_active or current.get("workspace_isolation_active", False)
        ),
        "updated_at": utc_now(),
        "error": str(error or "")[:400],
    }
    leases[worker_name] = entry
    _save_tmux_session_leases(shared_state=lead_context.shared_state, leases=leases)
    lead_context.logger.log(
        "tmux_worker_session_lease_updated",
        worker=worker_name,
        session_name=next_session_name,
        status=status,
        task_id=str(entry.get("last_task_id", "")),
        task_type=str(entry.get("last_task_type", "")),
        transport=str(entry.get("last_transport", "")),
        cleanup_result=str(entry.get("last_cleanup_result", "")),
        retained_for_reuse=bool(retained_for_reuse),
        reuse_authorized=bool(reuse_authorized),
        reused_existing=bool(reused_existing),
        reuse_count=reuse_count,
        retained_at=retained_at,
        recovery_result=str(entry.get("recovery_result", "")),
        error=str(entry.get("error", "")),
    )
    return entry


def _sync_session_boundary_from_lease(
    lead_context: Any,
    worker_name: str,
    lease_entry: Dict[str, Any],
    transport: str,
) -> None:
    if lead_context.session_registry is None:
        return
    if not isinstance(lease_entry, dict) or not lease_entry:
        return
    lead_context.session_registry.record_boundary(
        agent_name=worker_name,
        transport=transport,
        transport_session_name=str(lease_entry.get("session_name", "") or preferred_tmux_session_name(worker_name)),
        workspace_root=str(lease_entry.get("workspace_root", "") or ""),
        workspace_workdir=str(lease_entry.get("workspace_workdir", "") or ""),
        workspace_home_dir=str(lease_entry.get("workspace_home_dir", "") or ""),
        workspace_target_dir=str(lease_entry.get("workspace_target_dir", "") or ""),
        workspace_tmp_dir=str(lease_entry.get("workspace_tmp_dir", "") or ""),
        workspace_scope=str(lease_entry.get("workspace_scope", "") or ""),
        workspace_isolation_active=bool(lease_entry.get("workspace_isolation_active", False)),
        retained_for_reuse=bool(lease_entry.get("retained_for_reuse", False)),
        reuse_authorized=bool(lease_entry.get("reuse_authorized", False)),
        transport_reuse_count=int(lease_entry.get("reuse_count", 0) or 0),
    )


def _record_worker_boundary_from_diagnostics(
    lead_context: Any,
    worker_name: str,
    transport: str,
    execution_diagnostics: Dict[str, Any],
    retained_for_reuse: bool = False,
    reuse_authorized: bool = False,
    transport_reuse_count: int = 0,
) -> None:
    if lead_context.session_registry is None:
        return
    lead_context.session_registry.record_boundary(
        agent_name=worker_name,
        transport=transport,
        transport_session_name=str(
            execution_diagnostics.get("tmux_session_name", "")
            or execution_diagnostics.get("tmux_preferred_session_name", "")
            or preferred_tmux_session_name(worker_name)
        ),
        workspace_root=str(execution_diagnostics.get("tmux_session_workspace_root", "")),
        workspace_workdir=str(execution_diagnostics.get("tmux_session_workspace_workdir", "")),
        workspace_home_dir=str(execution_diagnostics.get("tmux_session_workspace_home_dir", "")),
        workspace_target_dir=str(execution_diagnostics.get("tmux_session_workspace_target_dir", "")),
        workspace_tmp_dir=str(execution_diagnostics.get("tmux_session_workspace_tmp_dir", "")),
        workspace_scope=str(execution_diagnostics.get("tmux_session_workspace_scope", "")),
        workspace_isolation_active=bool(execution_diagnostics.get("tmux_session_workspace_isolated", False)),
        retained_for_reuse=bool(retained_for_reuse),
        reuse_authorized=bool(reuse_authorized),
        transport_reuse_count=max(0, int(transport_reuse_count or 0)),
    )


def _tmux_session_exists(session_name: str) -> Dict[str, Any]:
    completed = subprocess.run(
        ["tmux", "has-session", "-t", session_name],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    stderr = str(completed.stderr or "").strip()
    if completed.returncode == 0:
        return {"exists": True, "error": ""}
    if _is_no_tmux_server_error(stderr) or _is_missing_tmux_session_error(stderr):
        return {"exists": False, "error": ""}
    return {"exists": False, "error": stderr[:400]}


def tmux_worker_diagnostics_file(output_dir: pathlib.Path) -> pathlib.Path:
    return output_dir / TMUX_WORKER_DIAGNOSTICS_FILENAME


def _output_preview(text: str, limit: int = TMUX_WORKER_OUTPUT_PREVIEW_LIMIT) -> str:
    compact = " ".join(str(text or "").split())
    return compact[:limit]


def _append_tmux_worker_diagnostics(output_dir: pathlib.Path, record: Dict[str, Any]) -> None:
    diagnostics_path = tmux_worker_diagnostics_file(output_dir)
    diagnostics_path.parent.mkdir(parents=True, exist_ok=True)
    with diagnostics_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")


def _attach_tmux_lifecycle(
    completed: subprocess.CompletedProcess[str],
    lifecycle: Dict[str, Any],
) -> subprocess.CompletedProcess[str]:
    completed.tmux_lifecycle = dict(lifecycle)
    return completed


def _extract_tmux_lifecycle(completed: subprocess.CompletedProcess[str]) -> Dict[str, Any]:
    lifecycle = getattr(completed, "tmux_lifecycle", {})
    if isinstance(lifecycle, dict):
        return dict(lifecycle)
    return {}


def _attach_transport_timeout(
    completed: subprocess.CompletedProcess[str],
    timeout_metadata: Dict[str, Any],
) -> subprocess.CompletedProcess[str]:
    completed.transport_timeout = dict(timeout_metadata)
    return completed


def _extract_transport_timeout(completed: subprocess.CompletedProcess[str]) -> Dict[str, Any]:
    timeout_metadata = getattr(completed, "transport_timeout", {})
    if isinstance(timeout_metadata, dict):
        return dict(timeout_metadata)
    return {}


def _normalize_timeout_output(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="ignore")
    return str(value)


def _completed_process_from_timeout(
    command: List[str],
    timeout_exc: subprocess.TimeoutExpired,
    transport: str,
    phase: str,
) -> subprocess.CompletedProcess[str]:
    stdout = _normalize_timeout_output(getattr(timeout_exc, "stdout", None))
    if not stdout:
        stdout = _normalize_timeout_output(getattr(timeout_exc, "output", None))
    stderr = _normalize_timeout_output(getattr(timeout_exc, "stderr", None))
    timeout_seconds = int(getattr(timeout_exc, "timeout", 0) or 0)
    if not stderr:
        stderr = f"{transport} worker timed out after {timeout_seconds}s"
    completed = subprocess.CompletedProcess(
        args=command,
        returncode=124,
        stdout=stdout,
        stderr=stderr,
    )
    return _attach_transport_timeout(
        completed,
        {
            "execution_timed_out": True,
            "timeout_transport": transport,
            "timeout_phase": phase,
            "timeout_message": stderr[:400],
        },
    )


def _is_duplicate_tmux_session_error(stderr: str) -> bool:
    message = str(stderr or "").casefold()
    return "duplicate session" in message or "session already exists" in message


def _is_no_tmux_server_error(message: str) -> bool:
    text = str(message or "").casefold()
    return "no server running" in text or "failed to connect to server" in text


def _is_missing_tmux_session_error(message: str) -> bool:
    text = str(message or "").casefold()
    return "can't find session" in text or "no such session" in text


def _build_tmux_ipc_paths(
    ipc_dir: pathlib.Path,
    session_name: str,
) -> Tuple[pathlib.Path, pathlib.Path, pathlib.Path]:
    return (
        ipc_dir / f"{session_name}.stdout.txt",
        ipc_dir / f"{session_name}.stderr.txt",
        ipc_dir / f"{session_name}.status.txt",
    )


def _build_tmux_session_name_candidates(
    session_prefix: str,
    max_attempts: int,
) -> List[str]:
    candidates = [session_prefix, session_prefix]
    while len(candidates) < max_attempts:
        candidates.append(f"{session_prefix}_{uuid.uuid4().hex[:8]}")
    return candidates[:max_attempts]


def _build_tmux_shell_command(
    command: List[str],
    stdout_file: pathlib.Path,
    stderr_file: pathlib.Path,
    status_file: pathlib.Path,
    worker_env: Optional[Dict[str, str]] = None,
) -> str:
    env_prefix = ""
    if isinstance(worker_env, dict) and worker_env:
        env_prefix = " ".join(
            f"{str(key)}={shlex.quote(str(value))}"
            for key, value in sorted(worker_env.items())
            if str(key)
        )
        if env_prefix:
            env_prefix += " "
    return (
        f"{env_prefix}{shlex.join(command)} > {shlex.quote(str(stdout_file))} "
        f"2> {shlex.quote(str(stderr_file))}; "
        f"echo $? > {shlex.quote(str(status_file))}"
    )


def _worker_subprocess_env(worker_env: Optional[Dict[str, str]]) -> Optional[Dict[str, str]]:
    if not isinstance(worker_env, dict) or not worker_env:
        return None
    env = dict(os.environ)
    env.update({str(key): str(value) for key, value in worker_env.items() if str(key)})
    return env


def _load_tmux_target_snapshot_metadata(metadata_path: pathlib.Path) -> Dict[str, Any]:
    if not metadata_path.exists():
        return {}
    try:
        payload = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(payload, dict):
        return {}
    return payload


def _tmux_snapshot_ignore_names(excluded_roots: Sequence[pathlib.Path]) -> Callable[[str, List[str]], List[str]]:
    resolved_roots = [path.resolve() for path in excluded_roots]
    ignored_names = {".git", ".codex_tmp", "__pycache__", ".pytest_cache"}

    def _ignore(dir_path: str, names: List[str]) -> List[str]:
        current = pathlib.Path(dir_path)
        ignored: List[str] = []
        for name in names:
            candidate = current / name
            if name in ignored_names:
                ignored.append(name)
                continue
            if any(_path_is_within(candidate, root) for root in resolved_roots):
                ignored.append(name)
        return ignored

    return _ignore


def _prepare_tmux_workspace_target_dir(
    output_dir: pathlib.Path,
    workspace_root: pathlib.Path,
    payload: Dict[str, Any],
) -> Dict[str, Any]:
    raw_target_dir = str(payload.get("target_dir", "") or "").strip()
    if not raw_target_dir:
        return {
            "workspace_target_dir": "",
            "workspace_target_source_dir": "",
            "workspace_target_status": "skipped_missing_target_dir",
            "workspace_target_reused": False,
        }

    source_target_dir = pathlib.Path(raw_target_dir).resolve()
    if not source_target_dir.exists() or not source_target_dir.is_dir():
        raise FileNotFoundError(f"tmux target_dir does not exist or is not a directory: {source_target_dir}")

    output_dir_resolved = output_dir.resolve()
    if source_target_dir == output_dir_resolved:
        return {
            "workspace_target_dir": "",
            "workspace_target_source_dir": str(source_target_dir),
            "workspace_target_status": "skipped_target_equals_output_dir",
            "workspace_target_reused": False,
        }
    if _path_is_within(source_target_dir, workspace_root):
        return {
            "workspace_target_dir": str(source_target_dir),
            "workspace_target_source_dir": str(source_target_dir),
            "workspace_target_status": "already_session_scoped",
            "workspace_target_reused": True,
        }

    snapshot_dir = workspace_root / TMUX_SESSION_TARGET_SNAPSHOT_DIRNAME
    build_dir = workspace_root / f"{TMUX_SESSION_TARGET_SNAPSHOT_DIRNAME}_build"
    metadata_path = workspace_root / TMUX_SESSION_TARGET_METADATA_FILENAME
    metadata = _load_tmux_target_snapshot_metadata(metadata_path)
    source_target_dir_str = str(source_target_dir)

    if (
        snapshot_dir.exists()
        and snapshot_dir.is_dir()
        and str(metadata.get("source_target_dir", "") or "") == source_target_dir_str
    ):
        return {
            "workspace_target_dir": str(snapshot_dir),
            "workspace_target_source_dir": source_target_dir_str,
            "workspace_target_status": "reused",
            "workspace_target_reused": True,
        }

    if build_dir.exists():
        shutil.rmtree(build_dir)
    if snapshot_dir.exists():
        shutil.rmtree(snapshot_dir)

    excluded_roots: List[pathlib.Path] = []
    if output_dir_resolved != source_target_dir and _path_is_within(output_dir_resolved, source_target_dir):
        excluded_roots.append(output_dir_resolved)

    shutil.copytree(
        source_target_dir,
        build_dir,
        ignore=_tmux_snapshot_ignore_names(excluded_roots=excluded_roots),
    )
    build_dir.replace(snapshot_dir)
    metadata_path.write_text(
        json.dumps(
            {
                "prepared_at": utc_now(),
                "source_target_dir": source_target_dir_str,
                "workspace_target_dir": str(snapshot_dir),
                "excluded_roots": [str(path) for path in excluded_roots],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return {
        "workspace_target_dir": str(snapshot_dir),
        "workspace_target_source_dir": source_target_dir_str,
        "workspace_target_status": "created",
        "workspace_target_reused": False,
    }


def _build_tmux_session_environment(
    output_dir: pathlib.Path,
    worker_name: str,
    payload: Dict[str, Any],
) -> Dict[str, Any]:
    raw_session_state = payload.get("session_state", {})
    session_state = raw_session_state if isinstance(raw_session_state, dict) else {}
    session_id = str(session_state.get("session_id", "") or worker_name)
    transport_session_name = preferred_tmux_session_name(worker_name)
    workspace_root = output_dir / TMUX_SESSION_WORKSPACE_DIRNAME / worker_name / session_id
    workspace_tmp_dir = workspace_root / "tmp"
    workspace_home_dir = workspace_root / "home"
    workspace_root.mkdir(parents=True, exist_ok=True)
    workspace_tmp_dir.mkdir(parents=True, exist_ok=True)
    workspace_home_dir.mkdir(parents=True, exist_ok=True)
    workspace_target = _prepare_tmux_workspace_target_dir(
        output_dir=output_dir,
        workspace_root=workspace_root,
        payload=payload,
    )
    workspace_workdir = pathlib.Path(
        str(workspace_target.get("workspace_target_dir", "") or workspace_root)
    ).resolve()
    workspace_workdir.mkdir(parents=True, exist_ok=True)
    workspace_home_cache_dir = workspace_home_dir / ".cache"
    workspace_home_config_dir = workspace_home_dir / ".config"
    workspace_home_data_dir = workspace_home_dir / ".local" / "share"
    workspace_home_appdata_dir = workspace_home_dir / "AppData" / "Roaming"
    workspace_home_localappdata_dir = workspace_home_dir / "AppData" / "Local"
    for path in (
        workspace_home_cache_dir,
        workspace_home_config_dir,
        workspace_home_data_dir,
        workspace_home_appdata_dir,
        workspace_home_localappdata_dir,
    ):
        path.mkdir(parents=True, exist_ok=True)
    worker_env = {
        "AGENT_TEAM_AGENT": str(worker_name),
        "AGENT_TEAM_SESSION_ID": session_id,
        "AGENT_TEAM_SESSION_DIR": str(workspace_root),
        "AGENT_TEAM_SESSION_WORKDIR": str(workspace_workdir),
        "AGENT_TEAM_SESSION_HOME": str(workspace_home_dir),
        "AGENT_TEAM_SESSION_TMP_DIR": str(workspace_tmp_dir),
        "AGENT_TEAM_WORKSPACE_SCOPE": "tmux_session_workspace",
        "HOME": str(workspace_home_dir),
        "USERPROFILE": str(workspace_home_dir),
        "XDG_CACHE_HOME": str(workspace_home_cache_dir),
        "XDG_CONFIG_HOME": str(workspace_home_config_dir),
        "XDG_DATA_HOME": str(workspace_home_data_dir),
        "APPDATA": str(workspace_home_appdata_dir),
        "LOCALAPPDATA": str(workspace_home_localappdata_dir),
        "TMPDIR": str(workspace_tmp_dir),
        "TMP": str(workspace_tmp_dir),
        "TEMP": str(workspace_tmp_dir),
    }
    if workspace_target.get("workspace_target_dir"):
        worker_env["AGENT_TEAM_WORKSPACE_TARGET_DIR"] = str(workspace_target.get("workspace_target_dir", ""))
    if workspace_target.get("workspace_target_source_dir"):
        worker_env["AGENT_TEAM_WORKSPACE_SOURCE_DIR"] = str(
            workspace_target.get("workspace_target_source_dir", "")
        )
    return {
        "worker_env": worker_env,
        "transport_session_name": transport_session_name,
        "workspace_root": str(workspace_root),
        "workspace_workdir": str(workspace_workdir),
        "workspace_home_dir": str(workspace_home_dir),
        "workspace_target_dir": str(workspace_target.get("workspace_target_dir", "")),
        "workspace_target_source_dir": str(workspace_target.get("workspace_target_source_dir", "")),
        "workspace_target_status": str(workspace_target.get("workspace_target_status", "")),
        "workspace_target_reused": bool(workspace_target.get("workspace_target_reused", False)),
        "workspace_tmp_dir": str(workspace_tmp_dir),
        "workspace_scope": "tmux_session_workspace",
        "workspace_isolation_active": True,
    }


def _reuse_tmux_session(
    command: List[str],
    session_name: str,
    stdout_file: pathlib.Path,
    stderr_file: pathlib.Path,
    status_file: pathlib.Path,
    worker_env: Optional[Dict[str, str]] = None,
    session_workdir: Optional[pathlib.Path] = None,
) -> Dict[str, Any]:
    shell_worker_env = dict(worker_env or {})
    shell_worker_env["AGENT_TEAM_TRANSPORT_SESSION"] = str(session_name)
    shell_cmd = _build_tmux_shell_command(
        command=command,
        stdout_file=stdout_file,
        stderr_file=stderr_file,
        status_file=status_file,
        worker_env=shell_worker_env,
    )
    reuse_command = ["tmux", "respawn-pane", "-k"]
    if session_workdir is not None:
        reuse_command.extend(["-c", str(session_workdir)])
    reuse_command.extend(["-t", f"{session_name}:0.0", shell_cmd])
    reused = subprocess.run(
        reuse_command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    stderr = (reused.stderr or "").strip()
    if reused.returncode == 0:
        return {
            "ok": True,
            "result": "respawned",
            "error": "",
            "completed": reused,
        }
    return {
        "ok": False,
        "result": "respawn_failed",
        "error": stderr[:400],
        "completed": reused,
    }


def _spawn_tmux_session(
    command: List[str],
    ipc_dir: pathlib.Path,
    session_prefix: str,
    preferred_session_found: bool = False,
    allow_existing_session_reuse: bool = False,
    worker_env: Optional[Dict[str, str]] = None,
    session_workdir: Optional[pathlib.Path] = None,
) -> Dict[str, Any]:
    attempts = 0
    retry_reason = ""
    last_error = ""
    last_spawn: Optional[subprocess.CompletedProcess[str]] = None
    session_name = ""
    stdout_file = ipc_dir / "stdout.txt"
    stderr_file = ipc_dir / "stderr.txt"
    status_file = ipc_dir / "status.txt"
    stale_cleanup_attempted = False
    stale_cleanup_session_name = ""
    stale_cleanup_result = ""
    stale_cleanup_error = ""
    stale_cleanup_retry_attempted = False
    stale_cleanup_retry_result = ""
    stale_cleanup_retry_error = ""
    stale_session_exists_after_cleanup = False
    stale_cleanup_verification_error = ""
    preferred_session_name = session_prefix
    preferred_session_retried = False
    preferred_session_reused = False
    session_name_strategy = ""
    preferred_session_reuse_attempted = False
    preferred_session_reuse_result = ""
    preferred_session_reuse_error = ""
    preferred_session_reused_existing = False
    candidate_names = _build_tmux_session_name_candidates(
        session_prefix=session_prefix,
        max_attempts=TMUX_SESSION_SPAWN_MAX_ATTEMPTS,
    )

    while attempts < TMUX_SESSION_SPAWN_MAX_ATTEMPTS:
        attempts += 1
        session_name = candidate_names[attempts - 1]
        session_name_strategy = "preferred" if session_name == preferred_session_name else "unique_suffix"
        stdout_file, stderr_file, status_file = _build_tmux_ipc_paths(ipc_dir=ipc_dir, session_name=session_name)
        shell_worker_env = dict(worker_env or {})
        shell_worker_env["AGENT_TEAM_TRANSPORT_SESSION"] = str(session_name)
        shell_cmd = _build_tmux_shell_command(
            command=command,
            stdout_file=stdout_file,
            stderr_file=stderr_file,
            status_file=status_file,
            worker_env=shell_worker_env,
        )
        spawn_command = ["tmux", "new-session", "-d", "-s", session_name]
        if session_workdir is not None:
            spawn_command.extend(["-c", str(session_workdir)])
        spawn_command.append(shell_cmd)
        last_spawn = subprocess.run(
            spawn_command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        if last_spawn.returncode == 0:
            preferred_session_reused = session_name == preferred_session_name
            return {
                "ok": True,
                "session_name": session_name,
                "preferred_session_name": preferred_session_name,
                "session_name_strategy": session_name_strategy,
                "preferred_session_retried": preferred_session_retried,
                "preferred_session_reused": preferred_session_reused,
                "preferred_session_reuse_attempted": preferred_session_reuse_attempted,
                "preferred_session_reuse_result": preferred_session_reuse_result[:400],
                "preferred_session_reuse_error": preferred_session_reuse_error[:400],
                "preferred_session_reused_existing": preferred_session_reused_existing,
                "stdout_file": stdout_file,
                "stderr_file": stderr_file,
                "status_file": status_file,
                "spawn": last_spawn,
                "attempts": attempts,
                "retried": attempts > 1,
                "retry_reason": retry_reason[:400],
                "spawn_error": "",
                "stale_cleanup_attempted": stale_cleanup_attempted,
                "stale_cleanup_session_name": stale_cleanup_session_name,
                "stale_cleanup_result": stale_cleanup_result[:400],
                "stale_cleanup_error": stale_cleanup_error[:400],
                "stale_cleanup_retry_attempted": stale_cleanup_retry_attempted,
                "stale_cleanup_retry_result": stale_cleanup_retry_result[:400],
                "stale_cleanup_retry_error": stale_cleanup_retry_error[:400],
                "stale_session_exists_after_cleanup": stale_session_exists_after_cleanup,
                "stale_cleanup_verification_error": stale_cleanup_verification_error[:400],
            }

        last_error = (last_spawn.stderr or "").strip()
        if _is_duplicate_tmux_session_error(last_error) and attempts < TMUX_SESSION_SPAWN_MAX_ATTEMPTS:
            retry_reason = last_error
            if (
                allow_existing_session_reuse
                and session_name == preferred_session_name
                and preferred_session_found
            ):
                preferred_session_reuse_attempted = True
                reuse = _reuse_tmux_session(
                    command=command,
                    session_name=session_name,
                    stdout_file=stdout_file,
                    stderr_file=stderr_file,
                    status_file=status_file,
                    worker_env=worker_env,
                    session_workdir=session_workdir,
                )
                preferred_session_reuse_result = str(reuse["result"])
                preferred_session_reuse_error = str(reuse["error"])
                if reuse["ok"]:
                    preferred_session_reused = True
                    preferred_session_reused_existing = True
                    return {
                        "ok": True,
                        "session_name": session_name,
                        "preferred_session_name": preferred_session_name,
                        "session_name_strategy": "preferred_reused_existing",
                        "preferred_session_retried": preferred_session_retried,
                        "preferred_session_reused": preferred_session_reused,
                        "preferred_session_reuse_attempted": preferred_session_reuse_attempted,
                        "preferred_session_reuse_result": preferred_session_reuse_result[:400],
                        "preferred_session_reuse_error": preferred_session_reuse_error[:400],
                        "preferred_session_reused_existing": preferred_session_reused_existing,
                        "stdout_file": stdout_file,
                        "stderr_file": stderr_file,
                        "status_file": status_file,
                        "spawn": reuse["completed"],
                        "attempts": attempts,
                        "retried": False,
                        "retry_reason": retry_reason[:400],
                        "spawn_error": "",
                        "stale_cleanup_attempted": stale_cleanup_attempted,
                        "stale_cleanup_session_name": stale_cleanup_session_name,
                        "stale_cleanup_result": stale_cleanup_result[:400],
                        "stale_cleanup_error": stale_cleanup_error[:400],
                        "stale_cleanup_retry_attempted": stale_cleanup_retry_attempted,
                        "stale_cleanup_retry_result": stale_cleanup_retry_result[:400],
                        "stale_cleanup_retry_error": stale_cleanup_retry_error[:400],
                        "stale_session_exists_after_cleanup": stale_session_exists_after_cleanup,
                        "stale_cleanup_verification_error": stale_cleanup_verification_error[:400],
                    }
            if session_name == preferred_session_name:
                preferred_session_retried = True
            stale_cleanup = _cleanup_stale_tmux_session(session_name=session_name)
            stale_cleanup_attempted = bool(stale_cleanup["attempted"])
            stale_cleanup_session_name = str(stale_cleanup["session_name"])
            stale_cleanup_result = str(stale_cleanup["result"])
            stale_cleanup_error = str(stale_cleanup["error"])
            stale_cleanup_retry_attempted = bool(stale_cleanup["retry_attempted"])
            stale_cleanup_retry_result = str(stale_cleanup["retry_result"])
            stale_cleanup_retry_error = str(stale_cleanup["retry_error"])
            stale_session_exists_after_cleanup = bool(stale_cleanup["session_exists_after_cleanup"])
            stale_cleanup_verification_error = str(stale_cleanup["verification_error"])
            continue
        break

    return {
        "ok": False,
        "session_name": session_name,
        "preferred_session_name": preferred_session_name,
        "session_name_strategy": session_name_strategy,
        "preferred_session_retried": preferred_session_retried,
        "preferred_session_reused": preferred_session_reused,
        "preferred_session_reuse_attempted": preferred_session_reuse_attempted,
        "preferred_session_reuse_result": preferred_session_reuse_result[:400],
        "preferred_session_reuse_error": preferred_session_reuse_error[:400],
        "preferred_session_reused_existing": preferred_session_reused_existing,
        "stdout_file": stdout_file,
        "stderr_file": stderr_file,
        "status_file": status_file,
        "spawn": last_spawn,
        "attempts": attempts,
        "retried": attempts > 1,
        "retry_reason": retry_reason[:400],
        "spawn_error": last_error[:400],
        "stale_cleanup_attempted": stale_cleanup_attempted,
        "stale_cleanup_session_name": stale_cleanup_session_name,
        "stale_cleanup_result": stale_cleanup_result[:400],
        "stale_cleanup_error": stale_cleanup_error[:400],
        "stale_cleanup_retry_attempted": stale_cleanup_retry_attempted,
        "stale_cleanup_retry_result": stale_cleanup_retry_result[:400],
        "stale_cleanup_retry_error": stale_cleanup_retry_error[:400],
        "stale_session_exists_after_cleanup": stale_session_exists_after_cleanup,
        "stale_cleanup_verification_error": stale_cleanup_verification_error[:400],
    }


def _kill_tmux_session(session_name: str) -> Dict[str, Any]:
    kill = subprocess.run(
        ["tmux", "kill-session", "-t", session_name],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    stderr = (kill.stderr or "").strip()
    if kill.returncode == 0:
        result = "killed"
    elif "can't find session" in stderr.casefold():
        result = "already_exited"
    else:
        result = "failed"
    return {
        "result": result,
        "returncode": kill.returncode,
        "error": stderr[:400],
    }


def _list_tmux_sessions() -> Dict[str, Any]:
    listed = subprocess.run(
        ["tmux", "list-sessions", "-F", "#{session_name}"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    stderr = (listed.stderr or "").strip()
    stdout = (listed.stdout or "").strip()
    if listed.returncode == 0:
        sessions = [line.strip() for line in stdout.splitlines() if line.strip()]
        return {
            "sessions": sessions,
            "error": "",
            "server_running": True,
        }
    if _is_no_tmux_server_error(stderr or stdout):
        return {
            "sessions": [],
            "error": "",
            "server_running": False,
        }
    return {
        "sessions": [],
        "error": (stderr or stdout)[:400],
        "server_running": True,
    }


def _cleanup_tmux_session_with_recovery(session_name: str) -> Dict[str, Any]:
    cleanup = _kill_tmux_session(session_name)
    retry_attempted = False
    retry_result = ""
    retry_error = ""
    session_exists_after_cleanup = False
    verification_error = ""

    if cleanup["result"] not in {"killed", "already_exited"}:
        existence = subprocess.run(
            ["tmux", "has-session", "-t", session_name],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        stderr = (existence.stderr or "").strip()
        stdout = (existence.stdout or "").strip()
        message = stderr or stdout
        if existence.returncode == 0:
            session_exists_after_cleanup = True
        elif existence.returncode == 1 or "can't find session" in message.casefold():
            session_exists_after_cleanup = False
        else:
            verification_error = message[:400]

        if session_exists_after_cleanup:
            retry_attempted = True
            retry = _kill_tmux_session(session_name)
            retry_result = retry["result"]
            retry_error = retry["error"]
            if retry["result"] in {"killed", "already_exited"}:
                cleanup = {
                    "result": "recovered_after_retry",
                    "returncode": retry["returncode"],
                    "error": retry["error"],
                }
            else:
                cleanup = {
                    "result": "cleanup_retry_failed",
                    "returncode": retry["returncode"],
                    "error": retry["error"] or cleanup["error"],
                }

    return {
        "session_name": session_name,
        "result": cleanup["result"],
        "error": cleanup["error"],
        "retry_attempted": retry_attempted,
        "retry_result": retry_result,
        "retry_error": retry_error,
        "session_exists_after_cleanup": session_exists_after_cleanup,
        "verification_error": verification_error,
    }


def _cleanup_stale_tmux_session(session_name: str) -> Dict[str, Any]:
    cleanup = _cleanup_tmux_session_with_recovery(session_name)
    cleanup["attempted"] = True
    return cleanup


def _cleanup_active_tmux_session(session_name: str) -> Dict[str, Any]:
    cleanup = _cleanup_tmux_session_with_recovery(session_name)
    cleanup["attempted"] = True
    return cleanup


def _cleanup_orphan_tmux_sessions(session_prefix: str, keep_exact_session: bool = False) -> Dict[str, Any]:
    listed = _list_tmux_sessions()
    preferred_session_found = False
    matched = [
        name
        for name in listed["sessions"]
        if str(name).startswith(f"{session_prefix}_")
    ]
    for name in listed["sessions"]:
        if str(name) == session_prefix:
            preferred_session_found = True
            if not keep_exact_session:
                matched.append(name)
    cleaned = 0
    failed: List[str] = []

    for session_name in matched:
        cleanup = _cleanup_tmux_session_with_recovery(session_name)
        if cleanup["result"] in {"killed", "already_exited", "recovered_after_retry"}:
            cleaned += 1
        else:
            failed.append(session_name)

    return {
        "attempted": True,
        "server_running": bool(listed["server_running"]),
        "preferred_session_found": preferred_session_found,
        "sessions_found": len(matched),
        "sessions_cleaned": cleaned,
        "sessions_failed": len(failed),
        "failed_sessions": failed,
        "error": str(listed["error"]),
    }


def _cleanup_tmux_ipc_files(paths: Sequence[pathlib.Path]) -> Dict[str, Any]:
    removed = 0
    errors: List[str] = []
    for path in paths:
        existed = path.exists()
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            errors.append(f"{path.name}: {exc}")
            continue
        if existed:
            removed += 1

    if errors:
        result = "partial_failed" if removed else "failed"
    elif removed > 0:
        result = "removed"
    else:
        result = "already_absent"
    return {
        "result": result,
        "removed": removed,
        "error": "; ".join(errors)[:400],
    }


def _worker_discover_markdown(target_dir: pathlib.Path, output_dir: pathlib.Path) -> Dict[str, Any]:
    inventory: List[Dict[str, Any]] = []
    ignore_output_dir = output_dir.resolve() != target_dir.resolve() and _path_is_within(output_dir, target_dir)
    for path in sorted(p for p in target_dir.rglob("*.md") if p.is_file()):
        if ignore_output_dir and _path_is_within(path, output_dir):
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


def _worker_discover_repository(target_dir: pathlib.Path, output_dir: pathlib.Path) -> Dict[str, Any]:
    inventory: List[Dict[str, Any]] = []
    ignore_output_dir = output_dir.resolve() != target_dir.resolve() and _path_is_within(output_dir, target_dir)
    for path in sorted(p for p in target_dir.rglob("*") if p.is_file()):
        if ignore_output_dir and _path_is_within(path, output_dir):
            continue
        relative_path = path.relative_to(target_dir)
        if ".git" in relative_path.parts:
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        lines = text.splitlines()
        inventory.append(
            {
                "path": str(relative_path),
                "extension": path.suffix.strip().lower() or "<no_ext>",
                "line_count": len(lines),
                "byte_count": path.stat().st_size,
                "top_level_dir": relative_path.parts[0] if len(relative_path.parts) > 1 else ".",
            }
        )
    return {
        "result": {"repository_files": len(inventory), "sample": inventory[:3]},
        "state_updates": {"repository_inventory": inventory},
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


def _worker_extension_audit(inventory: List[Dict[str, Any]]) -> Dict[str, Any]:
    totals: Dict[str, Dict[str, Any]] = {}
    for item in inventory:
        extension = str(item.get("extension", "<no_ext>") or "<no_ext>")
        bucket = totals.setdefault(
            extension,
            {
                "extension": extension,
                "file_count": 0,
                "total_lines": 0,
                "total_bytes": 0,
            },
        )
        bucket["file_count"] += 1
        bucket["total_lines"] += int(item.get("line_count", 0))
        bucket["total_bytes"] += int(item.get("byte_count", 0))
    ranked = sorted(
        totals.values(),
        key=lambda row: (int(row["file_count"]), int(row["total_lines"]), str(row["extension"])),
        reverse=True,
    )
    result = {
        "total_files": len(inventory),
        "unique_extensions": len(totals),
        "files_without_extension": int(totals.get("<no_ext>", {}).get("file_count", 0)),
        "top_extensions": ranked[:6],
    }
    return {
        "result": result,
        "state_updates": {"repository_extension_summary": result},
    }


def _worker_large_file_audit(
    inventory: List[Dict[str, Any]],
    line_threshold: int,
    byte_threshold: int,
) -> Dict[str, Any]:
    oversized = [
        item
        for item in inventory
        if int(item.get("line_count", 0)) >= line_threshold
        or int(item.get("byte_count", 0)) >= byte_threshold
    ]
    ranked = sorted(
        oversized,
        key=lambda row: (int(row.get("line_count", 0)), int(row.get("byte_count", 0)), str(row.get("path", ""))),
        reverse=True,
    )
    return {
        "result": {
            "line_threshold": line_threshold,
            "byte_threshold": byte_threshold,
            "oversized_files": len(ranked),
            "examples": [str(item.get("path", "")) for item in ranked[:10]],
        },
        "state_updates": {"repository_large_files": ranked},
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


def _worker_extension_hotspot_followup(inventory: List[Dict[str, Any]], top_n: int) -> Dict[str, Any]:
    totals: Dict[str, Dict[str, Any]] = {}
    for item in inventory:
        extension = str(item.get("extension", "<no_ext>") or "<no_ext>")
        bucket = totals.setdefault(
            extension,
            {
                "extension": extension,
                "file_count": 0,
                "total_lines": 0,
                "total_bytes": 0,
            },
        )
        bucket["file_count"] += 1
        bucket["total_lines"] += int(item.get("line_count", 0))
        bucket["total_bytes"] += int(item.get("byte_count", 0))
    hotspots = sorted(
        totals.values(),
        key=lambda row: (int(row["total_lines"]), int(row["file_count"]), str(row["extension"])),
        reverse=True,
    )[:top_n]
    result = {"top_n": top_n, "extension_hotspots": hotspots}
    return {
        "result": result,
        "state_updates": {"repo_extension_hotspots": result},
    }


def _worker_directory_hotspot_followup(inventory: List[Dict[str, Any]], top_n: int) -> Dict[str, Any]:
    totals: Dict[str, Dict[str, Any]] = {}
    for item in inventory:
        top_level_dir = str(item.get("top_level_dir", ".") or ".")
        bucket = totals.setdefault(
            top_level_dir,
            {
                "top_level_dir": top_level_dir,
                "file_count": 0,
                "total_lines": 0,
                "total_bytes": 0,
            },
        )
        bucket["file_count"] += 1
        bucket["total_lines"] += int(item.get("line_count", 0))
        bucket["total_bytes"] += int(item.get("byte_count", 0))
    busiest = sorted(
        totals.values(),
        key=lambda row: (int(row["total_lines"]), int(row["file_count"]), str(row["top_level_dir"])),
        reverse=True,
    )[:top_n]
    result = {"top_n": top_n, "busiest_directories": busiest}
    return {
        "result": result,
        "state_updates": {"repo_directory_hotspots": result},
    }


def run_tmux_worker_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    task_type = str(payload.get("task_type", "")).strip()
    task_payload = payload.get("task_payload", {})
    if not isinstance(task_payload, dict):
        task_payload = {}
    task_context = payload.get("task_context", {})
    if not isinstance(task_context, dict):
        task_context = {}
    shared_state = task_context.get("visible_shared_state", payload.get("shared_state", {}))
    if not isinstance(shared_state, dict):
        shared_state = {}

    if task_type == "discover_markdown":
        target_dir = pathlib.Path(str(payload.get("target_dir", "."))).resolve()
        output_dir = pathlib.Path(str(payload.get("output_dir", "."))).resolve()
        return _worker_discover_markdown(target_dir=target_dir, output_dir=output_dir)
    if task_type == "discover_repository":
        target_dir = pathlib.Path(str(payload.get("target_dir", "."))).resolve()
        output_dir = pathlib.Path(str(payload.get("output_dir", "."))).resolve()
        return _worker_discover_repository(target_dir=target_dir, output_dir=output_dir)

    inventory = shared_state.get("markdown_inventory", [])
    if not isinstance(inventory, list):
        inventory = []
    repository_inventory = shared_state.get("repository_inventory", [])
    if not isinstance(repository_inventory, list):
        repository_inventory = []

    if task_type == "heading_audit":
        return _worker_heading_audit(inventory=inventory)
    if task_type == "length_audit":
        threshold = int(task_payload.get("line_threshold", 200))
        return _worker_length_audit(inventory=inventory, threshold=threshold)
    if task_type == "extension_audit":
        return _worker_extension_audit(inventory=repository_inventory)
    if task_type == "large_file_audit":
        line_threshold = int(task_payload.get("line_threshold", 320))
        byte_threshold = int(task_payload.get("byte_threshold", 20000))
        return _worker_large_file_audit(
            inventory=repository_inventory,
            line_threshold=line_threshold,
            byte_threshold=byte_threshold,
        )
    if task_type == "heading_structure_followup":
        top_n = int(task_payload.get("top_n", 8))
        return _worker_heading_followup(inventory=inventory, top_n=top_n)
    if task_type == "length_risk_followup":
        top_n = int(task_payload.get("top_n", 8))
        threshold = int(task_payload.get("line_threshold", 180))
        return _worker_length_followup(inventory=inventory, threshold=threshold, top_n=top_n)
    if task_type == "extension_hotspot_followup":
        top_n = int(task_payload.get("top_n", 6))
        return _worker_extension_hotspot_followup(inventory=repository_inventory, top_n=top_n)
    if task_type == "directory_hotspot_followup":
        top_n = int(task_payload.get("top_n", 6))
        return _worker_directory_hotspot_followup(inventory=repository_inventory, top_n=top_n)

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


def execute_worker_subprocess(
    command: List[str],
    timeout_sec: int,
    worker_env: Optional[Dict[str, str]] = None,
    workdir: Optional[pathlib.Path] = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
        timeout=timeout_sec,
        env=_worker_subprocess_env(worker_env),
        cwd=str(workdir) if workdir is not None else None,
    )


def execute_worker_tmux(
    command: List[str],
    workdir: pathlib.Path,
    session_prefix: str,
    timeout_sec: int,
    retain_session_for_reuse: bool = False,
    allow_existing_session_reuse: bool = False,
    worker_env: Optional[Dict[str, str]] = None,
    session_workspace_root: str = "",
    session_workspace_workdir: str = "",
    session_workspace_home_dir: str = "",
    session_workspace_target_dir: str = "",
    session_workspace_tmp_dir: str = "",
) -> subprocess.CompletedProcess[str]:
    ipc_dir = workdir / "_tmux_worker_ipc"
    ipc_dir.mkdir(parents=True, exist_ok=True)
    lifecycle: Dict[str, Any] = {
        "tmux_session_name": "",
        "tmux_preferred_session_name": "",
        "tmux_session_name_strategy": "",
        "tmux_preferred_session_found_preflight": False,
        "tmux_preferred_session_retried": False,
        "tmux_preferred_session_reused": False,
        "tmux_preferred_session_reuse_attempted": False,
        "tmux_preferred_session_reuse_result": "",
        "tmux_preferred_session_reuse_error": "",
        "tmux_preferred_session_reused_existing": False,
        "tmux_preferred_session_reuse_authorized": allow_existing_session_reuse,
        "tmux_reuse_retention_requested": retain_session_for_reuse,
        "tmux_session_retained_for_reuse": False,
        "tmux_session_started": False,
        "tmux_orphan_cleanup_attempted": False,
        "tmux_orphan_server_running": False,
        "tmux_orphan_sessions_found": 0,
        "tmux_orphan_sessions_cleaned": 0,
        "tmux_orphan_sessions_failed": 0,
        "tmux_orphan_failed_sessions": [],
        "tmux_orphan_cleanup_error": "",
        "tmux_spawn_attempts": 0,
        "tmux_spawn_retried": False,
        "tmux_spawn_retry_reason": "",
        "tmux_spawn_error": "",
        "tmux_stale_session_cleanup_attempted": False,
        "tmux_stale_session_name": "",
        "tmux_stale_session_cleanup_result": "",
        "tmux_stale_session_cleanup_error": "",
        "tmux_stale_session_cleanup_retry_attempted": False,
        "tmux_stale_session_cleanup_retry_result": "",
        "tmux_stale_session_cleanup_retry_error": "",
        "tmux_stale_session_exists_after_cleanup": False,
        "tmux_stale_session_cleanup_verification_error": "",
        "tmux_cleanup_retry_attempted": False,
        "tmux_cleanup_retry_result": "",
        "tmux_cleanup_retry_error": "",
        "tmux_session_exists_after_cleanup": False,
        "tmux_cleanup_verification_error": "",
        "tmux_status_observed": False,
        "tmux_timed_out": False,
        "tmux_cleanup_result": "",
        "tmux_cleanup_error": "",
        "tmux_ipc_cleanup_result": "",
        "tmux_ipc_cleanup_error": "",
        "tmux_ipc_files_removed": 0,
        "tmux_session_workspace_root": str(session_workspace_root or ""),
        "tmux_session_workspace_workdir": str(session_workspace_workdir or ""),
        "tmux_session_workspace_home_dir": str(session_workspace_home_dir or ""),
        "tmux_session_workspace_target_dir": str(session_workspace_target_dir or ""),
        "tmux_session_workspace_tmp_dir": str(session_workspace_tmp_dir or ""),
        "tmux_session_workspace_scope": "tmux_session_workspace" if session_workspace_root else "",
        "tmux_session_workspace_isolated": bool(session_workspace_root),
        "tmux_session_env_keys": sorted({str(key) for key in (worker_env or {}).keys()}),
    }

    orphan_cleanup = _cleanup_orphan_tmux_sessions(
        session_prefix=session_prefix,
        keep_exact_session=allow_existing_session_reuse,
    )
    lifecycle["tmux_orphan_cleanup_attempted"] = bool(orphan_cleanup["attempted"])
    lifecycle["tmux_orphan_server_running"] = bool(orphan_cleanup["server_running"])
    lifecycle["tmux_preferred_session_found_preflight"] = bool(orphan_cleanup.get("preferred_session_found", False))
    lifecycle["tmux_orphan_sessions_found"] = int(orphan_cleanup["sessions_found"])
    lifecycle["tmux_orphan_sessions_cleaned"] = int(orphan_cleanup["sessions_cleaned"])
    lifecycle["tmux_orphan_sessions_failed"] = int(orphan_cleanup["sessions_failed"])
    lifecycle["tmux_orphan_failed_sessions"] = list(orphan_cleanup["failed_sessions"])
    lifecycle["tmux_orphan_cleanup_error"] = str(orphan_cleanup["error"])

    spawn_result = _spawn_tmux_session(
        command=command,
        ipc_dir=ipc_dir,
        session_prefix=session_prefix,
        preferred_session_found=bool(orphan_cleanup.get("preferred_session_found", False)),
        allow_existing_session_reuse=allow_existing_session_reuse,
        worker_env=worker_env,
        session_workdir=pathlib.Path(session_workspace_workdir).resolve() if session_workspace_workdir else None,
    )
    session_name = str(spawn_result.get("session_name", ""))
    stdout_file = pathlib.Path(spawn_result.get("stdout_file"))
    stderr_file = pathlib.Path(spawn_result.get("stderr_file"))
    status_file = pathlib.Path(spawn_result.get("status_file"))
    ipc_files = [stdout_file, stderr_file, status_file]
    spawn = spawn_result.get("spawn")
    if not isinstance(spawn, subprocess.CompletedProcess):
        spawn = subprocess.CompletedProcess(args=["tmux"], returncode=1, stdout="", stderr="tmux spawn failed")

    lifecycle["tmux_session_name"] = session_name
    lifecycle["tmux_preferred_session_name"] = str(spawn_result.get("preferred_session_name", ""))
    lifecycle["tmux_session_name_strategy"] = str(spawn_result.get("session_name_strategy", ""))
    lifecycle["tmux_preferred_session_retried"] = bool(spawn_result.get("preferred_session_retried", False))
    lifecycle["tmux_preferred_session_reused"] = bool(spawn_result.get("preferred_session_reused", False))
    lifecycle["tmux_preferred_session_reuse_attempted"] = bool(
        spawn_result.get("preferred_session_reuse_attempted", False)
    )
    lifecycle["tmux_preferred_session_reuse_result"] = str(
        spawn_result.get("preferred_session_reuse_result", "")
    )
    lifecycle["tmux_preferred_session_reuse_error"] = str(
        spawn_result.get("preferred_session_reuse_error", "")
    )
    lifecycle["tmux_preferred_session_reused_existing"] = bool(
        spawn_result.get("preferred_session_reused_existing", False)
    )
    lifecycle["tmux_session_started"] = spawn.returncode == 0
    lifecycle["tmux_spawn_attempts"] = int(spawn_result.get("attempts", 0))
    lifecycle["tmux_spawn_retried"] = bool(spawn_result.get("retried", False))
    lifecycle["tmux_spawn_retry_reason"] = str(spawn_result.get("retry_reason", ""))
    lifecycle["tmux_spawn_error"] = str(spawn_result.get("spawn_error", ""))
    lifecycle["tmux_stale_session_cleanup_attempted"] = bool(spawn_result.get("stale_cleanup_attempted", False))
    lifecycle["tmux_stale_session_name"] = str(spawn_result.get("stale_cleanup_session_name", ""))
    lifecycle["tmux_stale_session_cleanup_result"] = str(spawn_result.get("stale_cleanup_result", ""))
    lifecycle["tmux_stale_session_cleanup_error"] = str(spawn_result.get("stale_cleanup_error", ""))
    lifecycle["tmux_stale_session_cleanup_retry_attempted"] = bool(
        spawn_result.get("stale_cleanup_retry_attempted", False)
    )
    lifecycle["tmux_stale_session_cleanup_retry_result"] = str(
        spawn_result.get("stale_cleanup_retry_result", "")
    )
    lifecycle["tmux_stale_session_cleanup_retry_error"] = str(
        spawn_result.get("stale_cleanup_retry_error", "")
    )
    lifecycle["tmux_stale_session_exists_after_cleanup"] = bool(
        spawn_result.get("stale_session_exists_after_cleanup", False)
    )
    lifecycle["tmux_stale_session_cleanup_verification_error"] = str(
        spawn_result.get("stale_cleanup_verification_error", "")
    )
    if spawn.returncode != 0:
        cleanup = _cleanup_tmux_ipc_files(ipc_files)
        lifecycle["tmux_cleanup_result"] = "spawn_failed"
        lifecycle["tmux_cleanup_error"] = str(spawn_result.get("spawn_error", "") or (spawn.stderr or "").strip())[:400]
        lifecycle["tmux_ipc_cleanup_result"] = cleanup["result"]
        lifecycle["tmux_ipc_cleanup_error"] = cleanup["error"]
        lifecycle["tmux_ipc_files_removed"] = cleanup["removed"]
        return _attach_tmux_lifecycle(
            subprocess.CompletedProcess(
                args=command,
                returncode=spawn.returncode,
                stdout="",
                stderr=f"tmux spawn failed: {spawn.stderr.strip()}",
            ),
            lifecycle,
        )

    deadline = time.time() + timeout_sec
    while time.time() < deadline and not status_file.exists():
        time.sleep(TMUX_SESSION_POLL_INTERVAL_SEC)

    lifecycle["tmux_status_observed"] = status_file.exists()
    if not status_file.exists():
        cleanup = _cleanup_active_tmux_session(session_name)
        lifecycle["tmux_cleanup_result"] = cleanup["result"]
        lifecycle["tmux_cleanup_error"] = cleanup["error"]
        lifecycle["tmux_cleanup_retry_attempted"] = bool(cleanup["retry_attempted"])
        lifecycle["tmux_cleanup_retry_result"] = str(cleanup["retry_result"])
        lifecycle["tmux_cleanup_retry_error"] = str(cleanup["retry_error"])
        lifecycle["tmux_session_exists_after_cleanup"] = bool(cleanup["session_exists_after_cleanup"])
        lifecycle["tmux_cleanup_verification_error"] = str(cleanup["verification_error"])
        lifecycle["tmux_timed_out"] = True
        stdout = stdout_file.read_text(encoding="utf-8", errors="ignore") if stdout_file.exists() else ""
        stderr = stderr_file.read_text(encoding="utf-8", errors="ignore") if stderr_file.exists() else ""
        if not stderr:
            stderr = "tmux worker timed out"
        ipc_cleanup = _cleanup_tmux_ipc_files(ipc_files)
        lifecycle["tmux_ipc_cleanup_result"] = ipc_cleanup["result"]
        lifecycle["tmux_ipc_cleanup_error"] = ipc_cleanup["error"]
        lifecycle["tmux_ipc_files_removed"] = ipc_cleanup["removed"]
        return _attach_tmux_lifecycle(
            subprocess.CompletedProcess(
                args=command,
                returncode=124,
                stdout=stdout,
                stderr=stderr,
            ),
            lifecycle,
        )

    try:
        returncode = int(status_file.read_text(encoding="utf-8").strip() or "1")
    except ValueError:
        returncode = 1
    stdout = stdout_file.read_text(encoding="utf-8", errors="ignore") if stdout_file.exists() else ""
    stderr = stderr_file.read_text(encoding="utf-8", errors="ignore") if stderr_file.exists() else ""
    if (
        returncode == 0
        and retain_session_for_reuse
        and session_name == str(spawn_result.get("preferred_session_name", ""))
        and str(spawn_result.get("session_name_strategy", "")) in {"preferred", "preferred_reused_existing"}
    ):
        lifecycle["tmux_cleanup_result"] = "leased_for_reuse"
        lifecycle["tmux_cleanup_error"] = ""
        lifecycle["tmux_cleanup_retry_attempted"] = False
        lifecycle["tmux_cleanup_retry_result"] = ""
        lifecycle["tmux_cleanup_retry_error"] = ""
        lifecycle["tmux_session_exists_after_cleanup"] = True
        lifecycle["tmux_cleanup_verification_error"] = ""
        lifecycle["tmux_session_retained_for_reuse"] = True
    else:
        cleanup = _cleanup_active_tmux_session(session_name)
        lifecycle["tmux_cleanup_result"] = cleanup["result"]
        lifecycle["tmux_cleanup_error"] = cleanup["error"]
        lifecycle["tmux_cleanup_retry_attempted"] = bool(cleanup["retry_attempted"])
        lifecycle["tmux_cleanup_retry_result"] = str(cleanup["retry_result"])
        lifecycle["tmux_cleanup_retry_error"] = str(cleanup["retry_error"])
        lifecycle["tmux_session_exists_after_cleanup"] = bool(cleanup["session_exists_after_cleanup"])
        lifecycle["tmux_cleanup_verification_error"] = str(cleanup["verification_error"])
    ipc_cleanup = _cleanup_tmux_ipc_files(ipc_files)
    lifecycle["tmux_ipc_cleanup_result"] = ipc_cleanup["result"]
    lifecycle["tmux_ipc_cleanup_error"] = ipc_cleanup["error"]
    lifecycle["tmux_ipc_files_removed"] = ipc_cleanup["removed"]
    return _attach_tmux_lifecycle(
        subprocess.CompletedProcess(args=command, returncode=returncode, stdout=stdout, stderr=stderr),
        lifecycle,
    )


def _merge_tmux_lifecycle_into_diagnostics(
    diagnostics: Dict[str, Any],
    completed: subprocess.CompletedProcess[str],
) -> Dict[str, Any]:
    lifecycle = _extract_tmux_lifecycle(completed)
    if not lifecycle:
        return {}
    for key in (
        "tmux_session_name",
        "tmux_preferred_session_name",
        "tmux_session_name_strategy",
        "tmux_preferred_session_found_preflight",
        "tmux_preferred_session_retried",
        "tmux_preferred_session_reused",
        "tmux_preferred_session_reuse_attempted",
        "tmux_preferred_session_reuse_result",
        "tmux_preferred_session_reuse_error",
        "tmux_preferred_session_reused_existing",
        "tmux_preferred_session_reuse_authorized",
        "tmux_reuse_retention_requested",
        "tmux_session_retained_for_reuse",
        "tmux_session_started",
        "tmux_orphan_cleanup_attempted",
        "tmux_orphan_server_running",
        "tmux_orphan_sessions_found",
        "tmux_orphan_sessions_cleaned",
        "tmux_orphan_sessions_failed",
        "tmux_orphan_failed_sessions",
        "tmux_orphan_cleanup_error",
        "tmux_spawn_attempts",
        "tmux_spawn_retried",
        "tmux_spawn_retry_reason",
        "tmux_spawn_error",
        "tmux_stale_session_cleanup_attempted",
        "tmux_stale_session_name",
        "tmux_stale_session_cleanup_result",
        "tmux_stale_session_cleanup_error",
        "tmux_stale_session_cleanup_retry_attempted",
        "tmux_stale_session_cleanup_retry_result",
        "tmux_stale_session_cleanup_retry_error",
        "tmux_stale_session_exists_after_cleanup",
        "tmux_stale_session_cleanup_verification_error",
        "tmux_cleanup_retry_attempted",
        "tmux_cleanup_retry_result",
        "tmux_cleanup_retry_error",
        "tmux_session_exists_after_cleanup",
        "tmux_cleanup_verification_error",
        "tmux_status_observed",
        "tmux_timed_out",
        "tmux_cleanup_result",
        "tmux_cleanup_error",
        "tmux_ipc_cleanup_result",
        "tmux_ipc_cleanup_error",
        "tmux_ipc_files_removed",
        "tmux_session_workspace_root",
        "tmux_session_workspace_workdir",
        "tmux_session_workspace_home_dir",
        "tmux_session_workspace_target_dir",
        "tmux_session_workspace_tmp_dir",
        "tmux_session_workspace_scope",
        "tmux_session_workspace_isolated",
        "tmux_session_env_keys",
    ):
        diagnostics[key] = lifecycle.get(key, diagnostics.get(key))
    return lifecycle


def _merge_transport_timeout_into_diagnostics(
    diagnostics: Dict[str, Any],
    completed: subprocess.CompletedProcess[str],
) -> Dict[str, Any]:
    timeout_metadata = _extract_transport_timeout(completed)
    if not timeout_metadata:
        return {}
    for key in (
        "execution_timed_out",
        "timeout_transport",
        "timeout_phase",
        "timeout_message",
    ):
        diagnostics[key] = timeout_metadata.get(key, diagnostics.get(key))
    return timeout_metadata


def run_tmux_worker_task(
    runtime_script: pathlib.Path,
    output_dir: pathlib.Path,
    runtime_config: RuntimeConfig,
    payload: Dict[str, Any],
    worker_name: str,
    logger: EventLogger,
    timeout_sec: int = 120,
    retain_session_for_reuse: bool = False,
    allow_existing_session_reuse: bool = False,
    execute_worker_tmux_fn: Callable[
        [List[str], pathlib.Path, str, int, bool, bool, Optional[Dict[str, str]], str, str, str, str, str],
        subprocess.CompletedProcess[str],
    ] = execute_worker_tmux,
    execute_worker_subprocess_fn: Callable[
        [List[str], int, Optional[Dict[str, str]], Optional[pathlib.Path]],
        subprocess.CompletedProcess[str],
    ] = execute_worker_subprocess,
    which_fn: Callable[[str], str | None] = shutil.which,
) -> Dict[str, Any]:
    session_environment = _build_tmux_session_environment(
        output_dir=output_dir,
        worker_name=worker_name,
        payload=payload,
    )
    payload_for_transport = dict(payload)
    if session_environment.get("workspace_target_dir"):
        payload_for_transport["target_dir"] = str(session_environment.get("workspace_target_dir", ""))
    payload_dir = output_dir / "_tmux_worker_payloads"
    payload_dir.mkdir(parents=True, exist_ok=True)
    payload_file = payload_dir / f"{worker_name}_{uuid.uuid4().hex}.json"
    payload_file.write_text(json.dumps(payload_for_transport, ensure_ascii=False), encoding="utf-8")
    worker_env = dict(session_environment.get("worker_env", {}))

    command = [
        sys.executable,
        str(runtime_script),
        "--worker-task-file",
        str(payload_file),
    ]
    transport = "subprocess"
    completed: subprocess.CompletedProcess[str]
    started_at = time.time()
    requested_transport = str(runtime_config.teammate_mode)
    fallback_reason = ""
    diagnostics: Dict[str, Any] = {
        "recorded_at": utc_now(),
        "worker": worker_name,
        "task_type": str(payload.get("task_type", "")),
        "transport_requested": requested_transport,
        "transport_used": "",
        "fallback_used": False,
        "fallback_reason": "",
        "timeout_sec": timeout_sec,
        "returncode": "",
        "duration_ms": 0,
        "stdout_preview": "",
        "stderr_preview": "",
        "result": "unknown",
        "error": "",
        "execution_timed_out": False,
        "timeout_transport": "",
        "timeout_phase": "",
        "timeout_message": "",
        "tmux_session_name": "",
        "tmux_preferred_session_name": "",
        "tmux_session_name_strategy": "",
        "tmux_preferred_session_found_preflight": False,
        "tmux_preferred_session_retried": False,
        "tmux_preferred_session_reused": False,
        "tmux_preferred_session_reuse_attempted": False,
        "tmux_preferred_session_reuse_result": "",
        "tmux_preferred_session_reuse_error": "",
        "tmux_preferred_session_reused_existing": False,
        "tmux_preferred_session_reuse_authorized": allow_existing_session_reuse,
        "tmux_reuse_retention_requested": retain_session_for_reuse,
        "tmux_session_retained_for_reuse": False,
        "tmux_session_started": False,
        "tmux_orphan_cleanup_attempted": False,
        "tmux_orphan_server_running": False,
        "tmux_orphan_sessions_found": 0,
        "tmux_orphan_sessions_cleaned": 0,
        "tmux_orphan_sessions_failed": 0,
        "tmux_orphan_failed_sessions": [],
        "tmux_orphan_cleanup_error": "",
        "tmux_spawn_attempts": 0,
        "tmux_spawn_retried": False,
        "tmux_spawn_retry_reason": "",
        "tmux_spawn_error": "",
        "tmux_stale_session_cleanup_attempted": False,
        "tmux_stale_session_name": "",
        "tmux_stale_session_cleanup_result": "",
        "tmux_stale_session_cleanup_error": "",
        "tmux_stale_session_cleanup_retry_attempted": False,
        "tmux_stale_session_cleanup_retry_result": "",
        "tmux_stale_session_cleanup_retry_error": "",
        "tmux_stale_session_exists_after_cleanup": False,
        "tmux_stale_session_cleanup_verification_error": "",
        "tmux_cleanup_retry_attempted": False,
        "tmux_cleanup_retry_result": "",
        "tmux_cleanup_retry_error": "",
        "tmux_session_exists_after_cleanup": False,
        "tmux_cleanup_verification_error": "",
        "tmux_status_observed": False,
        "tmux_timed_out": False,
        "tmux_cleanup_result": "",
        "tmux_cleanup_error": "",
        "tmux_ipc_cleanup_result": "",
        "tmux_ipc_cleanup_error": "",
        "tmux_ipc_files_removed": 0,
        "tmux_session_workspace_root": str(session_environment.get("workspace_root", "")),
        "tmux_session_workspace_workdir": str(session_environment.get("workspace_workdir", "")),
        "tmux_session_workspace_home_dir": str(session_environment.get("workspace_home_dir", "")),
        "tmux_session_workspace_target_dir": str(session_environment.get("workspace_target_dir", "")),
        "tmux_session_workspace_tmp_dir": str(session_environment.get("workspace_tmp_dir", "")),
        "tmux_session_workspace_source_dir": str(session_environment.get("workspace_target_source_dir", "")),
        "tmux_session_workspace_target_status": str(session_environment.get("workspace_target_status", "")),
        "tmux_session_workspace_target_reused": bool(session_environment.get("workspace_target_reused", False)),
        "tmux_session_workspace_scope": str(session_environment.get("workspace_scope", "")),
        "tmux_session_workspace_isolated": bool(session_environment.get("workspace_isolation_active", False)),
        "tmux_session_env_keys": sorted({str(key) for key in worker_env.keys()}),
    }

    def _run_subprocess_fallback(reason: str) -> subprocess.CompletedProcess[str]:
        nonlocal fallback_reason
        fallback_reason = reason
        diagnostics["fallback_used"] = True
        diagnostics["fallback_reason"] = reason
        logger.log(
            "tmux_worker_fallback_attempt",
            worker=worker_name,
            reason=reason,
        )
        fallback_started = time.time()
        try:
            fallback_completed = execute_worker_subprocess_fn(
                command=command,
                timeout_sec=timeout_sec,
                worker_env=worker_env,
                workdir=pathlib.Path(str(session_environment.get("workspace_workdir", "") or output_dir)),
            )
        except subprocess.TimeoutExpired as exc:
            fallback_completed = _completed_process_from_timeout(
                command=command,
                timeout_exc=exc,
                transport="tmux->subprocess_fallback",
                phase="fallback_subprocess",
            )
            timeout_metadata = _extract_transport_timeout(fallback_completed)
            logger.log(
                "tmux_worker_transport_timeout",
                worker=worker_name,
                transport=str(timeout_metadata.get("timeout_transport", "tmux->subprocess_fallback")),
                phase=str(timeout_metadata.get("timeout_phase", "fallback_subprocess")),
                timeout_sec=timeout_sec,
                stdout_len=len(fallback_completed.stdout or ""),
                stderr_len=len(fallback_completed.stderr or ""),
            )
        logger.log(
            "tmux_worker_fallback_result",
            worker=worker_name,
            returncode=fallback_completed.returncode,
            duration_ms=int((time.time() - fallback_started) * 1000),
            stdout_len=len(fallback_completed.stdout or ""),
            stderr_len=len(fallback_completed.stderr or ""),
        )
        return fallback_completed

    def _run_primary_subprocess(transport_name: str, phase: str) -> subprocess.CompletedProcess[str]:
        try:
            return execute_worker_subprocess_fn(
                command=command,
                timeout_sec=timeout_sec,
                worker_env=worker_env,
                workdir=pathlib.Path(str(session_environment.get("workspace_workdir", "") or output_dir)),
            )
        except subprocess.TimeoutExpired as exc:
            completed_timeout = _completed_process_from_timeout(
                command=command,
                timeout_exc=exc,
                transport=transport_name,
                phase=phase,
            )
            timeout_metadata = _extract_transport_timeout(completed_timeout)
            logger.log(
                "tmux_worker_transport_timeout",
                worker=worker_name,
                transport=str(timeout_metadata.get("timeout_transport", transport_name)),
                phase=str(timeout_metadata.get("timeout_phase", phase)),
                timeout_sec=timeout_sec,
                stdout_len=len(completed_timeout.stdout or ""),
                stderr_len=len(completed_timeout.stderr or ""),
            )
            return completed_timeout

    try:
        if runtime_config.teammate_mode == "tmux":
            if which_fn("tmux"):
                transport = "tmux"
                completed = execute_worker_tmux_fn(
                    command=command,
                    workdir=output_dir,
                    session_prefix=preferred_tmux_session_name(worker_name),
                    timeout_sec=timeout_sec,
                    retain_session_for_reuse=retain_session_for_reuse,
                    allow_existing_session_reuse=allow_existing_session_reuse,
                    worker_env=worker_env,
                    session_workspace_root=str(session_environment.get("workspace_root", "")),
                    session_workspace_workdir=str(session_environment.get("workspace_workdir", "")),
                    session_workspace_home_dir=str(session_environment.get("workspace_home_dir", "")),
                    session_workspace_target_dir=str(session_environment.get("workspace_target_dir", "")),
                    session_workspace_tmp_dir=str(session_environment.get("workspace_tmp_dir", "")),
                )
            else:
                fallback_reason = "tmux binary not found"
                diagnostics["fallback_used"] = True
                diagnostics["fallback_reason"] = fallback_reason
                logger.log(
                    "tmux_unavailable_fallback_subprocess",
                    worker=worker_name,
                    reason=fallback_reason,
                )
                completed = _run_primary_subprocess(
                    transport_name="subprocess",
                    phase="tmux_unavailable_fallback",
                )
        else:
            completed = _run_primary_subprocess(
                transport_name="subprocess",
                phase="primary_subprocess",
            )

        lifecycle = _merge_tmux_lifecycle_into_diagnostics(diagnostics=diagnostics, completed=completed)
        timeout_metadata = _merge_transport_timeout_into_diagnostics(diagnostics=diagnostics, completed=completed)
        diagnostics["transport_used"] = transport
        diagnostics["returncode"] = completed.returncode
        diagnostics["duration_ms"] = int((time.time() - started_at) * 1000)
        diagnostics["stdout_preview"] = _output_preview(completed.stdout or "")
        diagnostics["stderr_preview"] = _output_preview(completed.stderr or "")
        logger.log(
            "tmux_worker_transport_result",
            worker=worker_name,
            transport=transport,
            returncode=completed.returncode,
            duration_ms=int((time.time() - started_at) * 1000),
            stdout_len=len(completed.stdout or ""),
            stderr_len=len(completed.stderr or ""),
            tmux_timed_out=bool(lifecycle.get("tmux_timed_out", False)),
            tmux_session_name_strategy=str(lifecycle.get("tmux_session_name_strategy", "")),
            tmux_preferred_session_found_preflight=bool(
                lifecycle.get("tmux_preferred_session_found_preflight", False)
            ),
            tmux_preferred_session_retried=bool(lifecycle.get("tmux_preferred_session_retried", False)),
            tmux_preferred_session_reused=bool(lifecycle.get("tmux_preferred_session_reused", False)),
            tmux_preferred_session_reuse_attempted=bool(
                lifecycle.get("tmux_preferred_session_reuse_attempted", False)
            ),
            tmux_preferred_session_reuse_result=str(
                lifecycle.get("tmux_preferred_session_reuse_result", "")
            ),
            tmux_preferred_session_reuse_authorized=bool(
                lifecycle.get("tmux_preferred_session_reuse_authorized", False)
            ),
            tmux_preferred_session_reused_existing=bool(
                lifecycle.get("tmux_preferred_session_reused_existing", False)
            ),
            tmux_reuse_retention_requested=bool(lifecycle.get("tmux_reuse_retention_requested", False)),
            tmux_session_retained_for_reuse=bool(lifecycle.get("tmux_session_retained_for_reuse", False)),
            tmux_cleanup_result=str(lifecycle.get("tmux_cleanup_result", "")),
            tmux_orphan_sessions_found=int(lifecycle.get("tmux_orphan_sessions_found", 0) or 0),
            tmux_orphan_sessions_cleaned=int(lifecycle.get("tmux_orphan_sessions_cleaned", 0) or 0),
            tmux_spawn_attempts=int(lifecycle.get("tmux_spawn_attempts", 0) or 0),
            tmux_spawn_retried=bool(lifecycle.get("tmux_spawn_retried", False)),
            tmux_stale_session_cleanup_attempted=bool(lifecycle.get("tmux_stale_session_cleanup_attempted", False)),
            tmux_stale_session_cleanup_result=str(lifecycle.get("tmux_stale_session_cleanup_result", "")),
            tmux_stale_session_cleanup_retry_attempted=bool(
                lifecycle.get("tmux_stale_session_cleanup_retry_attempted", False)
            ),
            tmux_stale_session_cleanup_retry_result=str(
                lifecycle.get("tmux_stale_session_cleanup_retry_result", "")
            ),
            tmux_cleanup_retry_attempted=bool(lifecycle.get("tmux_cleanup_retry_attempted", False)),
            tmux_cleanup_retry_result=str(lifecycle.get("tmux_cleanup_retry_result", "")),
            execution_timed_out=bool(timeout_metadata.get("execution_timed_out", False)),
            timeout_phase=str(timeout_metadata.get("timeout_phase", "")),
        )

        if completed.returncode != 0:
            if (
                transport == "tmux"
                and runtime_config.tmux_fallback_on_error
                and runtime_config.teammate_mode == "tmux"
            ):
                completed = _run_subprocess_fallback(reason=f"tmux_returncode={completed.returncode}")
                transport = "tmux->subprocess_fallback"
                diagnostics["transport_used"] = transport
                timeout_metadata = _merge_transport_timeout_into_diagnostics(
                    diagnostics=diagnostics,
                    completed=completed,
                )
                diagnostics["returncode"] = completed.returncode
                diagnostics["duration_ms"] = int((time.time() - started_at) * 1000)
                diagnostics["stdout_preview"] = _output_preview(completed.stdout or "")
                diagnostics["stderr_preview"] = _output_preview(completed.stderr or "")
            else:
                stderr = (completed.stderr or "").strip()
                stdout = (completed.stdout or "").strip()
                detail = (
                    str(timeout_metadata.get("timeout_message", "")).strip()
                    or stderr
                    or stdout
                    or f"worker exited with code {completed.returncode}"
                )
                if diagnostics.get("execution_timed_out"):
                    diagnostics["result"] = "timeout"
                    diagnostics["error"] = f"worker timed out via {transport}: {detail[:400]}"
                else:
                    diagnostics["result"] = "execution_failed"
                    diagnostics["error"] = f"worker execution failed via {transport}: {detail[:400]}"
                return {
                    "ok": False,
                    "error": diagnostics["error"],
                    "transport": transport,
                    "diagnostics": dict(diagnostics),
                }

        if completed.returncode != 0:
            stderr = (completed.stderr or "").strip()
            stdout = (completed.stdout or "").strip()
            detail = (
                str(timeout_metadata.get("timeout_message", "")).strip()
                or stderr
                or stdout
                or f"worker exited with code {completed.returncode}"
            )
            if diagnostics.get("execution_timed_out"):
                diagnostics["result"] = "timeout"
                diagnostics["error"] = f"worker timed out via {transport}: {detail[:400]}"
            else:
                diagnostics["result"] = "execution_failed"
                diagnostics["error"] = f"worker execution failed via {transport}: {detail[:400]}"
            return {
                "ok": False,
                "error": diagnostics["error"],
                "transport": transport,
                "diagnostics": dict(diagnostics),
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
                diagnostics["transport_used"] = transport
                timeout_metadata = _merge_transport_timeout_into_diagnostics(
                    diagnostics=diagnostics,
                    completed=completed,
                )
                diagnostics["returncode"] = completed.returncode
                diagnostics["duration_ms"] = int((time.time() - started_at) * 1000)
                diagnostics["stdout_preview"] = _output_preview(completed.stdout or "")
                diagnostics["stderr_preview"] = _output_preview(completed.stderr or "")
                if completed.returncode != 0:
                    stderr = (completed.stderr or "").strip()
                    stdout = (completed.stdout or "").strip()
                    detail = (
                        str(timeout_metadata.get("timeout_message", "")).strip()
                        or stderr
                        or stdout
                        or f"worker exited with code {completed.returncode}"
                    )
                    if diagnostics.get("execution_timed_out"):
                        diagnostics["result"] = "timeout"
                        diagnostics["error"] = f"worker timed out via {transport}: {detail[:400]}"
                    else:
                        diagnostics["result"] = "execution_failed"
                        diagnostics["error"] = f"worker execution failed via {transport}: {detail[:400]}"
                    return {
                        "ok": False,
                        "error": diagnostics["error"],
                        "transport": transport,
                        "diagnostics": dict(diagnostics),
                    }
                try:
                    parsed = json.loads((completed.stdout or "").strip())
                except json.JSONDecodeError as exc2:
                    diagnostics["result"] = "invalid_json"
                    diagnostics["error"] = f"worker returned invalid JSON via {transport}: {exc2}"
                    return {
                        "ok": False,
                        "error": diagnostics["error"],
                        "transport": transport,
                        "diagnostics": dict(diagnostics),
                    }
            else:
                diagnostics["result"] = "invalid_json"
                diagnostics["error"] = f"worker returned invalid JSON via {transport}: {exc}"
                return {
                    "ok": False,
                    "error": diagnostics["error"],
                    "transport": transport,
                    "diagnostics": dict(diagnostics),
                }
        if not isinstance(parsed, dict):
            diagnostics["result"] = "non_object_payload"
            diagnostics["error"] = f"worker returned non-object payload via {transport}"
            return {
                "ok": False,
                "error": diagnostics["error"],
                "transport": transport,
                "diagnostics": dict(diagnostics),
            }
        diagnostics["result"] = "success"
        return {
            "ok": True,
            "payload": parsed,
            "transport": transport,
            "diagnostics": dict(diagnostics),
        }
    finally:
        diagnostics["transport_used"] = diagnostics.get("transport_used") or transport
        diagnostics["duration_ms"] = int((time.time() - started_at) * 1000)
        if diagnostics.get("returncode", "") == "" and "completed" in locals():
            diagnostics["returncode"] = completed.returncode
            diagnostics["stdout_preview"] = _output_preview(completed.stdout or "")
            diagnostics["stderr_preview"] = _output_preview(completed.stderr or "")
        _append_tmux_worker_diagnostics(output_dir=output_dir, record=diagnostics)
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
            "task_context": build_task_context_snapshot(
                context=lead_context,
                task=task,
                profile=profile,
            ),
        }
        if lead_context.session_registry is not None:
            lead_context.session_state = lead_context.session_registry.bind_task(
                agent_name=profile.name,
                task=task,
                transport="tmux",
                task_context=payload["task_context"],
            )
            payload["session_state"] = dict(lead_context.session_state)
        payload["shared_state"] = payload["task_context"].get("visible_shared_state", {})
        board_snapshot = lead_context.board.snapshot()
        retain_session_for_reuse = any(
            str(item.get("task_id", "")) != task.task_id
            and str(item.get("task_type", "")) in supported_task_types
            and str(item.get("status", "")) in {"pending", "blocked"}
            for item in board_snapshot.get("tasks", [])
        )
        allow_existing_session_reuse = _lease_allows_preferred_session_reuse(
            shared_state=lead_context.shared_state,
            worker_name=profile.name,
        )
        lead_context.logger.log(
            "tmux_worker_task_dispatched",
            worker=profile.name,
            task_id=task.task_id,
            task_type=task.task_type,
            retain_session_for_reuse=retain_session_for_reuse,
            allow_existing_session_reuse=allow_existing_session_reuse,
        )
        task_context = payload.get("task_context", {})
        lead_context.logger.log(
            "task_context_prepared",
            agent=profile.name,
            task_id=task.task_id,
            task_type=task.task_type,
            scope=str(task_context.get("scope", "")),
            visible_shared_state_keys=list(task_context.get("visible_shared_state_keys", [])),
            visible_shared_state_key_count=int(task_context.get("visible_shared_state_key_count", 0)),
            omitted_shared_state_key_count=int(task_context.get("omitted_shared_state_key_count", 0)),
            dependency_task_ids=list(task_context.get("dependencies", [])),
            transport="tmux",
        )
        execution = run_worker_task_fn(
            runtime_script=runtime_script,
            output_dir=lead_context.output_dir,
            runtime_config=lead_context.runtime_config,
            payload=payload,
            worker_name=profile.name,
            logger=lead_context.logger,
            timeout_sec=worker_timeout_sec,
            retain_session_for_reuse=retain_session_for_reuse,
            allow_existing_session_reuse=allow_existing_session_reuse,
        )
        if not execution.get("ok"):
            error = str(execution.get("error", "unknown worker error"))
            execution_diagnostics = execution.get("diagnostics", {})
            transport = str(execution.get("transport", ""))
            lease_status = "failed"
            if "subprocess" in transport:
                lease_status = "fallback_subprocess"
            if lead_context.runtime_config.teammate_mode == "tmux":
                lease_entry = _update_tmux_session_lease(
                    lead_context=lead_context,
                    worker_name=profile.name,
                    session_name=str(
                        execution_diagnostics.get("tmux_session_name", "")
                        or execution_diagnostics.get("tmux_preferred_session_name", "")
                        or preferred_tmux_session_name(profile.name)
                    ),
                    status=lease_status,
                    task_id=task.task_id,
                    task_type=task.task_type,
                    transport=transport,
                    cleanup_result=str(execution_diagnostics.get("tmux_cleanup_result", "")),
                    retained_for_reuse=bool(execution_diagnostics.get("tmux_session_retained_for_reuse", False)),
                    reused_existing=bool(
                        execution_diagnostics.get("tmux_preferred_session_reused_existing", False)
                    ),
                    reuse_authorized=allow_existing_session_reuse,
                    error=error,
                    workspace_root=str(execution_diagnostics.get("tmux_session_workspace_root", "")),
                    workspace_workdir=str(execution_diagnostics.get("tmux_session_workspace_workdir", "")),
                    workspace_home_dir=str(execution_diagnostics.get("tmux_session_workspace_home_dir", "")),
                    workspace_target_dir=str(execution_diagnostics.get("tmux_session_workspace_target_dir", "")),
                    workspace_tmp_dir=str(execution_diagnostics.get("tmux_session_workspace_tmp_dir", "")),
                    workspace_scope=str(execution_diagnostics.get("tmux_session_workspace_scope", "")),
                    workspace_isolation_active=bool(
                        execution_diagnostics.get("tmux_session_workspace_isolated", False)
                    ),
                )
                if lead_context.session_registry is not None:
                    _sync_session_boundary_from_lease(
                        lead_context=lead_context,
                        worker_name=profile.name,
                        lease_entry=lease_entry,
                        transport=transport or "tmux",
                    )
                    lead_context.session_state = lead_context.session_registry.record_task_result(
                        agent_name=profile.name,
                        task=task,
                        transport=transport or "tmux",
                        success=False,
                        status="error",
                    )
            elif lead_context.session_registry is not None:
                _record_worker_boundary_from_diagnostics(
                    lead_context=lead_context,
                    worker_name=profile.name,
                    transport=transport or "subprocess",
                    execution_diagnostics=execution_diagnostics,
                    reuse_authorized=allow_existing_session_reuse,
                )
                lead_context.session_state = lead_context.session_registry.record_task_result(
                    agent_name=profile.name,
                    task=task,
                    transport=transport or "subprocess",
                    success=False,
                    status="error",
                )
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
                fallback_used=bool(execution_diagnostics.get("fallback_used", False)),
                fallback_reason=str(execution_diagnostics.get("fallback_reason", "")),
                execution_timed_out=bool(execution_diagnostics.get("execution_timed_out", False)),
                timeout_phase=str(execution_diagnostics.get("timeout_phase", "")),
                tmux_timed_out=bool(execution_diagnostics.get("tmux_timed_out", False)),
                tmux_session_name_strategy=str(execution_diagnostics.get("tmux_session_name_strategy", "")),
                tmux_preferred_session_found_preflight=bool(
                    execution_diagnostics.get("tmux_preferred_session_found_preflight", False)
                ),
                tmux_preferred_session_retried=bool(
                    execution_diagnostics.get("tmux_preferred_session_retried", False)
                ),
                tmux_preferred_session_reused=bool(
                    execution_diagnostics.get("tmux_preferred_session_reused", False)
                ),
                tmux_preferred_session_reuse_attempted=bool(
                    execution_diagnostics.get("tmux_preferred_session_reuse_attempted", False)
                ),
                tmux_preferred_session_reuse_result=str(
                    execution_diagnostics.get("tmux_preferred_session_reuse_result", "")
                ),
                tmux_preferred_session_reuse_authorized=bool(
                    execution_diagnostics.get("tmux_preferred_session_reuse_authorized", False)
                ),
                tmux_preferred_session_reused_existing=bool(
                    execution_diagnostics.get("tmux_preferred_session_reused_existing", False)
                ),
                tmux_session_retained_for_reuse=bool(
                    execution_diagnostics.get("tmux_session_retained_for_reuse", False)
                ),
                tmux_orphan_sessions_found=int(execution_diagnostics.get("tmux_orphan_sessions_found", 0) or 0),
                tmux_orphan_sessions_cleaned=int(
                    execution_diagnostics.get("tmux_orphan_sessions_cleaned", 0) or 0
                ),
                tmux_spawn_attempts=int(execution_diagnostics.get("tmux_spawn_attempts", 0) or 0),
                tmux_spawn_retried=bool(execution_diagnostics.get("tmux_spawn_retried", False)),
                tmux_stale_session_cleanup_attempted=bool(
                    execution_diagnostics.get("tmux_stale_session_cleanup_attempted", False)
                ),
                tmux_stale_session_cleanup_result=str(
                    execution_diagnostics.get("tmux_stale_session_cleanup_result", "")
                ),
                tmux_stale_session_cleanup_retry_attempted=bool(
                    execution_diagnostics.get("tmux_stale_session_cleanup_retry_attempted", False)
                ),
                tmux_stale_session_cleanup_retry_result=str(
                    execution_diagnostics.get("tmux_stale_session_cleanup_retry_result", "")
                ),
                tmux_cleanup_retry_attempted=bool(execution_diagnostics.get("tmux_cleanup_retry_attempted", False)),
                tmux_cleanup_retry_result=str(execution_diagnostics.get("tmux_cleanup_retry_result", "")),
                tmux_cleanup_result=str(execution_diagnostics.get("tmux_cleanup_result", "")),
            )
            if lock_paths:
                lead_context.file_locks.release(profile.name, lock_paths)
            return True

        worker_payload = execution.get("payload", {})
        execution_diagnostics = execution.get("diagnostics", {})
        result = worker_payload.get("result", {})
        state_updates = worker_payload.get("state_updates", {})
        if isinstance(state_updates, dict):
            for key, value in state_updates.items():
                lead_context.shared_state.set(str(key), value)
        if not isinstance(result, dict):
            result = {"raw_result": result}
        transport = str(execution.get("transport", ""))
        retained_for_reuse = bool(execution_diagnostics.get("tmux_session_retained_for_reuse", False))
        if lead_context.runtime_config.teammate_mode == "tmux":
            lease_status = "retained" if retained_for_reuse else "released"
            if "subprocess" in transport:
                lease_status = "fallback_subprocess"
            session_status = "retained" if retained_for_reuse else "ready"
            if "subprocess" in transport:
                session_status = "fallback_subprocess"
            lease_entry = _update_tmux_session_lease(
                lead_context=lead_context,
                worker_name=profile.name,
                session_name=str(
                    execution_diagnostics.get("tmux_session_name", "")
                    or execution_diagnostics.get("tmux_preferred_session_name", "")
                    or preferred_tmux_session_name(profile.name)
                ),
                status=lease_status,
                task_id=task.task_id,
                task_type=task.task_type,
                transport=transport,
                cleanup_result=str(execution_diagnostics.get("tmux_cleanup_result", "")),
                retained_for_reuse=retained_for_reuse,
                reused_existing=bool(execution_diagnostics.get("tmux_preferred_session_reused_existing", False)),
                reuse_authorized=allow_existing_session_reuse,
                workspace_root=str(execution_diagnostics.get("tmux_session_workspace_root", "")),
                workspace_workdir=str(execution_diagnostics.get("tmux_session_workspace_workdir", "")),
                workspace_home_dir=str(execution_diagnostics.get("tmux_session_workspace_home_dir", "")),
                workspace_target_dir=str(execution_diagnostics.get("tmux_session_workspace_target_dir", "")),
                workspace_tmp_dir=str(execution_diagnostics.get("tmux_session_workspace_tmp_dir", "")),
                workspace_scope=str(execution_diagnostics.get("tmux_session_workspace_scope", "")),
                workspace_isolation_active=bool(
                    execution_diagnostics.get("tmux_session_workspace_isolated", False)
                ),
            )
            if lead_context.session_registry is not None:
                _sync_session_boundary_from_lease(
                    lead_context=lead_context,
                    worker_name=profile.name,
                    lease_entry=lease_entry,
                    transport=transport or "tmux",
                )
                lead_context.session_state = lead_context.session_registry.record_task_result(
                    agent_name=profile.name,
                    task=task,
                    transport=transport or "tmux",
                    success=True,
                    status=session_status,
                )
        elif lead_context.session_registry is not None:
            _record_worker_boundary_from_diagnostics(
                lead_context=lead_context,
                worker_name=profile.name,
                transport=transport or "subprocess",
                execution_diagnostics=execution_diagnostics,
                reuse_authorized=allow_existing_session_reuse,
            )
            lead_context.session_state = lead_context.session_registry.record_task_result(
                agent_name=profile.name,
                task=task,
                transport=transport or "subprocess",
                success=True,
                status="ready",
            )
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
            fallback_used=bool(execution_diagnostics.get("fallback_used", False)),
            fallback_reason=str(execution_diagnostics.get("fallback_reason", "")),
            execution_timed_out=bool(execution_diagnostics.get("execution_timed_out", False)),
            timeout_phase=str(execution_diagnostics.get("timeout_phase", "")),
            tmux_timed_out=bool(execution_diagnostics.get("tmux_timed_out", False)),
            tmux_session_name_strategy=str(execution_diagnostics.get("tmux_session_name_strategy", "")),
            tmux_preferred_session_found_preflight=bool(
                execution_diagnostics.get("tmux_preferred_session_found_preflight", False)
            ),
            tmux_preferred_session_retried=bool(
                execution_diagnostics.get("tmux_preferred_session_retried", False)
            ),
            tmux_preferred_session_reused=bool(
                execution_diagnostics.get("tmux_preferred_session_reused", False)
            ),
            tmux_preferred_session_reuse_attempted=bool(
                execution_diagnostics.get("tmux_preferred_session_reuse_attempted", False)
            ),
            tmux_preferred_session_reuse_result=str(
                execution_diagnostics.get("tmux_preferred_session_reuse_result", "")
            ),
            tmux_preferred_session_reuse_authorized=bool(
                execution_diagnostics.get("tmux_preferred_session_reuse_authorized", False)
            ),
            tmux_preferred_session_reused_existing=bool(
                execution_diagnostics.get("tmux_preferred_session_reused_existing", False)
            ),
            tmux_session_retained_for_reuse=bool(
                execution_diagnostics.get("tmux_session_retained_for_reuse", False)
            ),
            tmux_orphan_sessions_found=int(execution_diagnostics.get("tmux_orphan_sessions_found", 0) or 0),
            tmux_orphan_sessions_cleaned=int(execution_diagnostics.get("tmux_orphan_sessions_cleaned", 0) or 0),
            tmux_spawn_attempts=int(execution_diagnostics.get("tmux_spawn_attempts", 0) or 0),
            tmux_spawn_retried=bool(execution_diagnostics.get("tmux_spawn_retried", False)),
            tmux_stale_session_cleanup_attempted=bool(
                execution_diagnostics.get("tmux_stale_session_cleanup_attempted", False)
            ),
            tmux_stale_session_cleanup_result=str(
                execution_diagnostics.get("tmux_stale_session_cleanup_result", "")
            ),
            tmux_stale_session_cleanup_retry_attempted=bool(
                execution_diagnostics.get("tmux_stale_session_cleanup_retry_attempted", False)
            ),
            tmux_stale_session_cleanup_retry_result=str(
                execution_diagnostics.get("tmux_stale_session_cleanup_retry_result", "")
            ),
            tmux_cleanup_retry_attempted=bool(execution_diagnostics.get("tmux_cleanup_retry_attempted", False)),
            tmux_cleanup_retry_result=str(execution_diagnostics.get("tmux_cleanup_retry_result", "")),
            tmux_cleanup_result=str(execution_diagnostics.get("tmux_cleanup_result", "")),
        )
        if lock_paths:
            lead_context.file_locks.release(profile.name, lock_paths)
        return True
    return False


def recover_tmux_analyst_sessions(
    lead_context: Any,
    analyst_profiles: Sequence[AgentProfile],
    resume_from: Optional[pathlib.Path] = None,
) -> Dict[str, Any]:
    leases = _load_tmux_session_leases(lead_context.shared_state)
    if not leases:
        summary = {
            "workers": [],
            "recovered": [],
            "missing": [],
            "inactive": [],
            "failed": [],
            "skipped": "no_leases",
            "resume_from": str(resume_from) if resume_from else "",
        }
        lead_context.logger.log(
            "tmux_worker_session_recovery_sweep",
            workers=[],
            recovered=[],
            missing=[],
            inactive=[],
            failed=[],
            skipped="no_leases",
            resume_from=str(resume_from) if resume_from else "",
        )
        lead_context.shared_state.set("tmux_session_recovery_summary", summary)
        return summary
    worker_names = [profile.name for profile in analyst_profiles]
    recovered: List[str] = []
    missing: List[str] = []
    inactive: List[str] = []
    failed: List[str] = []
    if shutil.which("tmux") is None:
        for worker_name in worker_names:
            lease = leases.get(worker_name, {})
            if not isinstance(lease, dict) or not lease:
                continue
            lease_entry = _update_tmux_session_lease(
                lead_context=lead_context,
                worker_name=worker_name,
                session_name=str(lease.get("session_name", "") or preferred_tmux_session_name(worker_name)),
                status="recovery_tmux_unavailable",
                transport="tmux_resume_recovery",
                reuse_authorized=False,
                recovery_result="tmux_unavailable",
            )
            _sync_session_boundary_from_lease(
                lead_context=lead_context,
                worker_name=worker_name,
                lease_entry=lease_entry,
                transport="tmux_resume_recovery",
            )
            failed.append(worker_name)
        summary = {
            "workers": worker_names,
            "recovered": recovered,
            "missing": missing,
            "inactive": inactive,
            "failed": failed,
            "skipped": "tmux_unavailable",
            "resume_from": str(resume_from) if resume_from else "",
        }
        lead_context.logger.log(
            "tmux_worker_session_recovery_sweep",
            workers=worker_names,
            recovered=recovered,
            missing=missing,
            inactive=inactive,
            failed=failed,
            skipped="tmux_unavailable",
            resume_from=str(resume_from) if resume_from else "",
        )
        lead_context.shared_state.set("tmux_session_recovery_summary", summary)
        return summary

    for worker_name in worker_names:
        lease = leases.get(worker_name, {})
        if not isinstance(lease, dict) or not lease:
            continue
        session_name = str(lease.get("session_name", "") or preferred_tmux_session_name(worker_name))
        lease_status = str(lease.get("status", ""))
        if lease_status != "retained":
            lease_entry = _update_tmux_session_lease(
                lead_context=lead_context,
                worker_name=worker_name,
                session_name=session_name,
                status="recovery_inactive",
                transport="tmux_resume_recovery",
                reuse_authorized=False,
                recovery_result="inactive",
            )
            _sync_session_boundary_from_lease(
                lead_context=lead_context,
                worker_name=worker_name,
                lease_entry=lease_entry,
                transport="tmux_resume_recovery",
            )
            inactive.append(worker_name)
            continue
        existence = _tmux_session_exists(session_name=session_name)
        if existence.get("exists"):
            lease_entry = _update_tmux_session_lease(
                lead_context=lead_context,
                worker_name=worker_name,
                session_name=session_name,
                status="recovered_available",
                transport="tmux_resume_recovery",
                retained_for_reuse=True,
                reuse_authorized=True,
                recovery_result="available",
            )
            _sync_session_boundary_from_lease(
                lead_context=lead_context,
                worker_name=worker_name,
                lease_entry=lease_entry,
                transport="tmux",
            )
            recovered.append(worker_name)
        else:
            error = str(existence.get("error", ""))
            status = "recovered_missing" if not error else "recovery_failed"
            lease_entry = _update_tmux_session_lease(
                lead_context=lead_context,
                worker_name=worker_name,
                session_name=session_name,
                status=status,
                transport="tmux_resume_recovery",
                retained_for_reuse=False,
                reuse_authorized=False,
                error=error,
                recovery_result="missing" if status == "recovered_missing" else "failed",
            )
            _sync_session_boundary_from_lease(
                lead_context=lead_context,
                worker_name=worker_name,
                lease_entry=lease_entry,
                transport="tmux_resume_recovery",
            )
            if status == "recovered_missing":
                missing.append(worker_name)
            else:
                failed.append(worker_name)

    summary = {
        "workers": worker_names,
        "recovered": recovered,
        "missing": missing,
        "inactive": inactive,
        "failed": failed,
        "skipped": "",
        "resume_from": str(resume_from) if resume_from else "",
    }
    lead_context.logger.log(
        "tmux_worker_session_recovery_sweep",
        workers=worker_names,
        recovered=recovered,
        missing=missing,
        inactive=inactive,
        failed=failed,
        skipped="",
        resume_from=str(resume_from) if resume_from else "",
    )
    lead_context.shared_state.set("tmux_session_recovery_summary", summary)
    return summary


def cleanup_tmux_analyst_sessions(lead_context: Any, analyst_profiles: Sequence[AgentProfile]) -> Dict[str, Any]:
    session_names = [preferred_tmux_session_name(profile.name) for profile in analyst_profiles]
    defer_cleanup = bool(lead_context.shared_state.get("tmux_cleanup_deferred_for_resume", False))
    deferred_reason = str(lead_context.shared_state.get("tmux_cleanup_deferred_reason", "") or "")
    if defer_cleanup:
        summary = {
            "sessions": session_names,
            "cleaned": 0,
            "already_exited": 0,
            "failed": [],
            "skipped": "deferred_for_resume",
            "deferred_reason": deferred_reason,
        }
        lead_context.logger.log(
            "tmux_worker_session_cleanup_sweep",
            sessions=session_names,
            cleaned=0,
            already_exited=0,
            failed=[],
            skipped="deferred_for_resume",
            deferred_reason=deferred_reason,
        )
        lead_context.shared_state.set("tmux_session_cleanup_summary", summary)
        return summary
    if shutil.which("tmux") is None:
        summary = {
            "sessions": session_names,
            "cleaned": 0,
            "already_exited": 0,
            "failed": [],
            "skipped": "tmux_unavailable",
            "deferred_reason": "",
        }
        lead_context.logger.log(
            "tmux_worker_session_cleanup_sweep",
            sessions=session_names,
            cleaned=0,
            already_exited=0,
            failed=[],
            skipped="tmux_unavailable",
            deferred_reason="",
        )
        for profile in analyst_profiles:
            _update_tmux_session_lease(
                lead_context=lead_context,
                worker_name=profile.name,
                session_name=preferred_tmux_session_name(profile.name),
                status="cleanup_skipped_tmux_unavailable",
                transport="tmux_cleanup_sweep",
                cleanup_result="tmux_unavailable",
            )
        lead_context.shared_state.set("tmux_session_cleanup_summary", summary)
        return summary
    cleaned = 0
    already_exited = 0
    failed: List[str] = []
    cleanup_results: Dict[str, str] = {}
    for session_name in session_names:
        cleanup = _cleanup_tmux_session_with_recovery(session_name)
        result = str(cleanup.get("result", ""))
        cleanup_results[session_name] = result
        if result in {"killed", "recovered_after_retry"}:
            cleaned += 1
        elif result == "already_exited":
            already_exited += 1
        else:
            failed.append(session_name)
    for profile in analyst_profiles:
        session_name = preferred_tmux_session_name(profile.name)
        cleanup_status = "cleanup_failed" if session_name in failed else "cleanup_swept"
        _update_tmux_session_lease(
            lead_context=lead_context,
            worker_name=profile.name,
            session_name=session_name,
            status=cleanup_status,
            transport="tmux_cleanup_sweep",
            cleanup_result=str(cleanup_results.get(session_name, "")),
        )
    summary = {
        "sessions": session_names,
        "cleaned": cleaned,
        "already_exited": already_exited,
        "failed": failed,
        "skipped": "",
        "deferred_reason": "",
    }
    lead_context.logger.log(
        "tmux_worker_session_cleanup_sweep",
        sessions=session_names,
        cleaned=cleaned,
        already_exited=already_exited,
        failed=failed,
        skipped="",
        deferred_reason="",
    )
    lead_context.shared_state.set("tmux_session_cleanup_summary", summary)
    return summary
