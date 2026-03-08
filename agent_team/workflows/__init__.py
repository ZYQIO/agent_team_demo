from __future__ import annotations

import dataclasses
import pathlib
from typing import Any, Callable, Dict, List, Sequence

from ..config import RuntimeConfig
from ..core import Task
from .markdown_audit import build_markdown_audit_tasks
from .markdown_audit_handlers import build_markdown_audit_handlers
from .repo_audit import build_repo_audit_tasks
from .repo_audit_handlers import build_repo_audit_handlers


HandlerMap = Dict[str, Callable[[Any, Task], Dict[str, Any]]]
LeadTaskOrder = Sequence[str]


@dataclasses.dataclass(frozen=True)
class WorkflowRuntimeMetadata:
    lead_task_order: tuple[str, ...] = ()
    report_task_ids: tuple[str, ...] = ()


@dataclasses.dataclass(frozen=True)
class WorkflowPack:
    name: str
    description: str
    build_tasks: Callable[[pathlib.Path, RuntimeConfig, Dict[str, Any]], List[Task]]
    build_handlers: Callable[[], HandlerMap]
    runtime_metadata: WorkflowRuntimeMetadata


WORKFLOW_PACKS: Dict[str, WorkflowPack] = {
    "markdown-audit": WorkflowPack(
        name="markdown-audit",
        description="Audit Markdown repositories with analysis, challenge, adjudication, and reporting.",
        build_tasks=build_markdown_audit_tasks,
        build_handlers=build_markdown_audit_handlers,
        runtime_metadata=WorkflowRuntimeMetadata(
            lead_task_order=("lead_adjudication", "lead_re_adjudication"),
            report_task_ids=("recommendation_pack",),
        ),
    ),
    "repo-audit": WorkflowPack(
        name="repo-audit",
        description="Audit repository structure and oversized files with the shared lead/challenge runtime.",
        build_tasks=build_repo_audit_tasks,
        build_handlers=build_repo_audit_handlers,
        runtime_metadata=WorkflowRuntimeMetadata(
            lead_task_order=("lead_adjudication", "lead_re_adjudication"),
            report_task_ids=("repo_recommendation_pack",),
        ),
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


def build_workflow_handlers(workflow_pack: str) -> HandlerMap:
    pack = resolve_workflow_pack(workflow_pack)
    return pack.build_handlers()


def build_workflow_lead_task_order(workflow_pack: str) -> List[str]:
    pack = resolve_workflow_pack(workflow_pack)
    return [str(task_id) for task_id in pack.runtime_metadata.lead_task_order]


def build_workflow_runtime_metadata(workflow_pack: str) -> WorkflowRuntimeMetadata:
    pack = resolve_workflow_pack(workflow_pack)
    return pack.runtime_metadata
