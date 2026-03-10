from __future__ import annotations

import dataclasses
from typing import Any, Dict, List, Mapping

from .config import HostConfig, RuntimeConfig
from .core import SharedState, utc_now


HOST_RUNTIME_ENFORCEMENT_KEY = "host_runtime_enforcement"


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
        notes.append("transport_isolation_partial_to_analyst_workers")
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
    }


@dataclasses.dataclass
class HostAdapter:
    config: HostConfig

    def runtime_metadata(self) -> Dict[str, Any]:
        capabilities = self.config.capabilities.to_dict()
        limits: List[str] = []
        if not capabilities.get("independent_sessions", False):
            limits.append("session_isolation_emulated")
        if not capabilities.get("plan_approval", False):
            limits.append("plan_approval_runtime_managed")
        if not capabilities.get("workspace_isolation", False):
            limits.append("worktree_isolation_unavailable")
        return {
            "kind": self.config.kind,
            "session_transport": self.config.session_transport,
            "capabilities": capabilities,
            "limits": limits,
            "note": self.config.note,
        }

    def runtime_enforcement(
        self,
        runtime_config: RuntimeConfig,
        policies: Any = None,
    ) -> Dict[str, Any]:
        metadata = self.runtime_metadata()
        capabilities = _normalize_capabilities(metadata.get("capabilities", {}))
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
            notes.append("transport_isolation_partial_to_analyst_workers")
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
            "notes": notes,
        }


def build_host_enforcement_snapshot(shared_state: SharedState) -> Dict[str, Any]:
    state_snapshot = shared_state.snapshot()
    host_metadata = state_snapshot.get("host", {})
    if not isinstance(host_metadata, Mapping):
        host_metadata = {}
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
        }
    return {
        "generated_at": utc_now(),
        "host": {
            "kind": str(host_metadata.get("kind", "") or ""),
            "session_transport": str(host_metadata.get("session_transport", "") or ""),
            "capabilities": _normalize_capabilities(host_metadata.get("capabilities", {})),
            "limits": _normalize_string_list(host_metadata.get("limits", [])),
            "note": str(host_metadata.get("note", "") or ""),
        },
        **enforcement,
    }


def build_host_adapter(config: HostConfig) -> HostAdapter:
    return HostAdapter(config=config)
