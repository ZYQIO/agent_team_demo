# Agent Team Demo

This folder now contains two layers:

1. `agent_team_demo.py`
- Minimal 3-role demo (`Planner / Executor / Reviewer`).

2. `agent_team_runtime.py`
- A richer local MVP inspired by Claude Code Agent Teams:
  - lead + teammates
  - shared task board with dependencies
  - mailbox messaging with output-scoped file-backed delivery in runtime runs, including transport-local mailbox views for worker/helper sessions
  - teammate debate rounds (round1 challenge + round2 rebuttal + optional round3 refinement)
  - lead adjudication with configurable verdict thresholds and rubric weights
  - challenge-closed-loop: supplemental evidence pack + lead re-adjudication
  - task assignment constraints with `allowed_agent_types` (Task(agent_type)-style gating)
  - compatibility hook events: `TeammateIdle` and `TaskCompleted`
  - team progress artifacts and lead-facing progress summary
  - lead interaction artifacts plus resumable teammate plan approval (`--teammate-plan-required`, `--approve-plan`, `--reject-plan`, `--approve-all-pending-plans`)
  - optional live lead command channel through `lead_commands.jsonl` while the run is waiting on teammate plan approval
  - optional embedded `stdin` lead prompt via `--lead-interactive` while the run is waiting on teammate plan approval
  - a terminal `lead_console.py` helper for in-run status inspection plus `show` / approve / reject commands
  - explicit teammate task-context boundaries with per-run context summary artifact
  - durable teammate session ledger with per-agent task/memory/activity snapshots, explicit resume continuity markers, and worker session workspace metadata
  - explicit session-boundary posture artifact describing host-native, tmux-backed, worker-subprocess-backed, or runtime-emulated session isolation
  - teammate execution mode toggle (`in-process` / `subprocess` / `tmux` / `host`, with subprocess fallback when tmux binary is unavailable, reviewer planning/report/llm_synthesis offload in subprocess mode while mailbox-driven reviewer tasks stay on the parent mailbox path, and host mode now launching external session-worker subprocesses for built-in workflow analyst tasks plus reviewer mailbox/planning/report/llm tasks via explicit `session_task_assignment`, `session_task_result`, and `session_telemetry` mailbox contracts)
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

## Research, Handoff, and Plan

- `PROJECT_HANDOFF.md`: fastest way to resume work from another context
- `ACTIVE_PLAN.md`: current prioritized execution plan
- `WORKLOG.md`: chronological engineering log and recent checkpoints
- `OPERATING_RULES.md`: project execution rules, validation, and handoff expectations
- `claude_agent_teams_research.md`: deep-dive notes from official docs/changelog
- `agent_team_implementation_plan.md`: original implementation plan and milestones (archival)
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

Require lead approval before teammate plan mutations are applied:

```bash
python3 agent_team_demo/agent_team_runtime.py \
  --target . \
  --output agent_team_demo/output \
  --teammate-plan-required

python3 agent_team_demo/agent_team_runtime.py \
  --target . \
  --output agent_team_demo/output \
  --resume-from agent_team_demo/output/run_checkpoint.json \
  --approve-plan dynamic_planning
```

Keep the run alive and approve a pending plan without resuming:

```bash
python3 agent_team_demo/agent_team_runtime.py \
  --target . \
  --output agent_team_demo/output \
  --teammate-plan-required \
  --lead-command-wait-seconds 30

python3 agent_team_demo/skills/agent-team-runtime/scripts/send_lead_command.py \
  --output agent_team_demo/output \
  --approve-plan dynamic_planning
```

Use the live terminal console instead of writing commands manually:

```bash
python3 agent_team_demo/agent_team_runtime.py \
  --target . \
  --output agent_team_demo/output \
  --teammate-plan-required \
  --lead-command-wait-seconds 30

python3 agent_team_demo/skills/agent-team-runtime/scripts/lead_console.py \
  --output agent_team_demo/output

python3 agent_team_demo/skills/agent-team-runtime/scripts/lead_console.py \
  --output agent_team_demo/output \
  --approve-plan dynamic_planning
```

The live snapshot, terminal console, and embedded prompt now show previews of proposed inserted tasks and dependency additions before approval is applied. The terminal console and embedded prompt also support `show <task_id>` for one pending request's detailed inspection.

Use an embedded lead prompt inside the runtime process:

```bash
python3 agent_team_demo/agent_team_runtime.py \
  --target . \
  --output agent_team_demo/output \
  --teammate-plan-required \
  --lead-interactive
```

When a pending teammate plan appears, the runtime will prompt:

```text
lead-approval> show dynamic_planning
lead-approval> approve dynamic_planning
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

With `--host-kind codex`, assigned host-mode teammate tasks now use a persistent Codex-backed session backend (`host_session_backend=codex_exec`) that reuses `codex exec` / `codex exec resume` thread ids. Session isolation is host-backed on that path; workspace isolation is still unavailable.

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

Run with host teammate mode:

```bash
python3 agent_team_demo/agent_team_runtime.py \
  --target . \
  --output agent_team_demo/output_host \
  --host-kind claude-code \
  --teammate-mode host
```

Host mode backend now depends on `host_kind`. `--host-kind claude-code` currently uses external session-worker subprocesses for the built-in workflow teammate task paths: analyst scans/follow-ups plus mailbox-driven reviewer flows (`peer_challenge`, `evidence_pack`), reviewer planning tasks (`dynamic_planning`, `repo_dynamic_planning`), reviewer `llm_synthesis`, report tasks (`recommendation_pack`, `repo_recommendation_pack`), and teammate auto-replies. `--host-kind codex` now runs those assigned-task paths through a persistent Codex-backed host session backend (`host_session_backend=codex_exec`) while preserving the same explicit `session_task_assignment` / `session_task_result` / `session_telemetry` contracts. `host_enforcement.json` records the active backend explicitly so host-mode artifacts no longer blur transport-backed `external_process` workers with true host-backed sessions.

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
- `host_enforcement.json`: runtime-resolved host/session enforcement posture, separating advertised host capabilities from the active backend in use and recording whether host mode is transport-backed (`host_session_backend=external_process`) or host-managed (`host_session_backend=codex_exec`)
- `session_boundaries.json`: host/session boundary posture for each teammate session, including `transport_backend`, workspace scope, isolated temp/session directories, and host-native descriptors such as `host://<host-kind>/sessions/<session_id>/...` only when a true host-native workspace boundary is active
- `teammate_sessions.json`: persistent per-agent session ids, transport, transport-session names, memory, recent task/message history, and resume continuity metadata
- `team_progress.json`: per-agent progress, backlog readiness, and message activity
- `team_progress.md`: lead-facing team progress dashboard
- `run_summary.json`: pointers to all artifacts
- `run_summary.json` now also includes `host`, `team`, `workflow`, `policies`, and `agent_team_config`
- `run_summary.json` also includes `host_enforcement_path`
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
- Host enforcement summary (configured host transport vs runtime-active enforcement)
- Session boundary summary (host-native vs tmux-backed vs worker-subprocess-backed vs runtime-emulated teammate session isolation)
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
