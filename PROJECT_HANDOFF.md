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
- Latest runtime checkpoint commit: `be756fd`
- Runtime shape: reusable `agent_team` package with CLI compatibility through `agent_team_runtime.py`
- Stable capabilities:
  - task board, mailbox, lead/reviewer flow
  - file-backed runtime mailbox delivery inside output-scoped `_mailbox/` directories
  - transport-local mailbox views for runtime worker/helper sessions with atomic file claims during pull
  - host-mode mailbox reviewer tasks now dispatch via explicit `session_task_assignment` mailbox messages onto long-lived teammate session threads instead of running lead-inline
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
  - mailbox-driven reviewer tasks (`peer_challenge`, `evidence_pack`) now reach the reviewer's long-lived host session thread through explicit `session_task_assignment` mailbox messages
  - host execution is still in-process runtime emulation rather than true external host-backed sessions

## Main Remaining Gaps

1. Mailbox-driven reviewer tasks still depend on the parent runtime mailbox.
   `peer_challenge` and `evidence_pack` rely on live request/reply loops against long-lived teammate sessions; the runtime now has a file-backed mailbox backend, transport-local mailbox views, and a host-mode mailbox assignment contract, but result publication and state mutation still do not cross a real external request/reply boundary.
2. Host teammate mode is still not true external host-backed execution.
   The transport path is real, host/helper sessions consume transport-local mailbox views, and mailbox reviewer tasks run from mailbox assignment messages on long-lived session threads, but all of that still happens inside the parent runtime instead of true external host-backed sessions.
3. Lead-facing team interaction and plan approval are still missing as runtime behavior.
   Host metadata models `plan_approval`, but there is no approval gate for teammate task-list mutations and no live team-message surface beyond logs/artifacts.
4. Replay is still checkpoint-based rather than true event-level state replay.

## Recommended Next Step

Finish the mailbox-boundary review for reviewer challenge tasks before attempting more external execution.

Why this is next:
- It protects the project from claiming isolation where there is only parent-runtime mailbox access.
- It is the main design dependency for believable external host-backed teammate sessions.
- It matches the current active plan after the latest direction review.

What that likely requires:
- define how mailbox request/reply traffic can cross process or host boundaries
- build on the new `session_task_assignment` contract so reviewer challenge results, state updates, and task-completion signals can return across an actual external process or host boundary
- decide whether teammate auto-replies stay in long-lived workers or move to a lead-mediated transport contract
- keep `peer_challenge` / `evidence_pack` on the parent mailbox path until that design exists
- extend tests, smoke run, and verifier expectations when the boundary changes
- after transport boundaries are credible, add lead-facing team interaction and plan approval before replay-first work

## Fast Validation Commands

Use these from repo root:

```powershell
python -m unittest discover -s tests -p 'test_*.py' -q
```

```powershell
python agent_team_runtime.py --target . --output .codex_tmp\smoke --provider heuristic --teammate-mode subprocess --peer-wait-seconds 1 --evidence-wait-seconds 1
```

```powershell
python skills\agent-team-runtime\scripts\verify_run.py --output .codex_tmp\smoke
```

If `python` resolves to a Windows Store alias on a new machine, use `py -3` or the concrete interpreter path instead.

## Update Policy

Whenever a meaningful round completes, update:
- `WORKLOG.md`
- `ACTIVE_PLAN.md`
- `README.md` if user-facing behavior changed
- `PARITY.md` or `RUNTIME_ROADMAP.md` if scope/status moved

