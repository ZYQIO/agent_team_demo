from __future__ import annotations

import dataclasses
import json
import pathlib
from typing import Any, Dict, List, Optional

from .core import AgentProfile


@dataclasses.dataclass
class RuntimeConfig:
    teammate_mode: str = "in-process"
    enable_dynamic_tasks: bool = True
    teammate_provider_replies: bool = False
    teammate_memory_turns: int = 4
    tmux_worker_timeout_sec: int = 120
    tmux_fallback_on_error: bool = True
    peer_wait_seconds: float = 4.0
    evidence_wait_seconds: float = 4.0
    auto_round3_on_challenge: bool = True
    adjudication_accept_threshold: int = 75
    adjudication_challenge_threshold: int = 50
    adjudication_weight_completeness: float = 0.45
    adjudication_weight_rebuttal_coverage: float = 0.35
    adjudication_weight_argument_depth: float = 0.20
    re_adjudication_max_bonus: int = 15
    re_adjudication_weight_coverage: float = 0.60
    re_adjudication_weight_depth: float = 0.40

    def to_dict(self) -> Dict[str, Any]:
        return dataclasses.asdict(self)


@dataclasses.dataclass
class HostCapabilities:
    independent_sessions: bool = False
    plan_approval: bool = False
    auto_context_files: bool = False
    mcp_bridge: bool = False
    skill_bridge: bool = False
    workspace_isolation: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return dataclasses.asdict(self)


@dataclasses.dataclass
class HostConfig:
    kind: str = "generic-cli"
    session_transport: str = "thread"
    capabilities: HostCapabilities = dataclasses.field(default_factory=HostCapabilities)
    note: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "kind": self.kind,
            "session_transport": self.session_transport,
            "capabilities": self.capabilities.to_dict(),
            "note": self.note,
        }


@dataclasses.dataclass
class ModelConfig:
    provider_name: str = "heuristic"
    model: str = "heuristic-v1"
    openai_api_key_env: str = "OPENAI_API_KEY"
    openai_base_url: str = "https://api.openai.com/v1"
    require_llm: bool = False
    timeout_sec: int = 60

    def to_dict(self) -> Dict[str, Any]:
        return dataclasses.asdict(self)


@dataclasses.dataclass
class TeamAgentConfig:
    name: str
    skills: List[str]
    agent_type: str = "general"

    def to_agent_profile(self) -> AgentProfile:
        return AgentProfile(
            name=self.name,
            skills={str(skill) for skill in self.skills},
            agent_type=self.agent_type,
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "skills": list(self.skills),
            "agent_type": self.agent_type,
        }


@dataclasses.dataclass
class TeamConfig:
    lead_name: str = "lead"
    agents: List[TeamAgentConfig] = dataclasses.field(default_factory=list)
    mailbox_model: str = "asynchronous pull-based inbox"

    def to_profiles(self) -> List[AgentProfile]:
        return [agent.to_agent_profile() for agent in self.agents]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "lead_name": self.lead_name,
            "mailbox_model": self.mailbox_model,
            "agents": [agent.to_dict() for agent in self.agents],
        }


@dataclasses.dataclass
class WorkflowConfig:
    pack: str = "markdown-audit"
    preset: str = "default"
    options: Dict[str, Any] = dataclasses.field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return dataclasses.asdict(self)


@dataclasses.dataclass
class PolicyConfig:
    teammate_plan_required: bool = False
    failure_mode: str = "fail-fast"
    isolation_strategy: str = "file-lock"
    allow_host_managed_context: bool = True

    def to_dict(self) -> Dict[str, Any]:
        return dataclasses.asdict(self)


@dataclasses.dataclass
class AgentTeamConfig:
    runtime: RuntimeConfig = dataclasses.field(default_factory=RuntimeConfig)
    host: HostConfig = dataclasses.field(default_factory=HostConfig)
    model: ModelConfig = dataclasses.field(default_factory=ModelConfig)
    team: TeamConfig = dataclasses.field(default_factory=TeamConfig)
    workflow: WorkflowConfig = dataclasses.field(default_factory=WorkflowConfig)
    policies: PolicyConfig = dataclasses.field(default_factory=PolicyConfig)
    source_path: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "runtime": self.runtime.to_dict(),
            "host": self.host.to_dict(),
            "model": self.model.to_dict(),
            "team": self.team.to_dict(),
            "workflow": self.workflow.to_dict(),
            "policies": self.policies.to_dict(),
            "source_path": self.source_path,
        }


def default_team_config() -> TeamConfig:
    return TeamConfig(
        lead_name="lead",
        mailbox_model="asynchronous pull-based inbox",
        agents=[
            TeamAgentConfig(
                name="analyst_alpha",
                skills=["inventory", "analysis"],
                agent_type="analyst",
            ),
            TeamAgentConfig(
                name="analyst_beta",
                skills=["analysis"],
                agent_type="analyst",
            ),
            TeamAgentConfig(
                name="reviewer_gamma",
                skills=["review", "writer", "llm"],
                agent_type="reviewer",
            ),
        ],
    )


def host_capabilities_for_kind(kind: str) -> HostCapabilities:
    normalized = str(kind or "generic-cli").strip().lower()
    if normalized == "claude-code":
        return HostCapabilities(
            independent_sessions=True,
            plan_approval=True,
            auto_context_files=True,
            mcp_bridge=True,
            skill_bridge=True,
            workspace_isolation=True,
        )
    if normalized == "codex":
        return HostCapabilities(
            independent_sessions=False,
            plan_approval=False,
            auto_context_files=True,
            mcp_bridge=True,
            skill_bridge=True,
            workspace_isolation=False,
        )
    return HostCapabilities()


def default_host_config(kind: str = "generic-cli") -> HostConfig:
    transport = "thread"
    normalized = str(kind or "generic-cli").strip().lower()
    if normalized == "claude-code":
        transport = "session"
    elif normalized == "codex":
        transport = "tooling-session"
    return HostConfig(
        kind=normalized,
        session_transport=transport,
        capabilities=host_capabilities_for_kind(normalized),
    )


def build_agent_team_config(
    runtime_config: RuntimeConfig,
    provider_name: str,
    model: str,
    openai_api_key_env: str,
    openai_base_url: str,
    require_llm: bool,
    provider_timeout_sec: int,
    workflow_pack: str = "markdown-audit",
    workflow_preset: str = "default",
    workflow_options: Optional[Dict[str, Any]] = None,
    host_kind: str = "generic-cli",
    team_config: Optional[TeamConfig] = None,
    source_path: str = "",
) -> AgentTeamConfig:
    effective_team = team_config or default_team_config()
    return AgentTeamConfig(
        runtime=runtime_config,
        host=default_host_config(host_kind),
        model=ModelConfig(
            provider_name=provider_name,
            model=model,
            openai_api_key_env=openai_api_key_env,
            openai_base_url=openai_base_url,
            require_llm=require_llm,
            timeout_sec=provider_timeout_sec,
        ),
        team=effective_team,
        workflow=WorkflowConfig(
            pack=workflow_pack,
            preset=workflow_preset,
            options=dict(workflow_options or {}),
        ),
        policies=PolicyConfig(),
        source_path=source_path,
    )


def _runtime_from_dict(payload: Dict[str, Any]) -> RuntimeConfig:
    return RuntimeConfig(**{key: value for key, value in payload.items() if key in RuntimeConfig.__annotations__})


def _host_from_dict(payload: Dict[str, Any]) -> HostConfig:
    capabilities_payload = payload.get("capabilities", {})
    if not isinstance(capabilities_payload, dict):
        capabilities_payload = {}
    kind = str(payload.get("kind", "generic-cli"))
    capabilities = host_capabilities_for_kind(kind)
    capabilities = dataclasses.replace(
        capabilities,
        **{
            key: value
            for key, value in capabilities_payload.items()
            if key in HostCapabilities.__annotations__
        },
    )
    session_transport = str(payload.get("session_transport", default_host_config(kind).session_transport))
    note = str(payload.get("note", ""))
    return HostConfig(
        kind=kind,
        session_transport=session_transport,
        capabilities=capabilities,
        note=note,
    )


def _model_from_dict(payload: Dict[str, Any]) -> ModelConfig:
    return ModelConfig(
        provider_name=str(payload.get("provider_name", payload.get("provider", "heuristic"))),
        model=str(payload.get("model", "heuristic-v1")),
        openai_api_key_env=str(payload.get("openai_api_key_env", "OPENAI_API_KEY")),
        openai_base_url=str(payload.get("openai_base_url", "https://api.openai.com/v1")),
        require_llm=bool(payload.get("require_llm", False)),
        timeout_sec=int(payload.get("timeout_sec", payload.get("provider_timeout_sec", 60))),
    )


def _team_from_dict(payload: Dict[str, Any]) -> TeamConfig:
    lead_name = str(payload.get("lead_name", "lead"))
    mailbox_model = str(payload.get("mailbox_model", "asynchronous pull-based inbox"))
    raw_agents = payload.get("agents", [])
    agents: List[TeamAgentConfig] = []
    if isinstance(raw_agents, list):
        for item in raw_agents:
            if not isinstance(item, dict):
                continue
            skills = item.get("skills", [])
            if not isinstance(skills, list):
                skills = []
            agents.append(
                TeamAgentConfig(
                    name=str(item.get("name", "")),
                    skills=[str(skill) for skill in skills],
                    agent_type=str(item.get("agent_type", "general")),
                )
            )
    if not agents:
        return default_team_config()
    return TeamConfig(lead_name=lead_name, agents=agents, mailbox_model=mailbox_model)


def _workflow_from_dict(payload: Dict[str, Any]) -> WorkflowConfig:
    options = payload.get("options", {})
    if not isinstance(options, dict):
        options = {}
    return WorkflowConfig(
        pack=str(payload.get("pack", "markdown-audit")),
        preset=str(payload.get("preset", "default")),
        options=dict(options),
    )


def _policies_from_dict(payload: Dict[str, Any]) -> PolicyConfig:
    return PolicyConfig(
        teammate_plan_required=bool(payload.get("teammate_plan_required", False)),
        failure_mode=str(payload.get("failure_mode", "fail-fast")),
        isolation_strategy=str(payload.get("isolation_strategy", "file-lock")),
        allow_host_managed_context=bool(payload.get("allow_host_managed_context", True)),
    )


def load_agent_team_config(config_path: pathlib.Path) -> AgentTeamConfig:
    payload = json.loads(config_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("config file must contain a top-level object")

    runtime_payload = payload.get("runtime", {})
    host_payload = payload.get("host", {})
    model_payload = payload.get("model", {})
    team_payload = payload.get("team", {})
    workflow_payload = payload.get("workflow", {})
    policies_payload = payload.get("policies", {})
    if not isinstance(runtime_payload, dict):
        runtime_payload = {}
    if not isinstance(host_payload, dict):
        host_payload = {}
    if not isinstance(model_payload, dict):
        model_payload = {}
    if not isinstance(team_payload, dict):
        team_payload = {}
    if not isinstance(workflow_payload, dict):
        workflow_payload = {}
    if not isinstance(policies_payload, dict):
        policies_payload = {}

    return AgentTeamConfig(
        runtime=_runtime_from_dict(runtime_payload),
        host=_host_from_dict(host_payload),
        model=_model_from_dict(model_payload),
        team=_team_from_dict(team_payload),
        workflow=_workflow_from_dict(workflow_payload),
        policies=_policies_from_dict(policies_payload),
        source_path=str(config_path.resolve()),
    )
