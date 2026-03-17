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

- Date: 2026-03-17
- Latest runtime checkpoint commit: see the most recent `WORKLOG.md` entry
- Runtime shape: reusable `agent_team` package with CLI compatibility through `agent_team_runtime.py`
- Product target: a Codex-usable agent team runtime that benchmarks itself against Claude Code Agent Teams-style functionality rather than trying to clone Claude internals exactly
- Stable capabilities:
  - task board, mailbox, lead/reviewer flow
  - file-backed runtime mailbox delivery inside output-scoped `_mailbox/` directories
  - transport-local mailbox views for runtime worker/helper sessions with atomic file claims during pull
  - host-mode mailbox reviewer/request-reply flows now use external session-worker subprocesses plus explicit `session_task_assignment`, `session_task_result`, and `session_telemetry` mailbox messages so workflow state and teammate session ledger updates are applied on the lead side
  - dynamic task insertion
  - progress artifacts and session ledgers
  - live lead interaction snapshots plus preview-capable teammate plan approval through resume CLI, file-backed commands, `lead_console.py`, and embedded stdin prompt, with teammate session summaries, `show <task_id>` detail inspection, teammate detail inspection, live teammate status/plan requests (`status <agent>`, `plan <agent>`), and teammate-scoped plan approval commands in the live console / command surfaces
  - task-scoped context boundaries
  - checkpoint resume / rewind / replay reports
  - `in-process`, `subprocess`, `tmux`, and `host` teammate modes
- Current subprocess coverage:
  - analyst tasks can run in isolated workers
  - reviewer `dynamic_planning`, `repo_dynamic_planning`, `llm_synthesis`, `recommendation_pack`, and `repo_recommendation_pack` can run in isolated workers
  - reviewer `peer_challenge` and `evidence_pack` stay on the parent mailbox path by design
- Current host coverage:
  - `--teammate-mode host` dispatches teammate work through a distinct host transport path
  - host-mode artifacts now explicitly record the active `host_session_backend`, so transport-backed host workers are no longer reported as true host-native sessions
  - built-in workflow teammate task paths now reach external session-worker subprocesses through explicit `session_task_assignment` mailbox messages, including analyst scans/follow-ups plus mailbox-driven reviewer tasks (`peer_challenge`, `evidence_pack`) and reviewer planning/report/llm tasks (`dynamic_planning`, `repo_dynamic_planning`, `llm_synthesis`, `recommendation_pack`, `repo_recommendation_pack`)
  - those external workers now return through explicit `session_task_result` mailbox messages and update teammate session ledgers through explicit `session_telemetry` mailbox messages
  - reviewer planning results now carry explicit task-mutation payloads so the lead side inserts follow-up tasks and dependencies instead of external workers mutating the board directly
  - analyst worker payloads now return lead-applied `state_updates`, and assigned host tasks still emit lead-side `task_context_prepared` events so `context_boundaries.json` remains accurate under full teammate offload
  - teammate auto-replies in the mailbox-driven reviewer flows now also come from external session-worker subprocesses rather than parent-runtime threads
  - `host_kind=codex` now uses a true host-backed `codex_exec` session backend with persisted Codex thread ids plus a one-shot `--host-session-task-file` runtime entrypoint behind the existing lead-owned assignment/result/telemetry contracts
  - `host_kind=claude-code` now has a guarded `claude_exec` host-backed session backend, but it only activates when local Claude prerequisites are ready on the canonical relay; this environment still remains on the transport-backed `external_process` worker backend, and host enforcement records the detected relay source/host plus whether the relay is official

## Main Remaining Gaps

1. Lead-facing team interaction is still not a richer embedded in-run surface.
   Plan approval now exists as live snapshots plus CLI/file-backed/terminal/embedded-stdin controls, live control surfaces can inspect one pending request in detail with `show <task_id>`, inspect one teammate in detail with teammate/session commands, see teammate session summaries, request teammate status/plan replies during the run, and approve/reject pending plans by teammate identity, but it still needs a more unified Codex-friendly control loop.
2. Event/report fidelity for external host workers is still lead-synthesized.
   External session workers now communicate only through mailbox/result/telemetry contracts, so the main `events.jsonl` intentionally replays only the lead-observed portion of worker traffic instead of every worker-local debug event.
3. Claude-flavored host backend coverage is still environment-dependent.
   The runtime now has a true host-backed `codex_exec` backend plus a guarded `claude_exec` implementation, and host enforcement exposes local Claude relay/subscription prerequisite state, but `host_kind=claude-code` still uses the transport-backed `external_process` worker path in this environment because official prerequisites are not locally ready.
4. Replay is still checkpoint-based rather than true event-level state replay.

## Recommended Next Step

Keep upgrading lead-facing interaction plus plan approval into a richer embedded in-run control surface.

Why this is next:
- The last rounds improved the Codex-facing runtime experience directly instead of only adding benchmark or artifact language.
- Lead can now see teammate session summaries, inspect a specific teammate in detail, and request status/plan replies during a blocked approval window, so the next high-value step is unifying those pieces into a more embedded control surface instead of adding more one-off command verbs.
- This environment still does not have official-ready Claude prerequisites, so guarded `claude_exec` validation remains useful background work but is not the best practical next slice on this machine.

What that likely requires:
- keep using the live interaction snapshot as the single source for pending approvals, teammate session summaries, and lead-visible team messages
- tighten the terminal/file-backed/embedded-stdin surfaces into something closer to one embedded control loop instead of a pile of adjacent helpers
- avoid slipping back into artifact-only additions or new single-purpose commands that do not improve the overall control surface
- keep the guarded `claude_exec` backend validation queued for the moment an official-ready Claude environment is available

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

