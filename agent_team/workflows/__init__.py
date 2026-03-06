from __future__ import annotations

import dataclasses
import pathlib
from typing import Any, Callable, Dict, List

from ..config import RuntimeConfig
from ..core import Task
from .markdown_audit import build_markdown_audit_tasks


@dataclasses.dataclass(frozen=True)
class WorkflowPack:
    name: str
    description: str
    build_tasks: Callable[[pathlib.Path, RuntimeConfig, Dict[str, Any]], List[Task]]


WORKFLOW_PACKS: Dict[str, WorkflowPack] = {
    "markdown-audit": WorkflowPack(
        name="markdown-audit",
        description="Audit Markdown repositories with analysis, challenge, adjudication, and reporting.",
        build_tasks=build_markdown_audit_tasks,
    ),
}


def resolve_workflow_pack(name: str) -> WorkflowPack:
    normalized = str(name or "markdown-audit").strip().lower()
    try:
        return WORKFLOW_PACKS[normalized]
    except KeyError as exc:
        raise ValueError(
            f"unsupported workflow pack: {name}. available={sorted(WORKFLOW_PACKS.keys())}"
        ) from exc


def build_workflow_tasks(
    workflow_pack: str,
    output_dir: pathlib.Path,
    runtime_config: RuntimeConfig,
    workflow_options: Dict[str, Any] | None = None,
) -> List[Task]:
    pack = resolve_workflow_pack(workflow_pack)
    return pack.build_tasks(
        output_dir=output_dir,
        runtime_config=runtime_config,
        workflow_options=dict(workflow_options or {}),
    )
