from __future__ import annotations

import dataclasses
import json
import os
import pathlib
import shutil
from typing import Any, Dict, List, Mapping

from .config import HostConfig, RuntimeConfig
from .core import SharedState, utc_now


HOST_RUNTIME_ENFORCEMENT_KEY = "host_runtime_enforcement"
CLAUDE_CODE_CANONICAL_RELAY = "gaccode.com"


def _policy_flag(policies: Any, name: str, default: bool) -> bool:
    if policies is None:
        return default
    if isinstance(policies, Mapping):
        return bool(policies.get(name, default))
    return bool(getattr(policies, name, default))


def _normalize_string_list(value: Any) -> List[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if str(item)]


def _normalize_capabilities(value: Any) -> Dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    return {str(key): value[key] for key in value}


def _dedupe_string_list(items: List[str]) -> List[str]:
    seen: set[str] = set()
    result: List[str] = []
    for item in items:
        normalized = str(item or "")
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        result.append(normalized)
    return result


def _load_json_file(path: pathlib.Path) -> Dict[str, Any]:
    try:
        raw_text = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    try:
        payload = json.loads(raw_text)
    except json.JSONDecodeError:
        return {}
    if not isinstance(payload, Mapping):
        return {}
    return {str(key): payload[key] for key in payload}


def _probe_codex_environment() -> Dict[str, Any]:
    cli_path = shutil.which("codex") or ""
    ready = bool(cli_path)
    reason = "cli_available" if ready else "cli_missing"
    return {
        "kind": "codex",
        "cli_installed": ready,
        "cli_path": cli_path,
        "native_session_prerequisites_ready": ready,
        "native_session_prerequisite_reason": reason,
        "notes": ["cli_installed" if ready else "cli_missing"],
    }


def _probe_claude_code_environment() -> Dict[str, Any]:
    cli_path = shutil.which("claude") or ""
    home_dir = pathlib.Path.home()
    config_dir = pathlib.Path(
        os.environ.get("CLAUDE_CONFIG_DIR", "") or (home_dir / ".claudecode")
    )
    relay_file = config_dir / "relay"
    auth_config_file = config_dir / "config"
    user_state_file = home_dir / ".claude.json"

    relay_host = str(os.environ.get("CLAUDE_CODE_HOST", "") or CLAUDE_CODE_CANONICAL_RELAY)
    relay_source = "env" if os.environ.get("CLAUDE_CODE_HOST", "") else "canonical_default"
    relay_payload = _load_json_file(relay_file)
    relay_host_from_file = str(relay_payload.get("host", "") or "").strip()
    if relay_host_from_file:
        relay_host = relay_host_from_file
        relay_source = "relay_file"

    api_key_present = bool(str(os.environ.get("ANTHROPIC_API_KEY", "") or "").strip())
    user_state = _load_json_file(user_state_file)
    subscription_available_raw = user_state.get("hasAvailableSubscription", None)
    subscription_available = (
        subscription_available_raw if isinstance(subscription_available_raw, bool) else None
    )

    notes: List[str] = []
    if cli_path:
        notes.append("cli_installed")
    else:
        notes.append("cli_missing")
    if relay_source == "relay_file":
        notes.append("relay_configured")
    elif relay_source == "env":
        notes.append("relay_overridden_by_env")
    else:
        notes.append("canonical_relay_defaulted")
    if api_key_present:
        notes.append("auth_api_key_env")
    elif subscription_available is True:
        notes.append("subscription_available")
    elif subscription_available is False:
        notes.append("subscription_unavailable")
    else:
        notes.append("subscription_unknown")

    if not cli_path:
        ready = False
        reason = "cli_missing"
        auth_source = "missing"
    elif api_key_present:
        ready = True
        reason = "api_key_env"
        auth_source = "api_key_env"
    elif subscription_available is True:
        ready = True
        reason = "subscription_available"
        auth_source = "claude_json"
    elif subscription_available is False:
        ready = False
        reason = "subscription_unavailable"
        auth_source = "claude_json"
    else:
        ready = False
        reason = "auth_unknown"
        auth_source = "unknown"

    return {
        "kind": "claude-code",
        "cli_installed": bool(cli_path),
        "cli_path": cli_path,
        "config_dir": str(config_dir),
        "auth_config_present": auth_config_file.exists(),
        "relay_file_present": relay_file.exists(),
        "relay_host": relay_host,
        "relay_source": relay_source,
        "subscription_available": subscription_available,
        "auth_source": auth_source,
        "native_session_prerequisites_ready": ready,
        "native_session_prerequisite_reason": reason,
        "notes": notes,
    }


def probe_host_environment(kind: str) -> Dict[str, Any]:
    normalized = str(kind or "generic-cli").strip().lower()
    if normalized == "claude-code":
        return _probe_claude_code_environment()
    if normalized == "codex":
        return _probe_codex_environment()
    return {}


def _default_runtime_enforcement(
    host_metadata: Mapping[str, Any],
    runtime_config: Mapping[str, Any],
    policies: Mapping[str, Any],
) -> Dict[str, Any]:
    capabilities = _normalize_capabilities(host_metadata.get("capabilities", {}))
    host_supports_native_sessions = bool(capabilities.get("independent_sessions", False))
    host_supports_workspace_isolation = bool(capabilities.get("workspace_isolation", False))
    host_supports_managed_context = bool(capabilities.get("auto_context_files", False))
    requested_teammate_mode = str(runtime_config.get("teammate_mode", "") or "in-process")
    host_managed_context_requested = bool(policies.get("allow_host_managed_context", True))
    notes: List[str] = ["host_runtime_enforcement_missing"]

    if requested_teammate_mode == "tmux":
        session_enforcement = "transport_managed"
        workspace_enforcement = "transport_managed"
        host_native_session_active = False
        host_native_workspace_active = False
        effective_boundary_source = "transport"
        effective_boundary_strength = "medium"
        notes.append("tmux_transport_manages_session_boundaries")
    elif requested_teammate_mode == "subprocess":
        session_enforcement = "transport_managed"
        workspace_enforcement = "transport_managed"
        host_native_session_active = False
        host_native_workspace_active = False
        effective_boundary_source = "transport"
        effective_boundary_strength = "medium"
        notes.append("subprocess_transport_manages_session_boundaries")
        notes.append("transport_isolation_partial_to_selected_worker_tasks")
    else:
        session_enforcement = "runtime_managed"
        workspace_enforcement = "runtime_managed"
        host_native_session_active = False
        host_native_workspace_active = False
        effective_boundary_source = "runtime"
        effective_boundary_strength = "emulated"
        if requested_teammate_mode == "in-process":
            notes.append("runtime_threads_share_process_state")

    if host_supports_native_sessions and not host_native_session_active:
        notes.append("host_independent_sessions_advertised_only")
    if host_supports_workspace_isolation and not host_native_workspace_active:
        notes.append("host_workspace_isolation_advertised_only")
    if host_managed_context_requested and host_supports_managed_context:
        notes.append("host_managed_context_not_bound_to_runtime")

    return {
        "host_kind": str(host_metadata.get("kind", "") or ""),
        "configured_session_transport": str(host_metadata.get("session_transport", "") or ""),
        "requested_teammate_mode": requested_teammate_mode,
        "session_enforcement": session_enforcement,
        "workspace_enforcement": workspace_enforcement,
        "host_native_session_active": host_native_session_active,
        "host_native_workspace_active": host_native_workspace_active,
        "host_managed_context_requested": host_managed_context_requested,
        "host_managed_context_active": False,
        "effective_boundary_source": effective_boundary_source,
        "effective_boundary_strength": effective_boundary_strength,
        "capabilities": capabilities,
        "limits": _normalize_string_list(host_metadata.get("limits", [])),
        "note": str(host_metadata.get("note", "") or ""),
        "notes": notes,
        "host_session_backend": "",
        "host_session_backend_source": "",
        "host_session_backend_host_managed": False,
        "host_session_backend_session_isolation_active": False,
        "host_session_backend_workspace_isolation_active": False,
    }


@dataclasses.dataclass
class HostAdapter:
    config: HostConfig

    def runtime_metadata(self) -> Dict[str, Any]:
        capabilities = self.config.capabilities.to_dict()
        environment = probe_host_environment(self.config.kind)
        limits: List[str] = []
        if not capabilities.get("independent_sessions", False):
            limits.append("session_isolation_emulated")
        if not capabilities.get("plan_approval", False):
            limits.append("plan_approval_runtime_managed")
        if not capabilities.get("workspace_isolation", False):
            limits.append("worktree_isolation_unavailable")
        if (
            isinstance(environment, Mapping)
            and str(environment.get("kind", "") or "") == "claude-code"
            and not bool(environment.get("native_session_prerequisites_ready", False))
        ):
            reason = str(environment.get("native_session_prerequisite_reason", "") or "unknown")
            limits.append(f"claude_code_prerequisites_{reason}")
        return {
            "kind": self.config.kind,
            "session_transport": self.config.session_transport,
            "capabilities": capabilities,
            "limits": limits,
            "note": self.config.note,
            "environment": dict(environment) if isinstance(environment, Mapping) else {},
        }

    def runtime_enforcement(
        self,
        runtime_config: RuntimeConfig,
        policies: Any = None,
    ) -> Dict[str, Any]:
        metadata = self.runtime_metadata()
        capabilities = _normalize_capabilities(metadata.get("capabilities", {}))
        environment = metadata.get("environment", {})
        requested_teammate_mode = str(getattr(runtime_config, "teammate_mode", "") or "in-process")
        configured_session_transport = str(self.config.session_transport or "")
        host_supports_native_sessions = bool(capabilities.get("independent_sessions", False))
        host_supports_workspace_isolation = bool(capabilities.get("workspace_isolation", False))
        host_supports_managed_context = bool(capabilities.get("auto_context_files", False))
        host_managed_context_requested = _policy_flag(
            policies,
            "allow_host_managed_context",
            True,
        )
        host_native_mode_requested = requested_teammate_mode in {
            "host",
            "host-native",
            "host-session",
            "native-session",
        }

        notes: List[str] = []
        if host_native_mode_requested and host_supports_native_sessions:
            session_enforcement = "host_managed"
            host_native_session_active = True
        elif requested_teammate_mode == "tmux":
            session_enforcement = "transport_managed"
            host_native_session_active = False
            notes.append("tmux_transport_manages_session_boundaries")
        elif requested_teammate_mode == "subprocess":
            session_enforcement = "transport_managed"
            host_native_session_active = False
            notes.append("subprocess_transport_manages_session_boundaries")
            notes.append("transport_isolation_partial_to_selected_worker_tasks")
        else:
            session_enforcement = "runtime_managed"
            host_native_session_active = False
            if requested_teammate_mode == "in-process":
                notes.append("runtime_threads_share_process_state")

        if host_native_session_active and host_supports_workspace_isolation:
            workspace_enforcement = "host_managed"
            host_native_workspace_active = True
        elif requested_teammate_mode in {"tmux", "subprocess"}:
            workspace_enforcement = "transport_managed"
            host_native_workspace_active = False
        else:
            workspace_enforcement = "runtime_managed"
            host_native_workspace_active = False

        if host_native_session_active:
            effective_boundary_source = "host"
            effective_boundary_strength = "strong"
            notes.append("host_transport_manages_session_boundaries")
        elif session_enforcement == "transport_managed":
            effective_boundary_source = "transport"
            effective_boundary_strength = "medium"
        else:
            effective_boundary_source = "runtime"
            effective_boundary_strength = "emulated"

        if host_supports_native_sessions and not host_native_session_active:
            notes.append("host_independent_sessions_advertised_only")
        if host_supports_workspace_isolation and not host_native_workspace_active:
            notes.append("host_workspace_isolation_advertised_only")
        if host_native_mode_requested and not host_supports_native_sessions:
            notes.append("requested_host_sessions_not_supported")

        host_managed_context_active = bool(
            host_managed_context_requested
            and host_supports_managed_context
            and host_native_session_active
        )
        if host_managed_context_requested and host_supports_managed_context and not host_managed_context_active:
            notes.append("host_managed_context_not_bound_to_runtime")
        if isinstance(environment, Mapping) and str(environment.get("kind", "") or "") == "claude-code":
            relay_source = str(environment.get("relay_source", "") or "")
            if relay_source == "relay_file":
                notes.append("claude_code_relay_configured")
            elif relay_source == "env":
                notes.append("claude_code_relay_overridden_by_env")
            elif relay_source:
                notes.append("claude_code_canonical_relay_defaulted")
            if bool(environment.get("native_session_prerequisites_ready", False)):
                notes.append("claude_code_prerequisites_ready")
            else:
                reason = str(
                    environment.get("native_session_prerequisite_reason", "") or "unknown"
                )
                notes.append(f"claude_code_prerequisites_{reason}")

        return {
            "host_kind": str(self.config.kind or ""),
            "configured_session_transport": configured_session_transport,
            "requested_teammate_mode": requested_teammate_mode,
            "session_enforcement": session_enforcement,
            "workspace_enforcement": workspace_enforcement,
            "host_native_session_active": host_native_session_active,
            "host_native_workspace_active": host_native_workspace_active,
            "host_managed_context_requested": host_managed_context_requested,
            "host_managed_context_active": host_managed_context_active,
            "effective_boundary_source": effective_boundary_source,
            "effective_boundary_strength": effective_boundary_strength,
            "capabilities": capabilities,
            "limits": _normalize_string_list(metadata.get("limits", [])),
            "note": str(self.config.note or ""),
            "notes": _dedupe_string_list(notes),
            "host_session_backend": "",
            "host_session_backend_source": "",
            "host_session_backend_host_managed": False,
            "host_session_backend_session_isolation_active": False,
            "host_session_backend_workspace_isolation_active": False,
        }


def apply_host_session_backend_enforcement(
    enforcement: Mapping[str, Any],
    backend: Mapping[str, Any] | None = None,
) -> Dict[str, Any]:
    raw_backend = dict(backend or {})
    normalized = {
        "host_kind": str(enforcement.get("host_kind", "") or ""),
        "configured_session_transport": str(enforcement.get("configured_session_transport", "") or ""),
        "requested_teammate_mode": str(enforcement.get("requested_teammate_mode", "") or "in-process"),
        "session_enforcement": str(enforcement.get("session_enforcement", "") or "runtime_managed"),
        "workspace_enforcement": str(enforcement.get("workspace_enforcement", "") or "runtime_managed"),
        "host_native_session_active": bool(enforcement.get("host_native_session_active", False)),
        "host_native_workspace_active": bool(enforcement.get("host_native_workspace_active", False)),
        "host_managed_context_requested": bool(
            enforcement.get("host_managed_context_requested", True)
        ),
        "host_managed_context_active": bool(enforcement.get("host_managed_context_active", False)),
        "effective_boundary_source": str(enforcement.get("effective_boundary_source", "") or "runtime"),
        "effective_boundary_strength": str(
            enforcement.get("effective_boundary_strength", "") or "emulated"
        ),
        "capabilities": _normalize_capabilities(enforcement.get("capabilities", {})),
        "limits": _normalize_string_list(enforcement.get("limits", [])),
        "note": str(enforcement.get("note", "") or ""),
        "notes": _normalize_string_list(enforcement.get("notes", [])),
        "host_session_backend": str(
            raw_backend.get("backend", "") or enforcement.get("host_session_backend", "") or ""
        ),
        "host_session_backend_source": str(
            raw_backend.get("source", "")
            or enforcement.get("host_session_backend_source", "")
            or ""
        ),
        "host_session_backend_host_managed": bool(
            raw_backend.get(
                "host_managed",
                enforcement.get("host_session_backend_host_managed", False),
            )
        ),
        "host_session_backend_session_isolation_active": bool(
            raw_backend.get(
                "session_isolation_active",
                enforcement.get("host_session_backend_session_isolation_active", False),
            )
        ),
        "host_session_backend_workspace_isolation_active": bool(
            raw_backend.get(
                "workspace_isolation_active",
                enforcement.get("host_session_backend_workspace_isolation_active", False),
            )
        ),
    }
    backend_name = normalized["host_session_backend"]
    if not backend_name or backend_name == "host_native":
        return normalized

    capabilities = normalized["capabilities"]
    host_supports_native_sessions = bool(capabilities.get("independent_sessions", False))
    host_supports_workspace_isolation = bool(capabilities.get("workspace_isolation", False))
    host_supports_managed_context = bool(capabilities.get("auto_context_files", False))
    backend_host_managed = bool(normalized["host_session_backend_host_managed"])
    session_isolation_active = bool(normalized["host_session_backend_session_isolation_active"])
    workspace_isolation_active = bool(normalized["host_session_backend_workspace_isolation_active"])

    notes = [item for item in normalized["notes"] if item != "host_transport_manages_session_boundaries"]
    notes.append(f"host_session_backend_{backend_name}")
    if backend_host_managed:
        normalized["host_native_session_active"] = session_isolation_active
        normalized["host_native_workspace_active"] = workspace_isolation_active
        normalized["host_managed_context_active"] = bool(
            normalized["host_managed_context_requested"]
            and host_supports_managed_context
            and session_isolation_active
        )
        if normalized["requested_teammate_mode"] == "host" and session_isolation_active:
            normalized["session_enforcement"] = "host_managed"
        normalized["workspace_enforcement"] = (
            "host_managed" if workspace_isolation_active else "runtime_managed"
        )
        normalized["effective_boundary_source"] = str(
            normalized["host_session_backend_source"] or "host"
        )
        normalized["effective_boundary_strength"] = "strong" if session_isolation_active else "emulated"
        if session_isolation_active:
            notes.append("host_transport_manages_session_boundaries")
        if host_supports_workspace_isolation and not workspace_isolation_active:
            notes.append("host_workspace_isolation_advertised_only")
    else:
        normalized["host_native_session_active"] = False
        normalized["host_native_workspace_active"] = False
        normalized["host_managed_context_active"] = False
        if normalized["requested_teammate_mode"] == "host" and session_isolation_active:
            normalized["session_enforcement"] = "transport_managed"
        normalized["workspace_enforcement"] = (
            "transport_managed" if workspace_isolation_active else "runtime_managed"
        )
        normalized["effective_boundary_source"] = str(
            normalized["host_session_backend_source"] or "transport"
        )
        normalized["effective_boundary_strength"] = "medium" if session_isolation_active else "emulated"
        if normalized["requested_teammate_mode"] == "host" and backend_name == "external_process":
            notes.append("requested_host_sessions_backed_by_transport_process")
        if host_supports_native_sessions:
            notes.append("host_independent_sessions_advertised_only")
        if host_supports_workspace_isolation and not workspace_isolation_active:
            notes.append("host_workspace_isolation_advertised_only")
    if (
        normalized["host_managed_context_requested"]
        and host_supports_managed_context
        and not normalized["host_managed_context_active"]
    ):
        notes.append("host_managed_context_not_bound_to_runtime")
    normalized["notes"] = _dedupe_string_list(notes)
    return normalized


def build_host_enforcement_snapshot(shared_state: SharedState) -> Dict[str, Any]:
    state_snapshot = shared_state.snapshot()
    host_metadata = state_snapshot.get("host", {})
    if not isinstance(host_metadata, Mapping):
        host_metadata = {}
    host_environment = host_metadata.get("environment", {})
    if not isinstance(host_environment, Mapping) or not host_environment:
        host_environment = probe_host_environment(str(host_metadata.get("kind", "") or ""))
    runtime_config = state_snapshot.get("runtime_config", {})
    if not isinstance(runtime_config, Mapping):
        runtime_config = {}
    policies = state_snapshot.get("policies", {})
    if not isinstance(policies, Mapping):
        policies = {}
    enforcement = state_snapshot.get(HOST_RUNTIME_ENFORCEMENT_KEY, {})
    if not isinstance(enforcement, Mapping) or not enforcement:
        enforcement = _default_runtime_enforcement(
            host_metadata=host_metadata,
            runtime_config=runtime_config,
            policies=policies,
        )
    else:
        enforcement = {
            "host_kind": str(
                enforcement.get("host_kind", "")
                or host_metadata.get("kind", "")
                or ""
            ),
            "configured_session_transport": str(
                enforcement.get("configured_session_transport", "")
                or host_metadata.get("session_transport", "")
                or ""
            ),
            "requested_teammate_mode": str(
                enforcement.get("requested_teammate_mode", "")
                or runtime_config.get("teammate_mode", "")
                or "in-process"
            ),
            "session_enforcement": str(enforcement.get("session_enforcement", "") or "runtime_managed"),
            "workspace_enforcement": str(
                enforcement.get("workspace_enforcement", "") or "runtime_managed"
            ),
            "host_native_session_active": bool(
                enforcement.get("host_native_session_active", False)
            ),
            "host_native_workspace_active": bool(
                enforcement.get("host_native_workspace_active", False)
            ),
            "host_managed_context_requested": bool(
                enforcement.get(
                    "host_managed_context_requested",
                    policies.get("allow_host_managed_context", True),
                )
            ),
            "host_managed_context_active": bool(
                enforcement.get("host_managed_context_active", False)
            ),
            "effective_boundary_source": str(
                enforcement.get("effective_boundary_source", "") or "runtime"
            ),
            "effective_boundary_strength": str(
                enforcement.get("effective_boundary_strength", "") or "emulated"
            ),
            "capabilities": _normalize_capabilities(
                enforcement.get("capabilities", host_metadata.get("capabilities", {}))
            ),
            "limits": _normalize_string_list(
                enforcement.get("limits", host_metadata.get("limits", []))
            ),
            "note": str(enforcement.get("note", "") or host_metadata.get("note", "") or ""),
            "notes": _normalize_string_list(enforcement.get("notes", [])),
            "host_session_backend": str(
                enforcement.get("host_session_backend", "") or ""
            ),
            "host_session_backend_source": str(
                enforcement.get("host_session_backend_source", "") or ""
            ),
            "host_session_backend_host_managed": bool(
                enforcement.get("host_session_backend_host_managed", False)
            ),
            "host_session_backend_session_isolation_active": bool(
                enforcement.get("host_session_backend_session_isolation_active", False)
            ),
            "host_session_backend_workspace_isolation_active": bool(
                enforcement.get("host_session_backend_workspace_isolation_active", False)
            ),
        }
    if enforcement.get("host_session_backend", ""):
        enforcement = apply_host_session_backend_enforcement(enforcement)
    return {
        "generated_at": utc_now(),
        "host": {
            "kind": str(host_metadata.get("kind", "") or ""),
            "session_transport": str(host_metadata.get("session_transport", "") or ""),
            "capabilities": _normalize_capabilities(host_metadata.get("capabilities", {})),
            "limits": _normalize_string_list(host_metadata.get("limits", [])),
            "note": str(host_metadata.get("note", "") or ""),
            "environment": dict(host_environment) if isinstance(host_environment, Mapping) else {},
        },
        **enforcement,
    }


def build_host_adapter(config: HostConfig) -> HostAdapter:
    return HostAdapter(config=config)
