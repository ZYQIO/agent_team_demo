# Agent Team Demo Validation

Date: 2026-03-09

## Scope

This document tracks repeatable checks for the local `agent_team_runtime.py` implementation:

- Core task orchestration and dependency flow
- Dynamic task insertion and dependency gating (`dynamic_planning`)
- Peer challenge rounds + lead adjudication
- Challenge closed-loop (`evidence_pack` + `lead_re_adjudication`)
- Optional provider-backed teammate reply generation
- Checkpoint persistence, history timeline, rewind/resume flow, rewind branching, and event-index rewind mapping
  (`run_checkpoint.json` + `_checkpoint_history` + `--resume-from` + `--rewind-to-history-index` + `--rewind-to-event-index` + `--rewind-branch`)
- Artifact completeness and event traceability
- Teammate session ledger generation, resume continuity tracking, and final report append behavior
- Host enforcement artifact generation, advertised-vs-active host isolation reporting, and final report append behavior
- Session-boundary posture artifact generation, tmux workspace isolation metadata, and final report append behavior
- Team progress artifact generation and report append behavior
- Task-context boundary generation and context summary artifact
- Config-driven host/model/team/workflow loading via `--config`

## Automated Checks

Run all tests:

```bash
python3 -m unittest discover -s agent_team_demo/tests -p "test_*.py"
```

Coverage by test files:

- `tests/test_runtime_logic.py`
  - adjudication score/verdict logic
  - evidence bonus math
  - focus area derivation
  - targeted evidence question generation
  - dynamic task insertion behavior
- `tests/test_runtime_end_to_end.py`
  - CLI end-to-end run writes all artifacts
  - all tasks (including dynamically inserted follow-ups) reach `completed`
  - event log includes run lifecycle + lead adjudication events
  - `evidence_pack` targeted flow behavior
  - `lead_re_adjudication` bonus application behavior

## Manual Smoke Checks

Default run:

```bash
python3 agent_team_demo/agent_team_runtime.py \
  --target . \
  --output agent_team_demo/output_smoke \
  --provider heuristic
```

Forced challenge run (deterministically exercises evidence closed-loop):

```bash
python3 agent_team_demo/agent_team_runtime.py \
  --target . \
  --output agent_team_demo/output_smoke_forced_challenge \
  --provider heuristic \
  --adjudication-accept-threshold 95 \
  --adjudication-challenge-threshold 0 \
  --peer-wait-seconds 0.01 \
  --evidence-wait-seconds 1
```

Verify artifacts in output directory:

- `final_report.md`
- `task_board.json`
- `events.jsonl`
- `shared_state.json`
- `file_locks.json`
- `context_boundaries.json`
- `host_enforcement.json`
- `session_boundaries.json`
- `teammate_sessions.json`
- `team_progress.json`
- `team_progress.md`
- `run_summary.json`
- `run_checkpoint.json`

Optional backward-compatibility smoke check (disable dynamic insertion):

```bash
python3 agent_team_demo/agent_team_runtime.py \
  --target . \
  --output agent_team_demo/output_smoke_static \
  --provider heuristic \
  --no-dynamic-tasks
```

Config-driven smoke check:

```bash
python3 agent_team_demo/agent_team_runtime.py \
  --config agent_team_demo/examples/agent-team.config.json \
  --target . \
  --output agent_team_demo/output_smoke_config
```

Tmux-mode smoke check:

```bash
python3 agent_team_demo/agent_team_runtime.py \
  --target . \
  --output agent_team_demo/output_smoke_tmux \
  --provider heuristic \
  --teammate-mode tmux
```

Resume smoke check:

```bash
python3 agent_team_demo/agent_team_runtime.py \
  --target . \
  --output agent_team_demo/output_resume_smoke \
  --provider heuristic \
  --max-completed-tasks 3
python3 agent_team_demo/agent_team_runtime.py \
  --target . \
  --output agent_team_demo/output_resume_smoke \
  --provider heuristic \
  --resume-from agent_team_demo/output_resume_smoke/run_checkpoint.json
```

Rewind smoke check:

```bash
python3 agent_team_demo/agent_team_runtime.py \
  --target . \
  --output agent_team_demo/output_resume_smoke \
  --provider heuristic \
  --rewind-to-history-index 0 \
  --max-completed-tasks 1
```

Event-index rewind smoke check:

```bash
python3 agent_team_demo/agent_team_runtime.py \
  --target . \
  --output agent_team_demo/output_resume_smoke \
  --provider heuristic \
  --rewind-to-event-index 0 \
  --max-completed-tasks 1
```

Rewind-branch smoke check:

```bash
python3 agent_team_demo/agent_team_runtime.py \
  --target . \
  --output agent_team_demo/output_resume_smoke \
  --provider heuristic \
  --rewind-to-history-index 0 \
  --rewind-branch \
  --max-completed-tasks 1
```

History replay report smoke check:

```bash
python3 agent_team_demo/agent_team_runtime.py \
  --output agent_team_demo/output_resume_smoke \
  --history-replay-report
```

Event replay report smoke check:

```bash
python3 agent_team_demo/agent_team_runtime.py \
  --output agent_team_demo/output_resume_smoke \
  --event-replay-report
```

## Acceptance Criteria

- Single command run completes without manual intervention.
- Shared task dependency graph reaches terminal state.
- Peer challenge and lead adjudication sections appear in report.
- Evidence pack data is persisted when verdict is `challenge`.
- Event log preserves key lifecycle events (`run_started`, task claim/complete, `run_finished`).
- Forced challenge run shows `evidence_round_started` and `lead_re_adjudication_published`.
- Default run with dynamic insertion shows `task_inserted`, `task_dependency_added`, `TaskCompleted`, and `TeammateIdle`.
- Provider-backed teammate mode should emit `teammate_provider_reply_generated` (or fallback events if provider call fails).
- Tmux mode should emit `teammate_mode_tmux_enabled`, `tmux_worker_task_dispatched`, and `tmux_worker_task_completed`.
- Tmux diagnostics should emit `tmux_worker_transport_result`; when tmux worker fails and fallback is enabled,
  runtime should emit `tmux_worker_fallback_attempt` and `tmux_worker_fallback_result`.
- Resume flow should emit `run_resume_loaded` and finish with all tasks `completed` after the second run.
- Resume flow should emit `teammate_session_resumed`, preserve prior `session_id` values in `teammate_sessions.json`,
  and record non-zero session resume counts after the second run.
- Rewind flow should load `_checkpoint_history/checkpoint_XXXXXX.json`, emit `run_resume_loaded`,
  and reflect `rewind_history_index` in `run_summary.json`.
- Event-index rewind should emit `rewind_event_index` and `rewind_event_resolution` in `run_summary.json`
  and include mapped checkpoint lineage in runtime logs.
- Rewind-branch flow should write to `output/branches/rewind_*/` and persist
  `rewind_source_output_dir`, `rewind_source_checkpoint`, `branch_run_id`,
  `rewind_seed_event_index`, and `rewind_seed_event_count` in `run_summary.json`.
- Rewind-branch flow should emit `run_branch_events_seeded` on first branch run and keep branch-local
  event history on subsequent branch resumes.
- History replay mode should generate `checkpoint_replay.md` with snapshot timeline sections.
- Event replay mode should generate `event_replay.md` with reconstructed status counts and transitions.
- Final report should include a `Team Progress` section and `run_summary.json` should point to
  `team_progress.json` and `team_progress.md`.
- Final report should include a `Teammate Sessions` section and `run_summary.json` should point to
  `teammate_sessions.json`.
- Final report should include a `Host Enforcement` section and `run_summary.json` should point to
  `host_enforcement.json`.
- Final report should include a `Session Boundaries` section and `run_summary.json` should point to
  `session_boundaries.json`.
- Tmux runs should record at least one teammate session boundary with `workspace_root`,
  `workspace_tmp_dir`, and `workspace_isolation_active=true`.
- Event log should emit `task_context_prepared` and `run_summary.json` should point to
  `context_boundaries.json`.
