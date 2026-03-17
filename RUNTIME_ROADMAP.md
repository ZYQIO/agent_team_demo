# Agent Team Runtime Roadmap

Current status snapshot: `2026-03-17`

This document tracks the working plan, completed refactors, validation status, and the recommended next tasks for `agent_team_demo`.

## 1. Current Objective

The project has already completed the original MVP goal in [agent_team_implementation_plan.md](/Users/zouxiaoyi/Desktop/project/学习总结/agent_team_demo/agent_team_implementation_plan.md): a runnable local lead-plus-teammates workflow with task board, mailbox, file locks, event logs, and final artifacts.

The current objective is different:

- Move from a runnable MVP to a reusable `Agent Team runtime`
- Reduce the size and coupling of [agent_team_runtime.py](/Users/zouxiaoyi/Desktop/project/学习总结/agent_team_demo/agent_team_runtime.py)
- Improve practical parity with Claude Code Agent Teams without breaking current behavior
- Keep CLI, tests, artifacts, and skill scripts stable during the refactor

Official parity review (2026-03-11):

- the repo is still pointed at the right target shape: long-lived teammates, shared task coordination, messaging, and independent teammate sessions
- the backlog is slightly skewed toward replay/rewind depth and workflow-specific debate mechanics
- current Claude Code Agent Teams docs make lead-facing team interaction and plan approval higher-value parity work than deeper replay
- latest runtime slices now add live-updating lead interaction snapshots, resumable CLI plan approval, proposed task/dependency previews inside those approval surfaces, detailed `show <task_id>` inspection inside the live console and embedded stdin prompt, file-backed live command intake, a terminal lead console, an embedded stdin approval prompt, a true host-backed `codex` session backend, a guarded `claude_exec` backend, and explicit local `claude-code` relay/subscription prerequisite reporting in host enforcement, so the remaining gap is richer embedded in-run interaction plus live validation of the Claude backend in an official-ready environment rather than total absence of approval flow or host-backed sessions

## 2. Target Architecture

The intended architecture is now:

- `agent_team/core.py`
  Runtime primitives: task board, mailbox, file locks, shared state, event logger
- `agent_team/runtime/`
  Runtime internals: adjudication, persistence, engine
- `agent_team/transports/`
  Teammate execution transports: `in-process`, `subprocess`, `tmux`, `host`
- `agent_team/workflows/`
  Workflow packs: task graph and workflow-specific handlers
- `agent_team_runtime.py`
  Compatibility entrypoint, CLI, thin wrappers for tests and scripts

## 3. Progress Board

| Workstream | Status | Notes |
|---|---|---|
| MVP runtime runnable | Completed | Original local lead + teammates flow is working. |
| Baseline validation | Completed | Unit/integration tests, smoke run, verifier all passed before refactor. |
| Runtime rules extraction | Completed | Adjudication and persistence moved out of main file. |
| Transport extraction | Completed | `in-process` and `tmux` logic moved into transport modules. |
| Engine extraction | Completed | Main run loop and runtime context moved into `runtime/engine.py`. |
| Workflow handler extraction | Completed | Markdown-specific `handle_*` logic moved into workflow handler module. |
| Compatibility preservation | Completed | Existing tests still call symbols from `agent_team_runtime.py`. |
| Lead orchestration generalization | Completed | Workflow packs now declare lead task order and engine consumes that metadata. |
| Handler module split | Completed | Markdown workflow handlers are now split into shared, analysis, challenge, and reporting modules. |
| Workflow contract expansion | Completed | Workflow packs now declare runtime metadata beyond task graph and handlers. |
| tmux diagnostics hardening | Completed | tmux worker runs now emit structured diagnostics artifact with transport and fallback details. |
| tmux session lifecycle hardening | Completed | tmux worker runs now record timeout/cleanup lifecycle metadata and clean IPC files after execution. |
| transport timeout resilience | Completed | subprocess timeout paths now degrade into structured failures instead of uncaught exceptions. |
| tmux spawn retry recovery | Completed | duplicate-session spawn failures now retry with fresh session names and emit structured retry metadata. |
| tmux stale-session cleanup | Completed | duplicate-session recovery now attempts stale-session cleanup and records cleanup metadata. |
| tmux stale-session recovery retry | Completed | failed stale-session cleanup now verifies lingering sessions and retries cleanup with structured recovery metadata. |
| tmux active-session cleanup recovery | Completed | worker-session cleanup now verifies lingering sessions and retries cleanup when `kill-session` fails. |
| tmux orphan-session preflight cleanup | Completed | worker launches now clean prior same-prefix orphan tmux sessions before spawning new work. |
| tmux stable session identity | Completed | worker launches now prefer stable session names, retry same-name recovery, and track reuse strategy in diagnostics. |
| tmux preferred-session reuse recovery | Completed | worker launches now reuse exact existing preferred sessions via `respawn-pane` before falling back to cleanup/retry. |
| tmux session lease strategy | Completed | preferred worker sessions can now be retained across likely future analyst tasks and swept at the end of the run. |
| tmux cleanup sweep artifact | Completed | retained-session cleanup summaries now persist as a standalone artifact and are referenced from `run_summary.json`. |
| tmux explicit lease ledger | Completed | preferred-session reuse is now authorized from a runtime-managed lease ledger that also persists worker lease state into artifacts. |
| tmux resume-aware lease recovery | Completed | resumed tmux runs now reconcile retained lease state up front and persist recovery summaries into runtime artifacts. |
| tmux artifact verification hardening | Completed | verifier now checks tmux recovery, cleanup, lease, and diagnostics artifacts for tmux-mode runs. |
| tmux deferred cleanup + artifact history | Completed | pause-for-resume runs now defer tmux cleanup, preserve retained leases for resume, and persist cleanup/recovery history alongside the latest summary snapshot. |
| resume runtime-config inheritance | Completed | resumed runs now inherit checkpoint runtime settings by default and only change behavior when current CLI/config explicitly overrides them. |
| Workflow plugin maturity | Completed | Built-in packs now include `markdown-audit` and `repo-audit` on the same runtime. |
| Team progress artifacts | Completed | Runtime now writes `team_progress.json` + `team_progress.md` and appends a per-agent progress section into `final_report.md`. |
| Task-context boundaries | Completed | Runtime now prepares task-scoped shared-state views, emits `task_context_prepared`, and writes `context_boundaries.json` for per-agent/task visibility auditing. |
| Teammate session ledger | Completed | Runtime now maintains durable per-agent session ids, transport, recent task history, message history, and provider memory in `teammate_sessions.json`. |
| File-backed mailbox backend | Completed | Runtime runs now use an output-scoped `_mailbox/` directory to preserve `send` / `pull` / `pull_matching` semantics across separate mailbox instances instead of relying on one in-memory inbox object. |
| Mailbox transport views | Completed | File-backed mailbox pulls now atomically claim message files, and worker/helper contexts consume transport-local mailbox views instead of only the lead runtime mailbox object. |
| Host mailbox reviewer session dispatch | Completed | In host mode, `peer_challenge` and `evidence_pack` now dispatch onto the reviewer's long-lived teammate session thread instead of running lead-inline. |
| Host mailbox assignment contract | Completed | Host-mode mailbox reviewer dispatch now uses explicit `session_task_assignment` mailbox messages so task assignment itself no longer depends on transport-private thread queues. |
| Host mailbox result contract | Completed | Host-mode mailbox reviewer tasks now publish explicit `session_task_result` mailbox messages so lead-side code applies shared-state updates and task completion/failure instead of worker threads mutating workflow state directly. |
| Host session telemetry contract | Completed | Host-mode long-lived session threads now publish explicit `session_telemetry` mailbox messages so teammate session ledger updates are applied on the lead side instead of worker threads mutating `session_registry` directly. |
| Host external session-worker boundary | Completed | Host-mode mailbox reviewer/request-reply flows now run through external session-worker subprocesses backed by the file-backed mailbox transport while lead-side code still owns shared-state updates and task completion. |
| Host reviewer planning task-mutation contract | Completed | Host-mode reviewer `dynamic_planning` and `repo_dynamic_planning` tasks now execute through the external assigned-task/session-worker path and return explicit task-mutation payloads for the lead side to apply. |
| Host analyst session-worker contract | Completed | Host-mode analyst scans and follow-up tasks for the built-in workflow packs now execute through the external assigned-task/session-worker path and return explicit lead-applied state updates instead of mutating shared state directly. |
| Session continuity on resume | Completed | Resumed runs now preserve prior teammate `session_id` values, increment session lifecycle counters, and emit explicit `teammate_session_resumed` events. |
| Tmux session workspaces | Completed | Tmux-mode workers now get stable session-scoped workspace/temp directories that are surfaced in `teammate_sessions.json` and `session_boundaries.json`. |
| Tmux workspace recovery continuity | Completed | Retained tmux lease recovery now restores workspace/session boundary metadata before the next analyst task runs. |
| Tmux target snapshot isolation | Completed | Tmux-mode workers now rewrite `target_dir` reads to a stable session-local source snapshot and persist the isolated `workspace_target_dir` in session artifacts. |
| Tmux execution-root isolation | Completed | Tmux-mode workers now execute with session-local `cwd`, `HOME`, cache/config dirs, and subprocess fallback workdirs surfaced in session artifacts. |
| Subprocess analyst mode | Completed | Analyst tasks can now run as first-class worker subprocesses without tmux, reusing session-scoped workdir/home/target isolation while reviewer tasks remain in-process. |
| Subprocess reviewer planning | Completed | Reviewer `dynamic_planning` and `repo_dynamic_planning` tasks now execute in isolated worker subprocesses that return task-mutation plans for the parent runtime to apply; mailbox-driven reviewer tasks still stay in-process. |
| Subprocess reviewer reporting | Completed | Reviewer `recommendation_pack` and `repo_recommendation_pack` tasks now render the base `final_report.md` inside isolated worker subprocesses; mailbox-driven reviewer tasks still stay in-process. |
| Subprocess reviewer llm synthesis | Completed | Reviewer `llm_synthesis` now rebuilds the configured provider inside isolated worker subprocesses and preserves the existing `llm_synthesis` shared-state contract for downstream report generation. |
| Mailbox reviewer transport guardrail | Completed | `peer_challenge` and `evidence_pack` are now explicitly protected from accidental subprocess offload until mailbox request/reply semantics can cross process or host boundaries. |
| Host transport skeleton | Completed | `--teammate-mode host` now routes teammate work through a distinct host transport path and records host-managed session/workspace boundaries in runtime artifacts. |
| Host enforcement posture artifact | Completed | Runtime now emits `host_enforcement.json` so configured host capabilities are separated from runtime-active host/session enforcement decisions, including explicit host session backend identity when host mode is still transport-backed. |
| Session-boundary posture artifact | Completed | Runtime now emits `session_boundaries.json` and final-report summaries describing whether each teammate session is host-native, transport-backed external-process, tmux-backed, worker-subprocess-backed, or runtime-emulated. |
| True independent teammate sessions | Pending | Still `Partial` per [PARITY.md](/Users/zouxiaoyi/Desktop/project/学习总结/agent_team_demo/PARITY.md). |

## 4. Completed Work

### Phase A: Runtime Rules Split

Completed:

- Added [adjudication.py](/Users/zouxiaoyi/Desktop/project/学习总结/agent_team_demo/agent_team/runtime/adjudication.py)
- Added [persistence.py](/Users/zouxiaoyi/Desktop/project/学习总结/agent_team_demo/agent_team/runtime/persistence.py)
- Moved:
  - adjudication scoring
  - evidence bonus logic
  - checkpoint read/write
  - rewind/replay helpers
  - artifact writing helpers
- Kept compatibility exports in [agent_team_runtime.py](/Users/zouxiaoyi/Desktop/project/学习总结/agent_team_demo/agent_team_runtime.py)

### Phase B: Transport Split

Completed:

- Added [inprocess.py](/Users/zouxiaoyi/Desktop/project/学习总结/agent_team_demo/agent_team/transports/inprocess.py)
- Added [tmux.py](/Users/zouxiaoyi/Desktop/project/学习总结/agent_team_demo/agent_team/transports/tmux.py)
- Moved:
  - in-process teammate execution logic
  - tmux worker payload helpers
  - subprocess fallback path
  - tmux task execution wrapper
- Preserved wrapper functions in [agent_team_runtime.py](/Users/zouxiaoyi/Desktop/project/学习总结/agent_team_demo/agent_team_runtime.py) because tests monkeypatch them directly

### Phase C: Engine and Workflow Split

Completed:

- Added [engine.py](/Users/zouxiaoyi/Desktop/project/学习总结/agent_team_demo/agent_team/runtime/engine.py)
- Added [markdown_audit_handlers.py](/Users/zouxiaoyi/Desktop/project/学习总结/agent_team_demo/agent_team/workflows/markdown_audit_handlers.py)
- Updated [workflows/__init__.py](/Users/zouxiaoyi/Desktop/project/学习总结/agent_team_demo/agent_team/workflows/__init__.py) to resolve workflow handlers
- Moved:
  - `AgentContext`
  - team helper functions
  - lead run loop
  - workflow-specific `handle_*` functions
- Reduced [agent_team_runtime.py](/Users/zouxiaoyi/Desktop/project/学习总结/agent_team_demo/agent_team_runtime.py) to CLI + wrappers + compatibility exports

### Phase D: Workflow-Driven Lead Scheduling

Completed:

- Extended [workflows/__init__.py](/Users/zouxiaoyi/Desktop/project/学习总结/agent_team_demo/agent_team/workflows/__init__.py) with workflow-declared `lead_task_order`
- Updated [engine.py](/Users/zouxiaoyi/Desktop/project/学习总结/agent_team_demo/agent_team/runtime/engine.py) to execute lead tasks from workflow metadata instead of hard-coded task ids
- Added regression coverage in [test_runtime_logic.py](/Users/zouxiaoyi/Desktop/project/学习总结/agent_team_demo/tests/test_runtime_logic.py)
- Preserved compatibility in [agent_team_runtime.py](/Users/zouxiaoyi/Desktop/project/学习总结/agent_team_demo/agent_team_runtime.py)

### Phase E: Handler Module Split

Completed:

- Added [markdown_audit_shared.py](/Users/zouxiaoyi/Desktop/project/学习总结/agent_team_demo/agent_team/workflows/markdown_audit_shared.py)
- Added [markdown_audit_analysis.py](/Users/zouxiaoyi/Desktop/project/学习总结/agent_team_demo/agent_team/workflows/markdown_audit_analysis.py)
- Added [markdown_audit_challenge.py](/Users/zouxiaoyi/Desktop/project/学习总结/agent_team_demo/agent_team/workflows/markdown_audit_challenge.py)
- Added [markdown_audit_reporting.py](/Users/zouxiaoyi/Desktop/project/学习总结/agent_team_demo/agent_team/workflows/markdown_audit_reporting.py)
- Reduced [markdown_audit_handlers.py](/Users/zouxiaoyi/Desktop/project/学习总结/agent_team_demo/agent_team/workflows/markdown_audit_handlers.py) to a compatibility aggregation layer

### Phase F: Workflow Contract Expansion

Completed:

- Extended [workflows/__init__.py](/Users/zouxiaoyi/Desktop/project/学习总结/agent_team_demo/agent_team/workflows/__init__.py) with `WorkflowRuntimeMetadata`
- Workflow packs now declare:
  - lead task order
  - report task ids
- Updated [engine.py](/Users/zouxiaoyi/Desktop/project/学习总结/agent_team_demo/agent_team/runtime/engine.py) to resolve and consume workflow pack objects directly
- Added regression coverage in [test_runtime_logic.py](/Users/zouxiaoyi/Desktop/project/学习总结/agent_team_demo/tests/test_runtime_logic.py)

### Phase G: tmux Diagnostics Hardening

Completed:

- Enhanced [tmux.py](/Users/zouxiaoyi/Desktop/project/学习总结/agent_team_demo/agent_team/transports/tmux.py) to record structured diagnostics for every worker execution
- Added `tmux_worker_diagnostics.jsonl` artifact with transport, fallback, return code, and output preview fields
- Enriched tmux completion/failure events with fallback metadata
- Extended both logic and end-to-end tests to validate the diagnostics path

### Phase H: tmux Session Lifecycle Hardening

Completed:

- Enhanced [tmux.py](/Users/zouxiaoyi/Desktop/project/学习总结/agent_team_demo/agent_team/transports/tmux.py) to attach lifecycle metadata to tmux worker executions
- Added explicit timeout, cleanup, and IPC cleanup fields into `tmux_worker_diagnostics.jsonl`
- tmux worker IPC files are now cleaned up after success, timeout, or spawn failure paths
- Extended tests to cover direct tmux timeout cleanup behavior and lifecycle metadata propagation

### Phase I: Transport Timeout Resilience

Completed:

- Enhanced [tmux.py](/Users/zouxiaoyi/Desktop/project/学习总结/agent_team_demo/agent_team/transports/tmux.py) to convert subprocess `TimeoutExpired` exceptions into structured worker failures
- Added timeout metadata fields into `tmux_worker_diagnostics.jsonl`, including `execution_timed_out`, `timeout_transport`, and `timeout_phase`
- Enriched tmux transport events with timeout metadata so failures are visible in both diagnostics and `events.jsonl`
- Extended tests to cover direct subprocess timeout handling and tmux-to-subprocess fallback timeout handling

### Phase J: tmux Spawn Retry Recovery

Completed:

- Enhanced [tmux.py](/Users/zouxiaoyi/Desktop/project/学习总结/agent_team_demo/agent_team/transports/tmux.py) to retry tmux session creation when `new-session` fails with duplicate-session style errors
- Added spawn retry metadata fields into `tmux_worker_diagnostics.jsonl`, including `tmux_spawn_attempts`, `tmux_spawn_retried`, and `tmux_spawn_retry_reason`
- Enriched tmux transport events with spawn retry metadata so session recovery behavior is visible in `events.jsonl`
- Extended tests to cover duplicate-session retry recovery and diagnostics propagation

### Phase K: tmux Stale-Session Cleanup

Completed:

- Enhanced [tmux.py](/Users/zouxiaoyi/Desktop/project/学习总结/agent_team_demo/agent_team/transports/tmux.py) to attempt stale-session cleanup when tmux spawn fails with duplicate-session style errors
- Added stale-session cleanup metadata into `tmux_worker_diagnostics.jsonl`, including `tmux_stale_session_cleanup_attempted`, `tmux_stale_session_name`, and `tmux_stale_session_cleanup_result`
- Enriched tmux transport and task events with stale-session cleanup metadata
- Extended tests to assert stale-session cleanup is attempted before duplicate-session retry recovery completes

### Phase L: tmux Stale-Session Recovery Retry

Completed:

- Enhanced [tmux.py](/Users/zouxiaoyi/Desktop/project/学习总结/agent_team_demo/agent_team/transports/tmux.py) so failed stale-session cleanup now verifies whether the session still exists and retries cleanup when needed
- Added stale-session recovery retry metadata into `tmux_worker_diagnostics.jsonl`, including `tmux_stale_session_cleanup_retry_attempted`, `tmux_stale_session_cleanup_retry_result`, and `tmux_stale_session_exists_after_cleanup`
- Enriched tmux transport and task events with stale-session recovery retry metadata
- Extended tests to cover direct stale-session recovery retry and diagnostics propagation through worker fallback paths

### Phase M: tmux Active-Session Cleanup Recovery

Completed:

- Enhanced [tmux.py](/Users/zouxiaoyi/Desktop/project/学习总结/agent_team_demo/agent_team/transports/tmux.py) so active worker-session cleanup now verifies whether the tmux session still exists and retries cleanup when needed
- Added active cleanup recovery metadata into `tmux_worker_diagnostics.jsonl`, including `tmux_cleanup_retry_attempted`, `tmux_cleanup_retry_result`, and `tmux_session_exists_after_cleanup`
- Enriched tmux transport and task events with active cleanup recovery metadata
- Extended tests to cover direct active cleanup recovery retry and diagnostics propagation through worker fallback paths

### Phase N: tmux Orphan-Session Preflight Cleanup

Completed:

- Enhanced [tmux.py](/Users/zouxiaoyi/Desktop/project/学习总结/agent_team_demo/agent_team/transports/tmux.py) to list and clean same-prefix orphan tmux sessions before spawning a new worker session
- Added orphan-session preflight metadata into `tmux_worker_diagnostics.jsonl`, including `tmux_orphan_sessions_found`, `tmux_orphan_sessions_cleaned`, and `tmux_orphan_sessions_failed`
- Enriched tmux transport and task events with orphan-session cleanup metadata
- Extended tests to cover direct orphan-session preflight cleanup and diagnostics propagation through worker fallback paths

### Phase N2: tmux Stable Session Identity

Completed:

- Enhanced [tmux.py](/Users/zouxiaoyi/Desktop/project/学习总结/agent_team_demo/agent_team/transports/tmux.py) so worker launches now prefer a stable session name per worker before falling back to suffixed session names
- Duplicate-session recovery now retries the preferred session name after cleanup before generating a new suffixed session name
- Orphan-session preflight cleanup now also matches exact preferred session names, not only suffixed sessions
- Added preferred-session diagnostics fields, including `tmux_preferred_session_name`, `tmux_session_name_strategy`, `tmux_preferred_session_retried`, and `tmux_preferred_session_reused`
- Extended tests to cover stable-name retry behavior and exact-name orphan cleanup

### Phase N3: tmux Preferred-Session Reuse Recovery

Completed:

- Enhanced [tmux.py](/Users/zouxiaoyi/Desktop/project/学习总结/agent_team_demo/agent_team/transports/tmux.py) so exact preferred sessions detected during preflight are preserved for reuse instead of being eagerly treated as orphan sessions
- Duplicate-session handling for an exact preferred session now tries `tmux respawn-pane -k` before falling back to stale-session cleanup and respawn
- Added reuse diagnostics fields, including `tmux_preferred_session_found_preflight`, `tmux_preferred_session_reuse_attempted`, `tmux_preferred_session_reuse_result`, and `tmux_preferred_session_reused_existing`
- Extended tests to cover exact preferred-session reuse recovery and the adjusted orphan-cleanup semantics

### Phase N4: tmux Session Lease Strategy

Completed:

- Enhanced [tmux.py](/Users/zouxiaoyi/Desktop/project/学习总结/agent_team_demo/agent_team/transports/tmux.py) so analyst dispatch can retain exact preferred sessions when more tmux-compatible analyst work is still pending
- Added lease retention diagnostics fields, including `tmux_reuse_retention_requested` and `tmux_session_retained_for_reuse`
- Added end-of-run preferred-session cleanup sweep so retained analyst sessions are reconciled before artifacts are finalized
- Added persisted `tmux_session_cleanup_summary.json` artifact plus `run_summary.json` reference for cleanup sweep results
- Extended tests to cover preferred-session lease retention, cleanup sweep behavior, and engine-level cleanup callback execution

### Phase N5: tmux Explicit Lease Ledger

Completed:

- Enhanced [tmux.py](/Users/zouxiaoyi/Desktop/project/学习总结/agent_team_demo/agent_team/transports/tmux.py) so exact preferred-session reuse is only authorized when the runtime lease ledger marks that worker session as retained
- Added runtime-managed `tmux_session_leases` state entries with per-worker status, authorization flag, reuse count, transport, and cleanup results
- Added persisted [tmux_session_leases.json](/Users/zouxiaoyi/Desktop/project/学习总结/agent_team_demo/output_analysis_m8_lease_ledger_tmux/tmux_session_leases.json) artifact plus `run_summary.json` reference for lease-ledger inspection
- Extended tests to cover unauthorized exact-session cleanup, lease-authorized reuse dispatch, lease-ledger updates, and end-to-end artifact persistence

### Phase N6: tmux Resume-Aware Lease Recovery

Completed:

- Enhanced [tmux.py](/Users/zouxiaoyi/Desktop/project/学习总结/agent_team_demo/agent_team/transports/tmux.py) so resumed tmux runs reconcile persisted lease state before analyst dispatch begins
- Added recovery outcomes for retained, missing, inactive, and tmux-unavailable lease states, with `tmux_worker_session_recovery_sweep` and per-worker lease updates
- Added persisted [tmux_session_recovery_summary.json](/Users/zouxiaoyi/Desktop/project/学习总结/agent_team_demo/output_analysis_m8_resume_recovery_tmux/tmux_session_recovery_summary.json) artifact plus `run_summary.json` reference for recovery inspection
- Extended tests to cover retained-session recovery, missing-session invalidation, engine-level recovery callback wiring, and tmux end-to-end recovery artifacts

### Phase N7: tmux Artifact Verification Hardening

Completed:

- Enhanced [verify_run.py](/Users/zouxiaoyi/Desktop/project/学习总结/agent_team_demo/skills/agent-team-runtime/scripts/verify_run.py) so tmux-mode runs now require diagnostics plus recovery, cleanup, and lease artifact paths in `run_summary.json`
- Added end-to-end coverage to assert verifier success against current tmux output artifacts
- Re-verified both standard tmux output and resumed tmux recovery output with the hardened verifier

### Phase N8: tmux Deferred Cleanup and Artifact History

Completed:

- Enhanced [engine.py](/Users/zouxiaoyi/Desktop/project/学习总结/agent_team_demo/agent_team/runtime/engine.py) so intentional `max_completed_tasks` pauses mark tmux cleanup as deferred-for-resume before shutdown cleanup executes
- Enhanced [tmux.py](/Users/zouxiaoyi/Desktop/project/学习总结/agent_team_demo/agent_team/transports/tmux.py) so deferred cleanup preserves retained session leases and emits structured `deferred_for_resume` cleanup summaries instead of killing preferred sessions
- Enhanced [persistence.py](/Users/zouxiaoyi/Desktop/project/学习总结/agent_team_demo/agent_team/runtime/persistence.py) so tmux cleanup and recovery summaries now also append to `tmux_session_cleanup_history.jsonl` and `tmux_session_recovery_history.jsonl`
- Enhanced [verify_run.py](/Users/zouxiaoyi/Desktop/project/学习总结/agent_team_demo/skills/agent-team-runtime/scripts/verify_run.py) so tmux-mode runs must also ship non-empty cleanup/recovery history artifacts
- Extended tests to cover deferred cleanup semantics, resume-time history preservation, and faster tmux recovery callback validation

### Phase N9: Resume Runtime-Config Inheritance

Completed:

- Enhanced [agent_team_runtime.py](/Users/zouxiaoyi/Desktop/project/学习总结/agent_team_demo/agent_team_runtime.py) so resumed runs load runtime defaults from the checkpoint before CLI/config overrides are applied
- Resumed runs now preserve prior behavior for `peer_wait_seconds`, `evidence_wait_seconds`, `auto_round3_on_challenge`, tmux mode, and the rest of the runtime config unless the current invocation explicitly changes them
- Added logic coverage for both checkpoint-inherited defaults and explicit override precedence
- Extended CLI end-to-end coverage to assert resumed runs keep prior runtime settings even when the resume command omits those flags

### Phase O: Second Workflow Pack

Completed:

- Added [repo_audit.py](/Users/zouxiaoyi/Desktop/project/学习总结/agent_team_demo/agent_team/workflows/repo_audit.py)
- Added [repo_audit_analysis.py](/Users/zouxiaoyi/Desktop/project/学习总结/agent_team_demo/agent_team/workflows/repo_audit_analysis.py)
- Added [repo_audit_reporting.py](/Users/zouxiaoyi/Desktop/project/学习总结/agent_team_demo/agent_team/workflows/repo_audit_reporting.py)
- Added [repo_audit_handlers.py](/Users/zouxiaoyi/Desktop/project/学习总结/agent_team_demo/agent_team/workflows/repo_audit_handlers.py)
- Added [shared_challenge.py](/Users/zouxiaoyi/Desktop/project/学习总结/agent_team_demo/agent_team/workflows/shared_challenge.py) so workflow packs can reuse peer challenge, evidence, and re-adjudication logic
- Added [team_shared.py](/Users/zouxiaoyi/Desktop/project/学习总结/agent_team_demo/agent_team/workflows/team_shared.py) so workflow packs can reuse team roster helpers
- Registered `repo-audit` in [workflows/__init__.py](/Users/zouxiaoyi/Desktop/project/学习总结/agent_team_demo/agent_team/workflows/__init__.py) without changing [engine.py](/Users/zouxiaoyi/Desktop/project/学习总结/agent_team_demo/agent_team/runtime/engine.py)
- Extended [tmux.py](/Users/zouxiaoyi/Desktop/project/学习总结/agent_team_demo/agent_team/transports/tmux.py) so tmux/subprocess analyst execution also supports repository discovery and follow-up tasks
- Added logic and end-to-end coverage for the second workflow pack

## 5. Current File Layout

Key files after the refactor:

- [agent_team_runtime.py](/Users/zouxiaoyi/Desktop/project/学习总结/agent_team_demo/agent_team_runtime.py)
- [agent_team/runtime/adjudication.py](/Users/zouxiaoyi/Desktop/project/学习总结/agent_team_demo/agent_team/runtime/adjudication.py)
- [agent_team/runtime/persistence.py](/Users/zouxiaoyi/Desktop/project/学习总结/agent_team_demo/agent_team/runtime/persistence.py)
- [agent_team/runtime/engine.py](/Users/zouxiaoyi/Desktop/project/学习总结/agent_team_demo/agent_team/runtime/engine.py)
- [agent_team/transports/inprocess.py](/Users/zouxiaoyi/Desktop/project/学习总结/agent_team_demo/agent_team/transports/inprocess.py)
- [agent_team/transports/tmux.py](/Users/zouxiaoyi/Desktop/project/学习总结/agent_team_demo/agent_team/transports/tmux.py)
- [agent_team/workflows/__init__.py](/Users/zouxiaoyi/Desktop/project/学习总结/agent_team_demo/agent_team/workflows/__init__.py)
- [agent_team/workflows/markdown_audit.py](/Users/zouxiaoyi/Desktop/project/学习总结/agent_team_demo/agent_team/workflows/markdown_audit.py)
- [agent_team/workflows/markdown_audit_shared.py](/Users/zouxiaoyi/Desktop/project/学习总结/agent_team_demo/agent_team/workflows/markdown_audit_shared.py)
- [agent_team/workflows/markdown_audit_analysis.py](/Users/zouxiaoyi/Desktop/project/学习总结/agent_team_demo/agent_team/workflows/markdown_audit_analysis.py)
- [agent_team/workflows/markdown_audit_challenge.py](/Users/zouxiaoyi/Desktop/project/学习总结/agent_team_demo/agent_team/workflows/markdown_audit_challenge.py)
- [agent_team/workflows/markdown_audit_reporting.py](/Users/zouxiaoyi/Desktop/project/学习总结/agent_team_demo/agent_team/workflows/markdown_audit_reporting.py)
- [agent_team/workflows/markdown_audit_handlers.py](/Users/zouxiaoyi/Desktop/project/学习总结/agent_team_demo/agent_team/workflows/markdown_audit_handlers.py)

Current size snapshot:

- [agent_team_runtime.py](/Users/zouxiaoyi/Desktop/project/学习总结/agent_team_demo/agent_team_runtime.py): `858` lines
- [engine.py](/Users/zouxiaoyi/Desktop/project/学习总结/agent_team_demo/agent_team/runtime/engine.py): `765` lines
- [workflows/__init__.py](/Users/zouxiaoyi/Desktop/project/学习总结/agent_team_demo/agent_team/workflows/__init__.py): `82` lines
- [markdown_audit_handlers.py](/Users/zouxiaoyi/Desktop/project/学习总结/agent_team_demo/agent_team/workflows/markdown_audit_handlers.py): `44` lines
- [markdown_audit_shared.py](/Users/zouxiaoyi/Desktop/project/学习总结/agent_team_demo/agent_team/workflows/markdown_audit_shared.py): `40` lines
- [markdown_audit_analysis.py](/Users/zouxiaoyi/Desktop/project/学习总结/agent_team_demo/agent_team/workflows/markdown_audit_analysis.py): `179` lines
- [markdown_audit_challenge.py](/Users/zouxiaoyi/Desktop/project/学习总结/agent_team_demo/agent_team/workflows/markdown_audit_challenge.py): `351` lines
- [markdown_audit_reporting.py](/Users/zouxiaoyi/Desktop/project/学习总结/agent_team_demo/agent_team/workflows/markdown_audit_reporting.py): `245` lines

This is already better than a single monolithic runtime file. The next obvious structural hotspot is now [engine.py](/Users/zouxiaoyi/Desktop/project/学习总结/agent_team_demo/agent_team/runtime/engine.py).

Key runtime artifact added during M8:

- `tmux_worker_diagnostics.jsonl` written into output directories when tmux/subprocess worker execution paths run
- `tmux_worker_diagnostics.jsonl` now includes timeout/cleanup lifecycle metadata for tmux-backed executions
- `tmux_worker_diagnostics.jsonl` now also includes structured subprocess timeout metadata for direct and fallback execution paths
- `tmux_worker_diagnostics.jsonl` now also includes tmux spawn retry metadata for duplicate-session recovery paths
- `tmux_worker_diagnostics.jsonl` now also includes stale-session cleanup metadata for duplicate-session recovery paths
- `tmux_worker_diagnostics.jsonl` now also includes stale-session recovery retry metadata for failed cleanup paths
- `tmux_worker_diagnostics.jsonl` now also includes active-session cleanup recovery retry metadata for worker session cleanup paths
- `tmux_worker_diagnostics.jsonl` now also includes orphan-session preflight cleanup metadata for interruption recovery paths
- `tmux_worker_diagnostics.jsonl` now also includes cross-task preferred-session lease retention metadata
- `tmux_worker_diagnostics.jsonl` now also includes explicit preferred-session reuse authorization metadata
- `tmux_session_recovery_summary.json` now persists resume-aware lease recovery decisions for tmux runs
- `tmux_session_cleanup_summary.json` now persists the end-of-run cleanup sweep summary for retained tmux sessions
- `tmux_session_leases.json` now persists explicit worker lease state, including authorization, reuse count, and cleanup status
- `tmux_session_cleanup_history.jsonl` now preserves cleanup sweep history across pause/resume cycles instead of only keeping the latest cleanup snapshot
- `tmux_session_recovery_history.jsonl` now preserves recovery sweep history across resumed runs instead of only keeping the latest recovery snapshot

## 6. Validation Status

Latest verified on `2026-03-10`.

### Tests

Command:

```bash
python3 -m unittest discover -s agent_team_demo/tests -p "test_*.py"
```

Result:

- `95/95` tests passed

### Smoke Runs

Reviewer subprocess smoke run:

```bash
python3 agent_team_demo/agent_team_runtime.py   --target .   --output .codex_tmp/smoke_output_llm_subprocess   --provider heuristic   --teammate-mode subprocess   --peer-wait-seconds 1   --evidence-wait-seconds 1
```

Host transport smoke run:

```bash
python3 agent_team_demo/agent_team_runtime.py   --target .   --output .codex_tmp/smoke_output_host_mode   --provider heuristic   --host-kind claude-code   --teammate-mode host   --peer-wait-seconds 1   --evidence-wait-seconds 1
```

Artifact verification:

```bash
python3 agent_team_demo/skills/agent-team-runtime/scripts/verify_run.py   --output .codex_tmp/smoke_output_llm_subprocess

python3 agent_team_demo/skills/agent-team-runtime/scripts/verify_run.py   --output .codex_tmp/smoke_output_host_mode
```

Evidence review:

- subprocess smoke confirms reviewer `task_history` contains `llm_synthesis=subprocess`
- host smoke confirms the current `claude-code` path still reports `transport=host`, `transport_backend=external_process`, and transport-backed host session enforcement instead of overstated host-native boundaries
- direct Codex backend probes confirm `codex exec` plus `codex exec resume` return stable thread ids that can back a true host-managed session ledger


## 7. Recommended Next Tasks

Priority order for the next work:

1. Validate and harden the guarded `claude_exec` backend in an official-ready Claude environment
   Goal: preserve the explicit mailbox/result/telemetry contracts while making Claude-parity host execution authentic without treating third-party relay configurations as official Claude-native sessions.
2. Upgrade the current lead-facing interaction plus preview-capable plan approval workflow into a richer embedded in-run control surface
   Goal: build past live snapshots, file-backed commands, the terminal lead console, and the embedded stdin approval prompt toward a closer Claude-style interaction model.
3. Add true event-level state replay
   Goal: move rewind/replay from checkpoint restoration plus event mapping toward stronger state reconstruction guarantees.


## 8. Proposed Next Milestones

### M5: Workflow-Driven Lead Scheduling

Status: `Completed on 2026-03-08`

Completed outcomes:

- Engine no longer hard-codes `lead_adjudication` and `lead_re_adjudication`
- Workflow pack now declares lead-managed task order
- Added regression tests and reran smoke/verify checks

Validation summary:

- `36/36` tests passed
- fast smoke run passed
- tmux smoke run passed
- both verifier runs passed

### M6: Handler Module Split

Status: `Completed on 2026-03-08`

Completed outcomes:

- Split Markdown workflow logic into:
  - shared helpers
  - analysis handlers
  - challenge/evidence handlers
  - reporting handlers
- Reduced [markdown_audit_handlers.py](/Users/zouxiaoyi/Desktop/project/学习总结/agent_team_demo/agent_team/workflows/markdown_audit_handlers.py) to a small aggregation layer
- Kept all existing runtime imports stable

Validation summary:

- `36/36` tests passed
- fast smoke run passed
- verifier passed

### M7: Workflow Contract Expansion

Status: `Completed on 2026-03-08`

Completed outcomes:

- Extended workflow packs beyond just `build_tasks` + `build_handlers`
- Introduced workflow runtime metadata for lead task order and report task ids
- Updated engine to resolve workflow pack objects directly

Validation summary:

- `37/37` tests passed
- fast smoke run passed
- verifier passed

### M8: Claude Parity Work

Status: `In progress`

Completed slice:

- Added structured tmux worker diagnostics artifact and fallback metadata logging
- Improved visibility into tmux-unavailable and tmux-error fallback paths
- Added tmux session timeout/cleanup lifecycle metadata and IPC cleanup
- Added structured subprocess timeout recovery for direct and fallback execution paths
- Added tmux duplicate-session spawn retry recovery and retry diagnostics
- Added tmux stale-session cleanup attempts and cleanup diagnostics for duplicate-session recovery
- Added tmux stale-session recovery retry and verification diagnostics for failed cleanup paths
- Added tmux active-session cleanup recovery retry and verification diagnostics for worker session cleanup
- Added tmux orphan-session preflight cleanup and diagnostics for interruption recovery
- Added stable preferred session naming, same-name recovery retry, and exact-name orphan cleanup for stronger interruption recovery
- Added preferred-session reuse via `respawn-pane` so exact existing worker sessions can recover without forced cleanup first
- Added preferred-session lease hints so tmux workers can retain reusable sessions when more analyst work is pending, plus an end-of-run cleanup sweep to reconcile retained sessions
- Added persisted cleanup sweep artifact so retained-session reconciliation is visible outside `events.jsonl` and `shared_state.json`
- Added explicit lease-authorized reuse semantics and persisted `tmux_session_leases.json` state so exact-session reuse is driven by runtime state instead of implicit session discovery
- Added resume-aware lease recovery so tmux runs can reconcile persisted retained-session state before dispatch and persist recovery summaries as first-class artifacts
- Hardened verifier expectations so tmux-mode artifact integrity now includes diagnostics, recovery, cleanup, and lease outputs
- Added deferred cleanup semantics for intentional pause-for-resume runs so retained tmux sessions survive until resume instead of being swept at pause time
- Added persisted cleanup/recovery history artifacts so pause/resume cycles keep audit-visible sweep history instead of overwriting prior summaries
- Added checkpoint-backed runtime-config inheritance so resumed runs keep prior wait/rounding/tmux behavior instead of silently drifting back to CLI defaults
- Added task-scoped shared-state views so teammate execution receives explicit bounded context instead of the full shared-state snapshot
- Added `task_context_prepared` events plus `context_boundaries.json` for per-agent/task visibility auditing
- Added a durable teammate session ledger so every agent now has a persistent session id, transport, recent tasks, recent messages, and provider memory snapshot in runtime artifacts
- Added `host_enforcement.json` so advertised host capabilities are separated from runtime-active enforcement decisions
- Added `session_boundaries.json` and final-report summaries so host/session isolation posture is explicit instead of implicit in host metadata and transport internals
- Added reviewer `llm_synthesis` subprocess isolation with provider reconstruction and shared-state-compatible output
- Added an executable `host` teammate mode with a distinct host transport path, explicit external session-worker subprocess boundaries for the current backend, and `host_session_backend=external_process` artifact reporting so transport-backed host runs are not mislabeled as true host-native sessions
- Verified subprocess and host-mode smoke runs plus artifact validation

Remaining focus:

- true external host-backed sessions on top of the executable host transport skeleton
- backend authenticity beyond the current `external_process` session-worker implementation
- lead-facing team interaction and plan approval
- true event-level replay


### M9: Second Workflow Pack

Status: `Completed on 2026-03-08`

Completed outcomes:

- Added `repo-audit` as the second built-in workflow pack
- Reused the same engine, lead scheduling metadata, challenge/evidence loop, verifier, and report artifact contract
- Added tmux/subprocess analyst support for the new workflow's analyst task types
- Added end-to-end coverage for both in-process and tmux-compatible repo-audit runs

Validation summary:

- `47/47` tests passed
- `repo-audit` smoke run passed
- `repo-audit + tmux` smoke run passed
- both verifier runs passed

## 9. Resume Checklist

When resuming work later, start here:

1. Read this file: [RUNTIME_ROADMAP.md](/Users/zouxiaoyi/Desktop/project/学习总结/agent_team_demo/RUNTIME_ROADMAP.md)
2. Re-read current parity snapshot: [PARITY.md](/Users/zouxiaoyi/Desktop/project/学习总结/agent_team_demo/PARITY.md)
3. Inspect current runtime split:
   - [agent_team_runtime.py](/Users/zouxiaoyi/Desktop/project/学习总结/agent_team_demo/agent_team_runtime.py)
   - [engine.py](/Users/zouxiaoyi/Desktop/project/学习总结/agent_team_demo/agent_team/runtime/engine.py)
   - [markdown_audit_handlers.py](/Users/zouxiaoyi/Desktop/project/学习总结/agent_team_demo/agent_team/workflows/markdown_audit_handlers.py)
4. Run full validation:

```bash
python3 -m unittest discover -s agent_team_demo/tests -v
python3 agent_team_demo/skills/agent-team-runtime/scripts/run_runtime.py --preset fast --target agent_team_demo --output agent_team_demo/output_resume_check
python3 agent_team_demo/skills/agent-team-runtime/scripts/verify_run.py --output agent_team_demo/output_resume_check
```

5. Continue from `M8: Claude Parity Work`

## 10. Notes for Future Changes

- Do not remove compatibility symbols from [agent_team_runtime.py](/Users/zouxiaoyi/Desktop/project/学习总结/agent_team_demo/agent_team_runtime.py) unless tests are updated at the same time.
- Tests rely on direct access to:
  - `handle_*` functions
  - `TaskBoard`, `Mailbox`, `CHECKPOINT_FILENAME`, and similar exports
  - tmux wrapper helpers such as `_execute_worker_tmux`
- Prefer non-behavioral refactors first, then feature changes.
- After each structural change, always run:
  - unit tests
  - one smoke run
  - artifact verifier
