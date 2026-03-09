# Agent Team Parity Snapshot

This file tracks practical parity against Claude Code Agent Teams as of 2026-03-09.

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
| Inter-agent mailbox | Implemented | Pull-based inbox with targeted and broadcast messaging. |
| Peer challenge loop | Implemented | Round1/round2/optional round3 with configurable wait and thresholds. |
| Lead adjudication + re-adjudication | Implemented | Initial verdict plus evidence bonus and final verdict. |
| Hook events for multi-agent workflows | Implemented | Emits `TeammateIdle` and `TaskCompleted` in `events.jsonl`. |
| Lead/team progress visibility | Implemented | Runtime now emits `team_progress.json` + `team_progress.md` and appends a per-agent summary into `final_report.md`. |
| Clear teammate context boundaries | Partial | Runtime now prepares task-scoped context views and emits `context_boundaries.json`, but handlers still share one in-process runtime and isolation is policy-based rather than process-hard. |
| Explicit session boundary posture | Partial | Runtime now emits `session_boundaries.json` to classify each teammate session as host-native, tmux-backed, or runtime-emulated, but those boundaries are still descriptive unless the host/transport can enforce them. |
| Provider abstraction | Implemented | `heuristic` plus OpenAI-compatible endpoint with strict/fallback modes. |
| Host/model/workflow config separation | Implemented | Host, model, team, workflow, and policy config are now separated under `agent_team/config.py`. |
| Multiple workflow packs on one runtime | Implemented | Built-in packs now include `markdown-audit` and `repo-audit`, both running on the same engine, verifier, and transport layers. |
| Teammate mode `in-process` | Implemented | Python threads in one process. |
| Teammate mode `tmux` | Partial | Analyst task execution supports `tmux` transport with timeout controls, subprocess fallback (tmux-unavailable + tmux-error fallback), structured `tmux_worker_diagnostics.jsonl` output, explicit timeout/cleanup lifecycle metadata, IPC cleanup after worker execution, structured recovery when subprocess fallback/direct execution times out, duplicate-session spawn retry recovery with structured retry metadata, stale-session cleanup attempts during duplicate-session recovery, stale-session recovery retry when cleanup initially fails, active worker-session cleanup recovery retry when `kill-session` fails, orphan-session preflight cleanup before spawning new worker sessions, stable preferred session naming with retry/fallback metadata, preferred-session reuse via `respawn-pane` for interruption recovery when an exact worker session already exists, explicit lease-authorized preferred-session reuse, resume-aware lease recovery sweeps, deferred cleanup for intentional pause/resume checkpoints, cross-task preferred-session lease hints for likely future analyst work, end-of-run preferred-session cleanup sweep metadata, and persisted `tmux_session_recovery_summary.json`, `tmux_session_cleanup_summary.json`, `tmux_session_recovery_history.jsonl`, `tmux_session_cleanup_history.jsonl`, plus `tmux_session_leases.json` artifacts for retained-session reconciliation. |
| Dynamic task creation during run | Implemented | `dynamic_planning` can insert follow-up tasks and gate downstream dependencies at runtime. |
| Resume/rewind state restoration | Partial | Checkpoint-based resume, checkpoint-backed runtime-config inheritance on resume, history-index rewind, event-index rewind mapping, branch-output rewind with seeded event lineage, and replay reports are implemented (`checkpoint_replay.md` + `event_replay.md`); true event-level runtime state restoration is not. |
| Real independent LLM teammate sessions | Partial | Runtime now maintains a durable per-agent teammate session ledger with stable session ids across resume, transport, recent task history, provider memory, and explicit `teammate_session_initialized`/`teammate_session_resumed` lifecycle events, but true isolated process/LLM sessions are not universal yet. |

## Near-Term Focus

1. Stabilize native tmux transport further on hosts with tmux installed, especially around stronger isolation, richer retained-session semantics, and higher-fidelity interruption recovery under real tmux-backed resumes.
2. Expand the new teammate session ledger into stronger host/session transport boundaries instead of only runtime-managed session state.
3. Add true event-level state replay (not only event-index-to-checkpoint mapping).
