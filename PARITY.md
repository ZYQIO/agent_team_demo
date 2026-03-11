# Agent Team Parity Snapshot

This file tracks practical parity against Claude Code Agent Teams as of 2026-03-10.

## Legend

- `Implemented`: available in this repository now.
- `Partial`: available but simplified compared to Claude Code behavior.
- `Gap`: not implemented yet.

## Capability Matrix

| Capability | Status | Notes |
|---|---|---|
| Lead + teammate model | Implemented | `lead` plus three teammate roles run concurrently. |
| Shared task board with dependencies | Implemented | `pending/blocked/in_progress/completed/failed` lifecycle. |
| Task claim gating by role type | Implemented | `Task.allowed_agent_types` supports Task(agent_type)-style restriction. |
| Inter-agent mailbox | Implemented | Pull-based inbox with targeted and broadcast messaging; runtime runs now use an output-scoped file-backed mailbox backend, file-backed pulls atomically claim message files, and worker/helper sessions can consume transport-local mailbox views without relying on one shared mailbox object. |
| Lead-facing team interaction surface | Partial | Runtime now emits `lead_interaction.json` + `lead_interaction.md` with centralized lead-visible mailbox traffic and pending teammate plan approvals, and can consume live file-backed lead commands from `lead_commands.jsonl` while waiting on approval, but it still lacks a richer in-run interactive UI. |
| Peer challenge loop | Implemented | Round1/round2/optional round3 with configurable wait and thresholds. |
| Lead adjudication + re-adjudication | Implemented | Initial verdict plus evidence bonus and final verdict. |
| Hook events for multi-agent workflows | Implemented | Emits `TeammateIdle` and `TaskCompleted` in `events.jsonl`. |
| Lead/team progress visibility | Implemented | Runtime now emits `team_progress.json` + `team_progress.md` and appends a per-agent summary into `final_report.md`. |
| Clear teammate context boundaries | Partial | Runtime now prepares task-scoped context views and emits `context_boundaries.json`, but handlers still share one in-process runtime and isolation is policy-based rather than process-hard. |
| Explicit session boundary posture | Implemented | Runtime now emits `host_enforcement.json` plus `session_boundaries.json`, and host-mode runs derive `host_native_session` / `host_native_workspace` boundaries from an executable `host` transport path instead of metadata-only classification. |
| Provider abstraction | Implemented | `heuristic` plus OpenAI-compatible endpoint with strict/fallback modes. |
| Host/model/workflow config separation | Implemented | Host, model, team, workflow, and policy config are now separated under `agent_team/config.py`. |
| Multiple workflow packs on one runtime | Implemented | Built-in packs now include `markdown-audit` and `repo-audit`, both running on the same engine, verifier, and transport layers. |
| Teammate mode `in-process` | Implemented | Python threads in one process. |
| Teammate mode `subprocess` | Partial | Analyst tasks plus reviewer planning/report/llm tasks (`dynamic_planning`, `repo_dynamic_planning`, `llm_synthesis`, `recommendation_pack`, `repo_recommendation_pack`) can now run as dedicated worker subprocesses with session-scoped workdir/home/target snapshots and `worker_subprocess_session` boundaries; mailbox-driven reviewer tasks (`peer_challenge`, `evidence_pack`) are explicitly guarded to stay on the parent mailbox path until mailbox transport exists. |
| Teammate mode `tmux` | Partial | Analyst task execution supports `tmux` transport with timeout controls, subprocess fallback (tmux-unavailable + tmux-error fallback), structured `tmux_worker_diagnostics.jsonl` output, explicit timeout/cleanup lifecycle metadata, IPC cleanup after worker execution, structured recovery when subprocess fallback/direct execution times out, duplicate-session spawn retry recovery with structured retry metadata, stale-session cleanup attempts during duplicate-session recovery, stale-session recovery retry when cleanup initially fails, active worker-session cleanup recovery retry when `kill-session` fails, orphan-session preflight cleanup before spawning new worker sessions, stable preferred session naming with retry/fallback metadata, preferred-session reuse via `respawn-pane` for interruption recovery when an exact worker session already exists, explicit lease-authorized preferred-session reuse, session-scoped worker temp/workspace directories, session-local source snapshots for tmux worker `target_dir` reads, session-local `cwd/home/cache` isolation for tmux worker execution, retained-session workspace continuity across recovery/resume, resume-aware lease recovery sweeps, deferred cleanup for intentional pause/resume checkpoints, cross-task preferred-session lease hints for likely future analyst work, end-of-run preferred-session cleanup sweep metadata, and persisted `tmux_session_recovery_summary.json`, `tmux_session_cleanup_summary.json`, `tmux_session_recovery_history.jsonl`, `tmux_session_cleanup_history.jsonl`, plus `tmux_session_leases.json` artifacts for retained-session reconciliation. |
| Teammate mode `host` | Partial | `--teammate-mode host` now executes teammate tasks through a distinct host transport path, records `host_native_session` / `host_native_workspace` boundaries, emits `claude-code:<agent>` transport session names plus `host://...` workspace roots, gives host/helper contexts transport-local mailbox views, launches external session-worker subprocesses for the built-in workflow teammate task paths, dispatches analyst scans/follow-ups plus reviewer mailbox/planning/report/llm tasks through explicit `session_task_assignment` mailbox messages, applies teammate result/state/task completion and task-graph mutations through explicit `session_task_result` mailbox messages on the lead side, and routes host session-ledger updates through explicit `session_telemetry` mailbox messages, but it still does not launch true external host-backed teammate sessions. |
| Dynamic task creation during run | Implemented | `dynamic_planning` can insert follow-up tasks and gate downstream dependencies at runtime. |
| Team plan approval workflow | Partial | When policy `teammate_plan_required` is enabled, teammate-generated task mutations are queued as pending approvals, lead can either resume with `--approve-plan`, `--reject-plan`, or `--approve-all-pending-plans`, or keep the run alive and feed live file-backed commands through `lead_commands.jsonl`; there is not yet a richer in-run interactive UI. |
| Resume/rewind state restoration | Partial | Checkpoint-based resume, checkpoint-backed runtime-config inheritance on resume, history-index rewind, event-index rewind mapping, branch-output rewind with seeded event lineage, and replay reports are implemented (`checkpoint_replay.md` + `event_replay.md`); true event-level runtime state restoration is not. |
| Real independent LLM teammate sessions | Partial | Runtime now maintains a durable per-agent teammate session ledger with stable session ids across resume, transport, recent task history, provider memory, explicit `teammate_session_initialized`/`teammate_session_resumed` lifecycle events, transport-local mailbox views for worker/helper contexts, and host-mode teammate assignment/result/telemetry contracts across external session-worker subprocesses, including lead-applied task-mutation payloads for reviewer planning tasks and lead-applied state updates for analyst tasks; host mode records host-managed boundaries through a distinct transport path, but the backend is still an external subprocess worker rather than a true host-backed teammate session. |

## Direction Check

- No major drift in the core objective: the repository is still aiming at the right Claude Code Agent Teams shape.
- Remaining drift is narrower now: lead-facing interaction and plan approval exist as artifact + resume/CLI + file-backed live-command workflows, but the repo still trails Claude Code on richer in-run interaction and true host-backed teammate sessions.

## Near-Term Focus

1. Replace the host `external_process` session-worker backend with a true host-backed teammate session while preserving the explicit mailbox/result/telemetry contracts.
2. Upgrade lead-facing interaction plus plan approval from file-backed command workflow into a richer in-run control surface.
3. Add true event-level state replay (not only event-index-to-checkpoint mapping).
