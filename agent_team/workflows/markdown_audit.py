from __future__ import annotations

import pathlib
from typing import Any, Dict, List

from ..config import RuntimeConfig
from ..core import Task


def build_markdown_audit_tasks(
    output_dir: pathlib.Path,
    runtime_config: RuntimeConfig,
    workflow_options: Dict[str, Any] | None = None,
) -> List[Task]:
    del workflow_options
    return [
        Task(
            task_id="discover_markdown",
            title="Discover markdown files",
            task_type="discover_markdown",
            required_skills={"inventory"},
            dependencies=[],
            payload={},
            locked_paths=[],
            allowed_agent_types={"analyst"},
        ),
        Task(
            task_id="heading_audit",
            title="Audit heading coverage",
            task_type="heading_audit",
            required_skills={"analysis"},
            dependencies=["discover_markdown"],
            payload={},
            locked_paths=[],
            allowed_agent_types={"analyst"},
        ),
        Task(
            task_id="length_audit",
            title="Audit long markdown files",
            task_type="length_audit",
            required_skills={"analysis"},
            dependencies=["discover_markdown"],
            payload={"line_threshold": 180},
            locked_paths=[],
            allowed_agent_types={"analyst"},
        ),
        Task(
            task_id="dynamic_planning",
            title="Plan runtime follow-up tasks",
            task_type="dynamic_planning",
            required_skills={"review"},
            dependencies=["heading_audit", "length_audit"],
            payload={},
            locked_paths=[],
            allowed_agent_types={"reviewer"},
        ),
        Task(
            task_id="peer_challenge",
            title="Run teammate challenge round",
            task_type="peer_challenge",
            required_skills={"review"},
            dependencies=["dynamic_planning"],
            payload={
                "wait_seconds": runtime_config.peer_wait_seconds,
                "auto_round3_on_challenge": runtime_config.auto_round3_on_challenge,
            },
            locked_paths=[],
            allowed_agent_types={"reviewer"},
        ),
        Task(
            task_id="lead_adjudication",
            title="Lead adjudicates debate quality",
            task_type="lead_adjudication",
            required_skills={"lead"},
            dependencies=["peer_challenge"],
            payload={},
            locked_paths=[],
            allowed_agent_types={"lead"},
        ),
        Task(
            task_id="evidence_pack",
            title="Collect supplemental evidence when challenged",
            task_type="evidence_pack",
            required_skills={"review"},
            dependencies=["lead_adjudication"],
            payload={"wait_seconds": runtime_config.evidence_wait_seconds},
            locked_paths=[],
            allowed_agent_types={"reviewer"},
        ),
        Task(
            task_id="lead_re_adjudication",
            title="Lead re-adjudicates after evidence pack",
            task_type="lead_re_adjudication",
            required_skills={"lead"},
            dependencies=["evidence_pack"],
            payload={},
            locked_paths=[],
            allowed_agent_types={"lead"},
        ),
        Task(
            task_id="llm_synthesis",
            title="Synthesize findings with provider",
            task_type="llm_synthesis",
            required_skills={"review", "llm"},
            dependencies=["heading_audit", "length_audit", "peer_challenge", "lead_re_adjudication"],
            payload={},
            locked_paths=[],
            allowed_agent_types={"reviewer"},
        ),
        Task(
            task_id="recommendation_pack",
            title="Write recommendation report",
            task_type="recommendation_pack",
            required_skills={"review", "writer"},
            dependencies=["llm_synthesis"],
            payload={},
            locked_paths=[str((output_dir / "final_report.md").resolve())],
            allowed_agent_types={"reviewer"},
        ),
    ]
