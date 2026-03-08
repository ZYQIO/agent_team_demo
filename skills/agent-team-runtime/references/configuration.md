# Configuration Guide

Use a JSON config file when the agent-team runtime must be embedded into different AI hosts or reused as a skill across tools.

## Top-Level Sections

- `runtime`
  - Runtime execution, timing, adjudication, replay, and teammate transport settings.
- `host`
  - Host adapter metadata such as `generic-cli`, `codex`, or `claude-code`.
- `model`
  - Provider/model/base URL/API key env/timeout.
- `team`
  - Lead metadata plus teammate roster and skills.
- `workflow`
  - Workflow pack name and preset label.
- `policies`
  - Runtime policy knobs such as failure mode and isolation strategy.

## Minimal Example

```json
{
  "host": {
    "kind": "codex"
  },
  "model": {
    "provider_name": "heuristic",
    "model": "heuristic-v1"
  },
  "team": {
    "lead_name": "lead",
    "agents": [
      {
        "name": "analyst_alpha",
        "agent_type": "analyst",
        "skills": ["inventory", "analysis"]
      },
      {
        "name": "reviewer_gamma",
        "agent_type": "reviewer",
        "skills": ["review", "writer", "llm"]
      }
    ]
  },
  "workflow": {
    "pack": "markdown-audit",
    "preset": "default"
  }
}
```

## Override Strategy

- Use the config file as the default source of truth for host/model/team/workflow.
- Use CLI flags to override only the values needed for a specific run.
- Keep workflow-specific logic in the workflow pack, not in host or model config.

## Current Built-In Options

- Host kinds:
  - `generic-cli`
  - `codex`
  - `claude-code`
- Workflow packs:
  - `markdown-audit`
  - `repo-audit`
