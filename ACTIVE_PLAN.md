# Active Plan

## Current Objective

Keep moving the project from a well-instrumented local runtime toward a more execution-real Agent Teams implementation.

The priority is no longer new artifacts or visibility layers.
The priority is execution isolation and real teammate transport behavior.

Priority 1 is complete: reviewer `llm_synthesis` runs in isolated worker subprocesses with provider reconstruction and shared-state-compatible output.
Priority 2 is complete: `--teammate-mode host` now routes teammate work through a distinct host transport path and records host-managed session/workspace boundaries from execution.
Priority 3 is complete: mailbox-driven reviewer/request-reply flows now cross an actual external session-worker subprocess boundary instead of staying on parent-runtime threads.
Priority 4 is in progress: host mode now routes mailbox reviewer flows plus reviewer planning/report/llm tasks through the external session-worker contract, but broader host execution still needs to leave the lead-inline executor.

Direction review (2026-03-10):
- the last three transport-focused rounds improved execution semantics
- they did not drift into artifact-only work
- the next priority remains broadening real host execution without regressing the mailbox contract or drifting into artifact-only work

Direction review (2026-03-10, mailbox contract checkpoint):
- the latest three mailbox-boundary rounds still improved execution semantics rather than adding reporting-only layers
- the priority order does not need to change
- the mailbox review scope is now narrower: assignment and result contracts exist, so the next transport step is pushing that contract across a real external host/session boundary

Direction review (2026-03-10, host session telemetry checkpoint):
- the latest rounds still moved execution semantics forward and did not drift into artifact-only work
- reviewer mailbox workflow state and teammate session-ledger updates now both cross explicit mailbox contracts
- the next priority is no longer defining more in-runtime contracts; it is replacing the in-runtime host session thread with a true external host-backed boundary

Direction review (2026-03-10, reviewer planning checkpoint):
- the latest three host transport rounds improved execution semantics rather than artifact shape
- reviewer mailbox flows, planning, synthesis, and reporting now all cross explicit external assignment/result/telemetry contracts
- the priority order still holds, but the remaining host transport gap is now broader than reviewer work and should shift toward analyst or other non-reviewer host execution before replay-first work

Official parity check (2026-03-10, against current Claude Code Agent Teams docs):
- the core target is still correct: long-lived teammates, shared task coordination, direct team messaging, and genuinely independent teammate sessions
- the backlog has some drift: replay/rewind depth and workflow-specific debate mechanics are ahead of official parity-critical features
- the clearest under-modeled official features are lead-facing teammate interaction and plan approval before teammate-driven task-list changes

## Priority Order

### 1. Move reviewer `llm_synthesis` into isolated worker execution
Status: Completed (2026-03-10)

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
Status: Completed (2026-03-10)

Completed outcomes:
- added a transport abstraction that is distinct from `in-process`, `subprocess`, and `tmux`
- made `--teammate-mode host` executable through a lead-dispatched host transport path
- routed session boundary artifacts from host-mode execution, including `host_native_session` boundaries and `host://<host-kind>/sessions/<session_id>/...` workspace descriptors

Validation evidence:
- targeted host runner tests passed
- full suite green
- real host-mode smoke green
- verifier green

### 3. Reassess reviewer mailbox tasks for isolation boundaries
Status: Completed (2026-03-10)

Scope:
- `peer_challenge`
- `evidence_pack`

Rule:
Only move these if the design preserves mailbox semantics and does not create a fake isolation story.

Completed outcomes:
- kept `peer_challenge` / `evidence_pack` on explicit mailbox contracts instead of silently offloading them through single-shot worker payloads
- launched host-mode teammate session workers as external subprocesses backed by the file-backed mailbox transport
- pushed assignment, result, and session-telemetry contracts across that external boundary while keeping lead-side shared-state/task completion ownership explicit
- preserved regression guardrails so subprocess mode still cannot silently offload mailbox-driven reviewer tasks

Acceptance criteria:
- targeted tests and host-mode CLI coverage proving mailbox reviewer dispatch/completion now record `session_worker_backend=external_process`
- full suite green
- real host smoke green
- verifier green

### 4. Expand host transport beyond the reviewer slice toward true external teammate sessions
Status: In Progress

Why:
- mailbox/request-reply flows plus reviewer planning/report/llm tasks now cross an external boundary, but analyst tasks and other host execution still keep broad portions of runtime work on the lead-inline path
- the next value is broadening real external execution beyond the reviewer slice, not inventing more in-runtime mailbox ceremony

Recent completed outcomes:
- host assigned-task results now carry explicit task-mutation payloads for reviewer `dynamic_planning` and `repo_dynamic_planning`
- lead-side host result application now owns inserted-task and dependency mutation application instead of letting external workers touch the board directly
- task-context snapshots now include the minimal board/task view needed for host planning workers to compute mutations without a full parent-runtime board object

Acceptance criteria:
- at least one additional non-reviewer host task path moves off the lead-inline executor
- artifacts and event logs continue to describe the real boundary used for each task path
- host task-mutation flows stay on explicit lead-applied contracts instead of regressing to direct shared-state or board mutation from worker contexts

### 5. Add lead-facing team interaction and plan approval
Status: Pending

Why:
- current Claude Code Agent Teams docs emphasize centralized team messages, asking teammates for plans, and approval before teammate task-list changes
- the runtime models host `plan_approval` capability metadata, but not the runtime behavior
- this is closer to official parity than deeper replay work

Acceptance criteria:
- lead can inspect teammate/team messages through a runtime surface, not only post-run artifacts
- teammate plan proposals can be reviewed before task-list mutations are applied
- task mutation policies align with host capability metadata instead of staying descriptive only

### 6. Add true event-level replay
Status: Pending

Why:
- current replay is still checkpoint-backed
- this is valuable, but should not preempt transport work or the missing official parity features above

## What Not To Do Next

Do not spend the next round on:
- new summary artifacts
- more report append sections
- more status dashboards
- cosmetic refactors without transport impact
- replay-first work that jumps ahead of plan approval or lead-facing team interaction

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

