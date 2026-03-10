# Project Handoff

## Purpose

Use this file as the fastest restart point when continuing `agent_team_demo` from a new chat, machine, or tool.

## Read Order

1. `README.md`
   What the project is, how to run it, and the high-level architecture.
2. `ACTIVE_PLAN.md`
   What should be worked on next, in priority order.
3. `WORKLOG.md`
   What changed recently, with commit references and validation notes.
4. `OPERATING_RULES.md`
   The execution rules that should be followed every round.
5. `PARITY.md`, `RUNTIME_ROADMAP.md`, `VALIDATION.md`
   Deeper status, backlog context, and validation details.

## Current Snapshot

- Date: 2026-03-10
- Latest runtime checkpoint commit: `ca30bf1`
- Runtime shape: reusable `agent_team` package with CLI compatibility through `agent_team_runtime.py`
- Stable capabilities:
  - task board, mailbox, lead/reviewer flow
  - file-backed runtime mailbox delivery inside output-scoped `_mailbox/` directories
  - transport-local mailbox views for runtime worker/helper sessions with atomic file claims during pull
  - host-mode mailbox reviewer/request-reply flows now use external session-worker subprocesses plus explicit `session_task_assignment`, `session_task_result`, and `session_telemetry` mailbox messages so workflow state and teammate session ledger updates are applied on the lead side
  - dynamic task insertion
  - progress artifacts and session ledgers
  - task-scoped context boundaries
  - checkpoint resume / rewind / replay reports
  - `in-process`, `subprocess`, `tmux`, and `host` teammate modes
- Current subprocess coverage:
  - analyst tasks can run in isolated workers
  - reviewer `dynamic_planning`, `repo_dynamic_planning`, `llm_synthesis`, `recommendation_pack`, and `repo_recommendation_pack` can run in isolated workers
  - reviewer `peer_challenge` and `evidence_pack` stay on the parent mailbox path by design
- Current host coverage:
  - `--teammate-mode host` dispatches teammate work through a distinct host transport path
  - host-mode artifacts record `host_native_session` and `host_native_workspace` posture from execution
  - mailbox-driven reviewer tasks (`peer_challenge`, `evidence_pack`) plus reviewer `llm_synthesis` now reach the reviewer's external session-worker subprocess through explicit `session_task_assignment` mailbox messages, return through explicit `session_task_result` mailbox messages, and update teammate session ledgers through explicit `session_telemetry` mailbox messages
  - teammate auto-replies in the mailbox-driven reviewer flows now also come from external session-worker subprocesses rather than parent-runtime threads
  - host planning/report tasks still execute on the lead-managed inline path, so host execution is only partially externalized

## Main Remaining Gaps

1. Host teammate mode is still not true external host-backed execution.
   Mailbox-driven reviewer/request-reply flows plus reviewer `llm_synthesis` now cross an actual external subprocess boundary, but host planning/report tasks (`dynamic_planning`, reporting) still execute on the lead-managed inline path rather than in independent host sessions.
2. Event/report fidelity for external host workers is still lead-synthesized.
   External session workers now communicate only through mailbox/result/telemetry contracts, so the main `events.jsonl` intentionally replays only the lead-observed portion of worker traffic instead of every worker-local debug event.
3. Lead-facing team interaction and plan approval are still missing as runtime behavior.
   Host metadata models `plan_approval`, but there is no approval gate for teammate task-list mutations and no live team-message surface beyond logs/artifacts.
4. Replay is still checkpoint-based rather than true event-level state replay.

## Recommended Next Step

Expand host transport beyond mailbox/request-reply flows.

Why this is next:
- The mailbox/request-reply boundary is now credible enough to stop treating it as purely design work.
- The next material gap is that host planning/report tasks and most other host execution still run lead-inline.
- It matches the updated active plan after the external session-worker round.

What that likely requires:
- decide which remaining host planning/report tasks should move first off the lead-inline path
- keep the existing external session-worker contract explicit instead of reintroducing shared in-process state
- improve event/report surfacing only where needed to describe real external execution, not to add artifact-only detail
- extend tests and smoke coverage for any additional externalized host tasks
- after host execution is broader than mailbox/request-reply flows, move to lead-facing interaction and plan approval before replay-first work

## Fast Validation Commands

Use these from repo root:

```powershell
python -m unittest discover -s tests -p 'test_*.py' -q
```

```powershell
python agent_team_runtime.py --target . --output .codex_tmp\smoke_output_host_external_session --provider heuristic --host-kind claude-code --teammate-mode host --peer-wait-seconds 1 --evidence-wait-seconds 1
```

```powershell
python skills\agent-team-runtime\scripts\verify_run.py --output .codex_tmp\smoke_output_host_external_session
```

If `python` resolves to a Windows Store alias on a new machine, use `py -3` or the concrete interpreter path instead.

## Update Policy

Whenever a meaningful round completes, update:
- `WORKLOG.md`
- `ACTIVE_PLAN.md`
- `README.md` if user-facing behavior changed
- `PARITY.md` or `RUNTIME_ROADMAP.md` if scope/status moved

