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
| Provider abstraction | Implemented | `heuristic` plus OpenAI-compatible endpoint with strict/fallback modes. |
| Host/model/workflow config separation | Implemented | Host, model, team, workflow, and policy config are separated under `agent_team/config.py`, and host adapters now produce per-agent context/workspace session artifacts. |
| Multiple workflow packs on one runtime | Implemented | Built-in packs now include `markdown-audit` and `repo-audit`, both running on the same engine, verifier, and transport layers. |
| Teammate mode `in-process` | Implemented | Python threads in one process. |
| Teammate mode `tmux` | Partial | Analyst tasks, reviewer tasks, and lead adjudication tasks now delegate through the same external worker transport with timeout controls, subprocess fallback (tmux-unavailable + tmux-error fallback), structured `tmux_worker_diagnostics.jsonl` output, explicit timeout/cleanup lifecycle metadata, IPC cleanup after worker execution, structured recovery when subprocess fallback/direct execution times out, duplicate-session spawn retry recovery with structured retry metadata, stale-session cleanup attempts during duplicate-session recovery, stale-session recovery retry when cleanup initially fails, active worker-session cleanup recovery retry when `kill-session` fails, orphan-session preflight cleanup before spawning new worker sessions, stable preferred session naming with retry/fallback metadata, preferred-session reuse via `respawn-pane` for interruption recovery when an exact worker session already exists, explicit lease-authorized preferred-session reuse, resume-aware lease recovery sweeps for lead plus teammate workers, deferred cleanup for intentional pause/resume checkpoints, cross-task preferred-session lease hints, persisted cleanup/recovery/lease artifacts, a mailbox bridge for debate/evidence coordination, worker-event replay into the main event log, and explicit degraded-mode reporting in `run_summary.json` when requested tmux execution falls back. Final report generation and the rest of lead orchestration are still in-process, and true full-session isolation is not complete. |
| Dynamic task creation during run | Implemented | `dynamic_planning` can insert follow-up tasks and gate downstream dependencies at runtime. |
| Resume/rewind state restoration | Partial | Checkpoint-based resume, checkpoint-backed runtime-config inheritance on resume, history-index rewind, event-index rewind mapping, branch-output rewind with seeded event lineage, and replay reports are implemented (`checkpoint_replay.md` + `event_replay.md`); true event-level runtime state restoration is not. |
| Real independent LLM teammate sessions | Partial | Optional provider-backed teammate replies with per-agent local memory are available, host adapters now prepare per-agent workspaces/context files, reviewer challenge/evidence tasks can execute in external workers via mailbox bridge, but true end-to-end isolated teammate sessions are not complete yet. |

## Near-Term Focus

1. Move final report generation and remaining lead orchestration paths onto the same session bridge when isolation is required.
2. Add a first-class lead/teammate session abstraction instead of task-level externalization only.
3. Add true event-level state replay (not only event-index-to-checkpoint mapping).
