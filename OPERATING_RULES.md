# Operating Rules

## Purpose

These are the project execution rules that should stay true even when work continues in another tool or another chat.

## Hard Rules

1. Commit after every completed round.
   Do not leave validated work uncommitted.
2. Run an explicit review after every 3 small iterations.
   The review must check for drift, not just summarize progress.
3. Prefer execution-isolation work over new artifacts.
   The project already has strong observability; the main remaining value is better transport behavior.
4. Do not revert unrelated user changes.
   Work with the current tree unless a destructive change was explicitly requested.
5. Do not commit `.codex_tmp/`.
   It is local smoke/verifier output only.
6. Preserve compatibility unless intentionally migrating it.
   `agent_team_runtime.py` is still a compatibility surface for tests and scripts.

## Documentation Rules

Update the docs when behavior changes:
- `WORKLOG.md`: what changed this round
- `ACTIVE_PLAN.md`: what should happen next
- `README.md`: user-facing behavior and architecture summary
- `PARITY.md`: capability status against the target
- `RUNTIME_ROADMAP.md`: progress board / longer-range plan

If a round is docs-only, still log it in `WORKLOG.md` and commit it.

## Validation Rules

### For runtime / transport / workflow changes
Run all of these unless there is a strong reason not to:

```powershell
python -m unittest discover -s tests -p 'test_*.py' -q
```

```powershell
python agent_team_runtime.py --target . --output .codex_tmp\smoke --provider heuristic --teammate-mode subprocess --peer-wait-seconds 1 --evidence-wait-seconds 1
```

```powershell
python skills\agent-team-runtime\scripts\verify_run.py --output .codex_tmp\smoke
```

### For docs-only changes
At minimum:
- confirm the changed docs are internally consistent
- confirm `git status` only shows intended doc changes

### For tmux-specific behavior
If the environment supports it, add a tmux-mode smoke or targeted test coverage.
Do not fake tmux validation if tmux is unavailable.

## Priority Guardrails

The next high-value work is:
1. subprocess-safe reviewer `llm_synthesis`
2. real host-native transport skeleton
3. only then reconsider harder mailbox-driven reviewer isolation
4. event-level replay after transport work stabilizes

If a proposed round does not help one of those tracks, stop and reassess before implementing it.

## Handoff Rules

When pausing work, leave the repo in a state where someone else can continue without chat history:
- clean worktree after commit
- updated `WORKLOG.md`
- updated `ACTIVE_PLAN.md` if direction changed
- mention exact validation performed in the final summary
- if something could not be run, state that explicitly

## Current Truth Sources

Use these docs in this order when reconstructing context:
1. `PROJECT_HANDOFF.md`
2. `ACTIVE_PLAN.md`
3. `WORKLOG.md`
4. `README.md`
5. `PARITY.md`
6. `RUNTIME_ROADMAP.md`
7. `VALIDATION.md`

Treat `agent_team_implementation_plan.md` as archival context, not the active source of truth.

