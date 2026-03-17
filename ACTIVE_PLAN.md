# Active Plan

## Current Objective

Keep moving the project from a well-instrumented local runtime toward a more usable Codex-first Agent Teams implementation.

The priority is no longer new artifacts or visibility layers.
The priority is Codex-usable teammate coordination, execution isolation where it matters, and a lead control surface that feels coherent in practice.

Priority 1 is complete: reviewer `llm_synthesis` runs in isolated worker subprocesses with provider reconstruction and shared-state-compatible output.
Priority 2 is complete: `--teammate-mode host` now routes teammate work through a distinct host transport path and records host-managed session/workspace boundaries from execution.
Priority 3 is complete: mailbox-driven reviewer/request-reply flows now cross an actual external session-worker subprocess boundary instead of staying on parent-runtime threads.
Priority 4 is in progress: host mode now has true host-backed `codex_exec` plus guarded `claude_exec` session backends, and the runtime now records local `claude-code` relay/subscription prerequisites in host enforcement, but the parity-critical Claude path in this environment still falls back to the transport-backed `external_process` worker backend because official prerequisites are not locally ready.
Priority 5 is also materially closer now: pending approvals expose proposed task/dependency previews through live artifacts, `lead_console.py`, and the embedded stdin prompt, those control surfaces now also surface teammate session summaries from the live session ledger, both live control surfaces can inspect one pending request in detail with `show <task_id>` and one teammate in detail with teammate/session inspection commands, and live command surfaces can request teammate status/plan replies plus approve or reject pending plans by teammate identity. The newest slices add a combined teammate review view, a pending-teammate review overview, and a direct `review next` jump into the next suggested teammate review, so lead can scan or jump through pending teammates with less manual command choice; the next lead-side gap is richer embedded control rather than missing inspection/action primitives.

Direction reset (2026-03-17, Codex-first target clarification):
- the project goal is not a literal Claude Code runtime clone; it is a Codex-usable agent team runtime that benchmarks itself against Claude Code Agent Teams-style functionality
- Claude-specific host paths are still useful reference implementations, but they are no longer the product-defining priority unless they directly improve the Codex runtime experience
- the practical next slices should favor lead control coherence, teammate usability, and Codex-friendly runtime ergonomics over environment-specific Claude validation work

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

Direction review (2026-03-10, analyst checkpoint):
- the latest three host transport rounds still improved execution semantics and did not regress into artifact-only work
- built-in workflow analyst task paths now also cross explicit external assignment/result/telemetry contracts
- the remaining host gap is no longer task-path coverage; it is replacing the remaining `external_process` fallback with validated official-ready host-backed teammate sessions while preserving the explicit contract model

Official parity check (2026-03-11, against current Claude Code Agent Teams docs):
- the core target is still correct: long-lived teammates, shared task coordination, direct team messaging, and genuinely independent teammate sessions
- the backlog has some drift: replay/rewind depth and workflow-specific debate mechanics are ahead of official parity-critical features
- the clearest under-modeled official features are richer lead-facing teammate interaction and stronger host-native teammate sessions; file-backed live plan approval plus a terminal lead console and embedded stdin prompt now exist, but they are still not a first-class embedded in-run UI

Direction review (2026-03-11, lead console checkpoint):
- the last three parity-focused rounds improved official Agent Teams behavior rather than transport-only depth
- they did not drift into artifact-only work; the latest slice added a real in-run control loop through live snapshots plus a terminal lead console
- the priority order still holds: the remaining gaps are official-ready host-backed teammate validation and a richer embedded lead control surface, not more reporting

Direction review (2026-03-11, embedded lead prompt checkpoint):
- the latest parity slice still improved in-run behavior rather than adding another artifact layer
- the runtime can now stay inside one process and prompt for approval decisions on stdin instead of forcing a separate console or manual file edits
- the remaining lead-side gap is now interaction richness and ergonomics, not basic availability of in-run approval control

Direction review (2026-03-17, Codex host plus approval preview checkpoint):
- the last three rounds improved execution semantics and lead-side runtime behavior rather than artifact-only visibility
- the runtime now has one true host-backed session backend plus readable preview details for pending teammate plans
- the priority order should tilt toward Priority 5 for the next slice unless an official-ready Claude environment becomes available locally; the remaining host gap is now Claude-specific validation/authenticity rather than absence of any host-backed backend

Direction review (2026-03-17, guarded Claude backend checkpoint):
- the latest three rounds still improved execution semantics instead of drifting into artifact-only work
- the runtime now has a guarded `claude_exec` backend, but it only counts as Claude-parity execution when the local relay is canonical and native prerequisites are truly ready
- the priority order should now tilt toward Priority 5 unless an official-ready Claude environment is available for live backend validation

Direction review (2026-03-17, live teammate session summary checkpoint):
- the latest three lead-control rounds still improved official Agent Teams behavior instead of drifting into artifact-only visibility work
- the runtime now gives lead a live picture of teammate state through status/plan requests plus always-visible teammate session summaries in the snapshot, terminal console, and embedded stdin prompt
- there is no meaningful drift from the Claude Code Agent Teams target; in this environment the next practical slice should keep upgrading Priority 5 into a more unified embedded control surface unless an official-ready Claude environment becomes available for backend validation

Direction review (2026-03-17, teammate review checkpoint):
- the latest three Codex-first lead-control rounds still improved the runtime interaction loop instead of drifting into report-only work
- lead can now move from one teammate review entry point to status/plan requests, detailed request inspection, and teammate-scoped approval actions without manually correlating separate surfaces
- there is no target drift: the remaining Priority 5 gap is reducing surface fragmentation further inside the embedded prompt rather than inventing more standalone verbs or Claude-specific environment work

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

### 4. Replace the host session-worker backend with true external host-backed teammate sessions
Status: In Progress

Why:
- built-in workflow teammate task paths now cross an external boundary through explicit assignment/result/telemetry contracts
- the next value is replacing the remaining `external_process` fallback with validated official-ready host-backed teammate sessions, not inventing more in-runtime mailbox ceremony

Recent completed outcomes:
- host assigned-task results now carry explicit task-mutation payloads for reviewer `dynamic_planning` and `repo_dynamic_planning`
- lead-side host result application now owns inserted-task and dependency mutation application instead of letting external workers touch the board directly
- task-context snapshots now include the minimal board/task view needed for host planning workers to compute mutations without a full parent-runtime board object
- host assigned-task coverage now includes worker-payload-backed analyst tasks, so the built-in workflow teammate task paths no longer need the lead-inline executor
- lead-side `task_context_prepared` logging now stays accurate for assigned host tasks, keeping `context_boundaries.json` valid under full teammate offload
- host-mode enforcement and boundary artifacts now record the current `external_process` backend explicitly instead of treating transport-backed host workers as true host-native sessions
- `host_kind=codex` now uses a real host-backed `codex_exec` session backend with persisted Codex thread ids, while preserving the existing lead-owned assignment/result/telemetry contracts through a one-shot host session task entrypoint
- `host_kind=claude-code` host metadata now probes local CLI/relay/subscription prerequisites, rejects third-party relay configurations as non-parity execution, and records that state in `host_enforcement.json`
- `host_kind=claude-code` now also has a guarded `claude_exec` session backend that preserves the existing lead-owned assignment/result/telemetry contracts, but only activates when native Claude prerequisites are ready on the canonical relay

Acceptance criteria:
- built-in workflow teammate task paths remain off the lead-inline executor
- artifacts and event logs continue to describe the real boundary used for each task path
- host task-mutation flows stay on explicit lead-applied contracts instead of regressing to direct shared-state or board mutation from worker contexts
- the remaining Claude host work does not hide whether a task ran through `external_process` versus a true host-backed session such as `codex_exec` or `claude_exec`, and does not erase local prerequisite state such as relay selection, official-relay posture, or subscription availability

### 5. Add lead-facing team interaction and plan approval
Status: In Progress

Why:
- current Claude Code Agent Teams docs emphasize centralized team messages, asking teammates for plans, and approval before teammate task-list changes
- the runtime models host `plan_approval` capability metadata, but not the runtime behavior
- this is closer to official parity than deeper replay work

Recent completed outcomes:
- teammate-generated task mutations can now queue as pending approvals instead of mutating the board immediately when policy `teammate_plan_required` is enabled
- lead can now resolve pending approvals through resume-time CLI controls or live file-backed commands while the run waits
- runtime now refreshes `lead_interaction.json` + `lead_interaction.md` during the run instead of only writing them at shutdown
- added a terminal `lead_console.py` helper so lead can inspect pending approvals and recent team messages, then send approve/reject commands without editing files by hand
- added `--lead-interactive` so the runtime itself can prompt on stdin for approve/reject/pause commands when pending teammate plans block progress
- pending teammate plans now also expose proposed task/dependency previews through the live interaction snapshot, terminal lead console, and embedded stdin prompt before approval is applied
- terminal `lead_console.py` and the embedded stdin prompt now both support `show <task_id>` to inspect one pending request in detail before approval, including result/state-update keys and task/dependency preview lines
- live lead command surfaces can now request teammate status/plan replies with `status <agent>` / `plan <agent>` and `--request-status <agent>` / `--request-plan <agent>`, and those replies are summarized directly in `lead_interaction.json` / `lead_interaction.md`
- the live interaction snapshot, terminal lead console, and embedded stdin prompt now also surface teammate session summaries from the runtime session ledger, so lead can see current teammate state without issuing one-off requests first
- interactive lead surfaces now support teammate detail inspection (`teammate <agent>` / `show teammate <agent>` in the interactive prompts plus `--show-teammate <agent>` in `lead_console.py`) so lead can inspect one teammate's current/last task, activity counters, recent teammate-visible messages, and recent provider topic without leaving the runtime flow
- live command surfaces can now also approve or reject pending plans by teammate identity (`approve teammate <agent>` / `reject teammate <agent>` in interactive flows plus `--approve-teammate <agent>` / `--reject-teammate <agent>` in `lead_console.py` and `send_lead_command.py`), reducing the need to manually map teammate inspection back to task ids
- lead control surfaces now also support a combined teammate review view (`review teammate <agent>` in the embedded prompt plus `--review-teammate <agent>` in `lead_console.py`) that gathers one teammate's session state, recent lead-visible messages, pending approvals, and likely next actions into one Codex-friendly output
- lead control surfaces now also support `review pending` plus `--review-pending` so lead can enumerate pending approvals by teammate before drilling into one review or approval path
- lead control surfaces now also support `review next` plus `--review-next`, backed by `next_pending_review_agent` in the live interaction snapshot, so lead can jump straight to the next suggested teammate review from the pending queue

Acceptance criteria:
- lead can inspect teammate/team messages through a runtime surface, not only post-run artifacts
- teammate plan proposals can be reviewed before task-list mutations are applied
- task mutation policies align with host capability metadata instead of staying descriptive only
- remaining gap: upgrade the current live snapshot + terminal/file-backed + embedded-stdin flow into a richer embedded in-run control surface

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

