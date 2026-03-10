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
- Latest runtime checkpoint commit: `423e445`
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
  - built-in workflow teammate task paths now reach external session-worker subprocesses through explicit `session_task_assignment` mailbox messages, including analyst scans/follow-ups plus mailbox-driven reviewer tasks (`peer_challenge`, `evidence_pack`) and reviewer planning/report/llm tasks (`dynamic_planning`, `repo_dynamic_planning`, `llm_synthesis`, `recommendation_pack`, `repo_recommendation_pack`)
  - those external workers now return through explicit `session_task_result` mailbox messages and update teammate session ledgers through explicit `session_telemetry` mailbox messages
  - reviewer planning results now carry explicit task-mutation payloads so the lead side inserts follow-up tasks and dependencies instead of external workers mutating the board directly
  - analyst worker payloads now return lead-applied `state_updates`, and assigned host tasks still emit lead-side `task_context_prepared` events so `context_boundaries.json` remains accurate under full teammate offload
  - teammate auto-replies in the mailbox-driven reviewer flows now also come from external session-worker subprocesses rather than parent-runtime threads
  - built-in workflow teammate coverage is now fully off the lead-inline executor, but the backend is still an `external_process` session worker rather than a true host-backed teammate session

## Main Remaining Gaps

1. Host teammate mode is still not true external host-backed execution.
   Built-in workflow teammate task paths now cross an actual external subprocess boundary, but the backend is still an `external_process` session worker rather than a true host-managed session.
2. Event/report fidelity for external host workers is still lead-synthesized.
   External session workers now communicate only through mailbox/result/telemetry contracts, so the main `events.jsonl` intentionally replays only the lead-observed portion of worker traffic instead of every worker-local debug event.
3. Lead-facing team interaction and plan approval are still missing as runtime behavior.
   Host metadata models `plan_approval`, but there is no approval gate for teammate task-list mutations and no live team-message surface beyond logs/artifacts.
4. Replay is still checkpoint-based rather than true event-level state replay.

## Recommended Next Step

Replace the host session-worker subprocess backend with a true host-backed teammate session.

Why this is next:
- The mailbox/request-reply boundary is now credible enough to stop treating it as purely design work.
- Built-in workflow teammate task paths now all cross explicit assignment/result/telemetry contracts.
- The next material gap is backend authenticity rather than task-path coverage.
- It matches the updated active plan after the external session-worker round.

What that likely requires:
- replace the `external_process` worker backend without weakening the existing explicit mailbox/result/telemetry contracts
- keep the existing external session-worker contract explicit instead of reintroducing shared in-process state
- improve event/report surfacing only where needed to describe real external execution, not to add artifact-only detail
- extend tests and smoke coverage so a true host-backed backend can be distinguished from the current `external_process` session worker
- after backend authenticity is improved, move to lead-facing interaction and plan approval before replay-first work

## Fast Validation Commands

Use these from repo root:

```powershell
python -m unittest discover -s tests -p 'test_*.py' -q
```

```powershell
python agent_team_runtime.py --target . --output .codex_tmp\smoke_output_host_analyst_session --provider heuristic --host-kind claude-code --teammate-mode host --peer-wait-seconds 1 --evidence-wait-seconds 1
```

```powershell
python skills\agent-team-runtime\scripts\verify_run.py --output .codex_tmp\smoke_output_host_analyst_session
```

If `python` resolves to a Windows Store alias on a new machine, use `py -3` or the concrete interpreter path instead.

## Update Policy

Whenever a meaningful round completes, update:
- `WORKLOG.md`
- `ACTIVE_PLAN.md`
- `README.md` if user-facing behavior changed
- `PARITY.md` or `RUNTIME_ROADMAP.md` if scope/status moved

