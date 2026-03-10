# Active Plan

## Current Objective

Keep moving the project from a well-instrumented local runtime toward a more execution-real Agent Teams implementation.

The priority is no longer new artifacts or visibility layers.
The priority is execution isolation and real teammate transport behavior.

## Priority Order

### 1. Move reviewer `llm_synthesis` into isolated worker execution
Status: Next

Why:
- it is the largest remaining reviewer task still running in-process
- it continues the current transport direction cleanly
- it is smaller and lower risk than jumping directly to full host-native transport

Expected implementation shape:
- pass model config to worker payloads
- rebuild provider inside worker subprocess
- keep output shape compatible with current `llm_synthesis` shared-state contract
- preserve final report generation behavior

Acceptance criteria:
- targeted tests for worker payload and reviewer history
- full suite green
- real subprocess smoke green
- verifier green

### 2. Build a real host-native teammate transport skeleton
Status: Pending

Why:
- current host integration is still posture / classification, not transport execution
- this is the biggest remaining parity gap versus Claude Code Agent Teams

Expected implementation shape:
- add a transport abstraction that is distinct from `tmux` and `subprocess`
- make `host` mode executable, not just descriptive
- route session boundary artifacts from real host transport events

Acceptance criteria:
- `--teammate-mode host` or equivalent actually changes execution path
- session boundaries identify host-native runtime behavior from execution, not from metadata only

### 3. Reassess reviewer mailbox tasks for isolation boundaries
Status: Pending

Scope:
- `peer_challenge`
- `evidence_pack`

Rule:
Only move these if the design preserves mailbox semantics and does not create a fake isolation story.

### 4. Add true event-level replay
Status: Pending

Why:
- current replay is still checkpoint-backed
- this is valuable, but should not preempt transport work

## What Not To Do Next

Do not spend the next round on:
- new summary artifacts
- more report append sections
- more status dashboards
- cosmetic refactors without transport impact

## Review Cadence

Perform an explicit direction review after every 3 small iterations.
That review should answer:
- did the last three rounds improve execution semantics?
- did they accidentally drift into artifact-only work?
- should the priority order above change?

When a review happens, update this file and add a matching note to `WORKLOG.md`.

## Minimum Update Set Per Round

For each meaningful round:
- update `WORKLOG.md`
- update this file if priorities changed or a planned item moved forward
- update `README.md` / `PARITY.md` / `RUNTIME_ROADMAP.md` if external behavior or status changed
- commit the round immediately after validation

