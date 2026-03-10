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
- Latest runtime checkpoint commit: `2bc8f1a`
- Runtime shape: reusable `agent_team` package with CLI compatibility through `agent_team_runtime.py`
- Stable capabilities:
  - task board, mailbox, lead/reviewer flow
  - dynamic task insertion
  - progress artifacts and session ledgers
  - task-scoped context boundaries
  - checkpoint resume / rewind / replay reports
  - `in-process`, `subprocess`, and `tmux` teammate modes
- Current subprocess coverage:
  - analyst tasks can run in isolated workers
  - reviewer `dynamic_planning`, `repo_dynamic_planning`, `recommendation_pack`, and `repo_recommendation_pack` can run in isolated workers
  - reviewer `peer_challenge`, `evidence_pack`, and `llm_synthesis` still run in-process

## Main Remaining Gaps

1. `llm_synthesis` is still in-process.
   This is the next best target for execution isolation because it is reviewer-owned but not mailbox-driven.
2. Host-native teammate transport is still descriptive, not executable.
   `host_enforcement.json` and `session_boundaries.json` describe posture, but there is no real host-native transport yet.
3. Replay is still checkpoint-based rather than true event-level state replay.

## Recommended Next Step

Implement subprocess-safe reviewer `llm_synthesis`.

Why this is next:
- It continues the current isolation track instead of changing priorities.
- It reduces the remaining reviewer work still pinned to the parent runtime.
- It is smaller than a full host-native transport but moves the architecture in the right direction.

What that likely requires:
- pass model/provider config into worker payloads
- rebuild the provider inside the worker process
- keep `peer_challenge` / `evidence_pack` in-process for now
- extend tests, smoke run, and verifier expectations as needed

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

