from __future__ import annotations

import dataclasses
import os
import pathlib
import shutil
from typing import Any, Dict, List

from .config import HostConfig


def _reset_path(path: pathlib.Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink(missing_ok=True)
        return
    if path.is_dir():
        shutil.rmtree(path)


def _write_context_file(path: pathlib.Path, lines: List[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


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

    def prepare_agent_session(
        self,
        *,
        output_dir: pathlib.Path,
        target_dir: pathlib.Path,
        agent_name: str,
        agent_type: str,
        goal: str,
        workflow_pack: str,
        workflow_preset: str,
    ) -> Dict[str, Any]:
        capabilities = self.config.capabilities.to_dict()
        session_dir = output_dir / "_host_sessions" / str(agent_name)
        session_dir.mkdir(parents=True, exist_ok=True)

        effective_target_dir = target_dir.resolve()
        workspace_dir = session_dir
        target_link_path = session_dir / "target"
        workspace_isolated = bool(capabilities.get("workspace_isolation", False))
        if workspace_isolated:
            workspace_dir = session_dir / "workspace"
            workspace_dir.mkdir(parents=True, exist_ok=True)
            if target_link_path.exists() or target_link_path.is_symlink():
                _reset_path(target_link_path)
            try:
                os.symlink(str(target_dir.resolve()), str(target_link_path), target_is_directory=True)
                effective_target_dir = target_link_path.absolute()
            except OSError:
                effective_target_dir = target_dir.resolve()

        context_file_path = session_dir / "AGENT_TEAM_CONTEXT.md"
        if bool(capabilities.get("auto_context_files", False)):
            lines = [
                "# Agent Team Context",
                "",
                f"- Agent: {agent_name}",
                f"- Agent type: {agent_type}",
                f"- Host kind: {self.config.kind}",
                f"- Session transport: {self.config.session_transport}",
                f"- Workflow pack: {workflow_pack}",
                f"- Workflow preset: {workflow_preset}",
                f"- Workspace isolated: {workspace_isolated}",
                f"- Source target: {target_dir.resolve()}",
                f"- Effective target: {effective_target_dir}",
                "",
                "## Goal",
                "",
                goal,
                "",
                "## Limits",
                "",
            ]
            limits = self.runtime_metadata().get("limits", [])
            if limits:
                lines.extend([f"- {item}" for item in limits])
            else:
                lines.append("- none")
            _write_context_file(context_file_path, lines)

        return {
            "agent": agent_name,
            "agent_type": agent_type,
            "host_kind": self.config.kind,
            "session_transport": self.config.session_transport,
            "workspace_dir": str(workspace_dir.resolve()),
            "source_target_dir": str(target_dir.resolve()),
            "effective_target_dir": str(effective_target_dir),
            "workspace_isolated": workspace_isolated,
            "auto_context_enabled": bool(capabilities.get("auto_context_files", False)),
            "context_file": str(context_file_path.resolve()) if context_file_path.exists() else "",
            "limits": list(self.runtime_metadata().get("limits", [])),
        }


def build_host_adapter(config: HostConfig) -> HostAdapter:
    return HostAdapter(config=config)
