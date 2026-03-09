# Agent Team Demo

This folder now contains two layers:

1. `agent_team_demo.py`
- Minimal 3-role demo (`Planner / Executor / Reviewer`).

2. `agent_team_runtime.py`
- A richer local MVP inspired by Claude Code Agent Teams:
  - lead + teammates
  - shared task board with dependencies
  - mailbox messaging
  - teammate debate rounds (round1 challenge + round2 rebuttal + optional round3 refinement)
  - lead adjudication with configurable verdict thresholds and rubric weights
  - challenge-closed-loop: supplemental evidence pack + lead re-adjudication
  - task assignment constraints with `allowed_agent_types` (Task(agent_type)-style gating)
  - compatibility hook events: `TeammateIdle` and `TaskCompleted`
  - team progress artifacts and lead-facing progress summary
  - explicit teammate task-context boundaries with per-run context summary artifact
  - durable teammate session ledger with per-agent task/memory/activity snapshots and explicit resume continuity markers
  - explicit session-boundary posture artifact describing host-native, tmux-backed, or runtime-emulated session isolation
  - teammate execution mode toggle (`in-process` / `tmux`, with subprocess fallback when tmux binary is unavailable)
  - file lock registry
  - pluggable provider (`heuristic` / `openai`)
  - event logs + final report artifacts

## Config-Driven Architecture

The runtime now exposes a host-agnostic configuration layer so the same team engine can be packaged as a reusable skill across different AI tools.

- `agent_team/core.py`
  - Shared runtime primitives: task board, mailbox, locks, checkpoints, events.
- `agent_team/config.py`
  - Declarative runtime, host, model, team, workflow, and policy config types.
- `agent_team/host.py`
  - Host adapter metadata layer (`generic-cli`, `codex`, `claude-code`).
- `agent_team/models.py`
  - Model adapter layer with heuristic + OpenAI-compatible providers.
- `agent_team/workflows/`
  - Workflow-pack registry. Current built-in packs: `markdown-audit`, `repo-audit`.

The legacy entrypoint `agent_team_runtime.py` remains compatible, but now acts as the CLI wrapper around those reusable layers.

## Research and Plan

- `claude_agent_teams_research.md`: deep-dive notes from official docs/changelog
- `agent_team_implementation_plan.md`: implementation plan and milestones
- `PARITY.md`: capability parity snapshot vs Claude Code Agent Teams

## Run Minimal Demo

```bash
python3 agent_team_demo/agent_team_demo.py --target .
```

## Run Team Runtime (MVP)

```bash
python3 agent_team_demo/agent_team_runtime.py --target . --output agent_team_demo/output
```

Optional custom goal:

```bash
python3 agent_team_demo/agent_team_runtime.py --target . --goal "Audit markdown structure with agent team" --output agent_team_demo/output
```

Run with OpenAI-compatible provider:

```bash
export OPENAI_API_KEY="your_key"
python3 agent_team_demo/agent_team_runtime.py \
  --provider openai \
  --model gpt-4.1-mini \
  --target . \
  --output agent_team_demo/output
```

Run with externalized team/host/workflow config:

```bash
python3 agent_team_demo/agent_team_runtime.py \
  --config agent_team_demo/examples/agent-team.config.json \
  --target . \
  --output agent_team_demo/output
```

Host/workflow metadata can also be overridden directly:

```bash
python3 agent_team_demo/agent_team_runtime.py \
  --host-kind codex \
  --workflow-pack repo-audit \
  --workflow-preset default \
  --target . \
  --output agent_team_demo/output
```

Run the second built-in workflow pack:

```bash
python3 agent_team_demo/agent_team_runtime.py \
  --workflow-pack repo-audit \
  --target . \
  --output agent_team_demo/output_repo_audit
```

Strict mode (do not fallback to heuristic):

```bash
python3 agent_team_demo/agent_team_runtime.py \
  --provider openai \
  --require-llm \
  --target . \
  --output agent_team_demo/output
```

Tune adjudication strategy:

```bash
python3 agent_team_demo/agent_team_runtime.py \
  --target . \
  --output agent_team_demo/output \
  --adjudication-accept-threshold 80 \
  --adjudication-challenge-threshold 55 \
  --adjudication-weight-completeness 0.4 \
  --adjudication-weight-rebuttal-coverage 0.4 \
  --adjudication-weight-argument-depth 0.2 \
  --peer-wait-seconds 5 \
  --evidence-wait-seconds 5
```

Disable round3 auto-escalation:

```bash
python3 agent_team_demo/agent_team_runtime.py \
  --target . \
  --output agent_team_demo/output \
  --no-auto-round3-on-challenge
```

Tune re-adjudication bonus:

```bash
python3 agent_team_demo/agent_team_runtime.py \
  --target . \
  --output agent_team_demo/output \
  --re-adjudication-max-bonus 20 \
  --re-adjudication-weight-coverage 0.7 \
  --re-adjudication-weight-depth 0.3
```

Disable dynamic task insertion:

```bash
python3 agent_team_demo/agent_team_runtime.py \
  --target . \
  --output agent_team_demo/output \
  --no-dynamic-tasks
```

Enable provider-backed teammate debate replies:

```bash
python3 agent_team_demo/agent_team_runtime.py \
  --target . \
  --output agent_team_demo/output \
  --teammate-provider-replies \
  --teammate-memory-turns 6
```

Run with tmux teammate mode:

```bash
python3 agent_team_demo/agent_team_runtime.py \
  --target . \
  --output agent_team_demo/output \
  --teammate-mode tmux
```

Tune tmux worker timeout/fallback:

```bash
python3 agent_team_demo/agent_team_runtime.py \
  --target . \
  --output agent_team_demo/output \
  --teammate-mode tmux \
  --tmux-worker-timeout-sec 180 \
  --no-tmux-fallback-on-error
```

Resume from a checkpoint:

```bash
python3 agent_team_demo/agent_team_runtime.py \
  --target . \
  --output agent_team_demo/output \
  --resume-from agent_team_demo/output/run_checkpoint.json
```

Create a partial run on purpose (for resume testing):

```bash
python3 agent_team_demo/agent_team_runtime.py \
  --target . \
  --output agent_team_demo/output_resume_test \
  --max-completed-tasks 3
```

Rewind to a historical checkpoint snapshot by index:

```bash
python3 agent_team_demo/agent_team_runtime.py \
  --target . \
  --output agent_team_demo/output_resume_test \
  --rewind-to-history-index 0 \
  --max-completed-tasks 1
```

Rewind by event index (maps to nearest checkpoint snapshot):

```bash
python3 agent_team_demo/agent_team_runtime.py \
  --target . \
  --output agent_team_demo/output_resume_test \
  --rewind-to-event-index 0 \
  --max-completed-tasks 1
```

Rewind and fork into a branch output (keep source output immutable):

```bash
python3 agent_team_demo/agent_team_runtime.py \
  --target . \
  --output agent_team_demo/output_resume_test \
  --rewind-to-history-index 0 \
  --rewind-branch \
  --max-completed-tasks 1
```

Generate a checkpoint history replay report:

```bash
python3 agent_team_demo/agent_team_runtime.py \
  --output agent_team_demo/output_resume_test \
  --history-replay-report
```

Generate an event replay report (state transitions reconstructed from `events.jsonl`):

```bash
python3 agent_team_demo/agent_team_runtime.py \
  --output agent_team_demo/output_resume_test \
  --event-replay-report
```

## Output Artifacts

After runtime execution, output directory contains:

- `final_report.md`: human-readable recommendations
- `task_board.json`: task states/results
- `events.jsonl`: trace of task/message/lock events
- `shared_state.json`: shared runtime data
- `file_locks.json`: lock state snapshot
- `context_boundaries.json`: prepared task-context scopes and visible shared-state keys per agent/task
- `session_boundaries.json`: host/session boundary posture for each teammate session
- `teammate_sessions.json`: persistent per-agent session ids, transport, memory, recent task/message history, and resume continuity metadata
- `team_progress.json`: per-agent progress, backlog readiness, and message activity
- `team_progress.md`: lead-facing team progress dashboard
- `run_summary.json`: pointers to all artifacts
- `run_summary.json` now also includes `host`, `team`, `workflow`, `policies`, and `agent_team_config`
- `run_summary.json` also includes `session_boundary_path`
- `run_summary.json` also includes `teammate_sessions_path`
- `run_summary.json` also includes `team_progress_path` and `team_progress_report_path`
- `run_summary.json` also includes `context_boundary_path`
- `run_checkpoint.json`: resumable runtime checkpoint snapshot
- `_checkpoint_history/checkpoint_XXXXXX.json`: checkpoint history timeline for rewind (with `event_count`)
- `checkpoint_replay.md`: optional replay report generated by `--history-replay-report`
- `event_replay.md`: optional event replay report generated by `--event-replay-report`
- `branches/rewind_XXXXXX_*/`: optional rewind branch outputs when `--rewind-branch` is enabled
- `run_summary.json` includes rewind lineage keys: `rewind_history_index`, `rewind_event_index`, `rewind_event_resolution`,
  `rewind_source_output_dir`, `rewind_source_checkpoint`, `branch_run_id`,
  `rewind_seed_event_index`, `rewind_seed_event_count`
- `events.jsonl` includes monotonic `event_index` for deterministic replay mapping

Branch rewind behavior:

- first branch run seeds `events.jsonl` from source output up to rewind point
- subsequent branch resumes keep branch-local event history (no reseed overwrite)

The report now includes:

- Team findings
- Dynamic tasking summary (inserted follow-up tasks + dependency gates)
- Peer challenge rounds summary (agent-to-agent critiques + rebuttals + optional refinement round)
- Evidence pack summary (triggered/missing/replies)
- Lead adjudication initial/final verdict, score, thresholds, and rubric weights
- LLM synthesis
- Final recommended actions
- Session boundary summary (host-native vs tmux-backed vs runtime-emulated teammate session isolation)
- Teammate session summary (per-agent session status, transport, memory depth, recent task history)
- Team progress summary (per-agent completed/failed/ready/blocked counts)
- Context boundary summary (task-scoped shared-state visibility by agent/task in `context_boundaries.json`)

## Tests and Validation

Run unit + integration tests:

```bash
python3 -m unittest discover -s agent_team_demo/tests -p "test_*.py"
```

Smoke run (heuristic provider):

```bash
python3 agent_team_demo/agent_team_runtime.py \
  --target . \
  --output agent_team_demo/output_smoke \
  --provider heuristic
```

Validation checklist and latest verification notes:

- `VALIDATION.md`

## Reusable Skill

Reusable skill package path:

- `agent_team_demo/skills/agent-team-runtime`

Quick usage with bundled skill scripts:

```bash
python3 agent_team_demo/skills/agent-team-runtime/scripts/run_runtime.py \
  --preset forced-challenge \
  --target . \
  --output agent_team_demo/output_skill_forced
python3 agent_team_demo/skills/agent-team-runtime/scripts/verify_run.py \
  --output agent_team_demo/output_skill_forced \
  --require-evidence-events
```
