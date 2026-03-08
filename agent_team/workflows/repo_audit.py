from __future__ import annotations

import pathlib
from typing import Any, Dict, List

from ..config import RuntimeConfig
from ..core import Task


def _workflow_int_option(
    workflow_options: Dict[str, Any] | None,
    key: str,
    default: int,
) -> int:
    value = (workflow_options or {}).get(key, default)
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def build_repo_audit_tasks(
    output_dir: pathlib.Path,
    runtime_config: RuntimeConfig,
    workflow_options: Dict[str, Any] | None = None,
) -> List[Task]:
    line_threshold = _workflow_int_option(workflow_options, "line_threshold", 320)
    byte_threshold = _workflow_int_option(workflow_options, "byte_threshold", 20000)
    return [
        Task(
            task_id="discover_repository",
            title="Discover repository files",
            task_type="discover_repository",
            required_skills={"inventory"},
            dependencies=[],
            payload={},
            locked_paths=[],
            allowed_agent_types={"analyst"},
        ),
        Task(
            task_id="extension_audit",
            title="Audit repository extension mix",
            task_type="extension_audit",
            required_skills={"analysis"},
            dependencies=["discover_repository"],
            payload={},
            locked_paths=[],
            allowed_agent_types={"analyst"},
        ),
        Task(
            task_id="large_file_audit",
            title="Audit oversized repository files",
            task_type="large_file_audit",
            required_skills={"analysis"},
            dependencies=["discover_repository"],
            payload={"line_threshold": line_threshold, "byte_threshold": byte_threshold},
            locked_paths=[],
            allowed_agent_types={"analyst"},
        ),
        Task(
            task_id="repo_dynamic_planning",
            title="Plan repository follow-up tasks",
            task_type="repo_dynamic_planning",
            required_skills={"review"},
            dependencies=["extension_audit", "large_file_audit"],
            payload={},
            locked_paths=[],
            allowed_agent_types={"reviewer"},
        ),
        Task(
            task_id="peer_challenge",
            title="Run repository teammate challenge round",
            task_type="peer_challenge",
            required_skills={"review"},
            dependencies=["repo_dynamic_planning"],
            payload={
                "wait_seconds": runtime_config.peer_wait_seconds,
                "auto_round3_on_challenge": runtime_config.auto_round3_on_challenge,
                "round1_question": (
                    "Identify one weak assumption in the current repository audit and propose one concrete fix."
                ),
                "round2_question": (
                    "Critique the other analyst's repository-risk proposal and suggest one improvement."
                ),
                "round3_question": (
                    "Provide a revised repository readiness plan with measurable checks and rollout order."
                ),
            },
            locked_paths=[],
            allowed_agent_types={"reviewer"},
        ),
        Task(
            task_id="lead_adjudication",
            title="Lead adjudicates repository debate quality",
            task_type="lead_adjudication",
            required_skills={"lead"},
            dependencies=["peer_challenge"],
            payload={},
            locked_paths=[],
            allowed_agent_types={"lead"},
        ),
        Task(
            task_id="evidence_pack",
            title="Collect repository evidence when challenged",
            task_type="evidence_pack",
            required_skills={"review"},
            dependencies=["lead_adjudication"],
            payload={"wait_seconds": runtime_config.evidence_wait_seconds},
            locked_paths=[],
            allowed_agent_types={"reviewer"},
        ),
        Task(
            task_id="lead_re_adjudication",
            title="Lead re-adjudicates repository findings",
            task_type="lead_re_adjudication",
            required_skills={"lead"},
            dependencies=["evidence_pack"],
            payload={},
            locked_paths=[],
            allowed_agent_types={"lead"},
        ),
        Task(
            task_id="llm_synthesis",
            title="Synthesize repository findings with provider",
            task_type="llm_synthesis",
            required_skills={"review", "llm"},
            dependencies=["extension_audit", "large_file_audit", "peer_challenge", "lead_re_adjudication"],
            payload={},
            locked_paths=[],
            allowed_agent_types={"reviewer"},
        ),
        Task(
            task_id="repo_recommendation_pack",
            title="Write repository readiness report",
            task_type="repo_recommendation_pack",
            required_skills={"review", "writer"},
            dependencies=["llm_synthesis"],
            payload={},
            locked_paths=[str((output_dir / "final_report.md").resolve())],
            allowed_agent_types={"reviewer"},
        ),
    ]
