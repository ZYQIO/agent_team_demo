# Active Plan

## Current Objective

Keep moving the project from a well-instrumented local runtime toward a more execution-real Agent Teams implementation.

The priority is no longer new artifacts or visibility layers.
The priority is execution isolation and real teammate transport behavior.

Priority 1 is complete: reviewer `llm_synthesis` runs in isolated worker subprocesses with provider reconstruction and shared-state-compatible output.
Priority 2 is complete: `--teammate-mode host` now routes teammate work through a distinct host transport path and records host-managed session/workspace boundaries from execution.
The next priority is reassessing mailbox-driven reviewer tasks without inventing fake isolation semantics.

Direction review (2026-03-10):
- the last three transport-focused rounds improved execution semantics
- they did not drift into artifact-only work
- the next priority remains mailbox-driven reviewer boundaries, and true external host sessions stay behind that design because both tracks need a believable mailbox transport story

Direction review (2026-03-10, mailbox contract checkpoint):
- the latest three mailbox-boundary rounds still improved execution semantics rather than adding reporting-only layers
- the priority order does not need to change
- the mailbox review scope is now narrower: assignment and result contracts exist, so the next transport step is pushing that contract across a real external host/session boundary

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
Status: In Progress

Scope:
- `peer_challenge`
- `evidence_pack`

Rule:
Only move these if the design preserves mailbox semantics and does not create a fake isolation story.

Current findings:
- the worker payload path is single-shot and only receives a shared-state snapshot plus task payload
- `peer_challenge` and `evidence_pack` depend on live request/reply mailbox loops against long-lived teammate sessions
- subprocess mode now has an explicit guardrail that keeps these task types on the parent mailbox path until a real mailbox transport exists
- runtime runs now use a file-backed mailbox backend under the output directory, so `send` / `pull` / `pull_matching` semantics no longer depend on one in-memory mailbox instance
- file-backed mailbox pulls now atomically claim message files, and runtime worker/helper contexts consume transport-local mailbox views instead of only the lead mailbox object
- in host mode, mailbox-driven reviewer tasks now dispatch through an explicit `session_task_assignment` mailbox message onto the long-lived teammate session thread instead of executing lead-inline
- in host mode, mailbox-driven reviewer task results now return through an explicit `session_task_result` mailbox message, and lead-side result application now owns shared-state updates plus task completion/failure instead of the worker mutating the workflow state directly
- the remaining gap is no longer mailbox contract shape; it is that both sides of the contract still run inside one parent runtime, and session lifecycle/ledger state is still shared in-process rather than crossing a true external host/session boundary

Acceptance criteria:
- decide whether these tasks stay in-process, move onto an IPC-backed mailbox transport, or wait for true host-native sessions
- document the mailbox contract required for external execution and keep both assignment and result paths explicit
- keep regression coverage that prevents accidental subprocess offload before that contract exists

### 4. Expand host transport toward true external teammate sessions
Status: Pending

Why:
- host mode still executes handler logic inside the parent runtime
- believable external host sessions need the mailbox boundary from priority 3 first

Acceptance criteria:
- teammate execution is no longer parent-inline bookkeeping only
- mailbox-dependent reviewer flows have a real cross-boundary request/reply path
- artifacts describe real execution, not just posture

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

