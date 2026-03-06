from __future__ import annotations

import dataclasses
from typing import Any, Dict, List

from .config import HostConfig


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


def build_host_adapter(config: HostConfig) -> HostAdapter:
    return HostAdapter(config=config)
