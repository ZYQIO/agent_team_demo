# Worklog

## How To Use This File

Add one entry per completed round.
Each entry should capture:
- date
- goal
- key changes
- validation performed
- commit hash
- next implication

## Recent History

### 2026-03-10 - Host mailbox reviewer session-thread dispatch
- Goal: stop running host-mode mailbox reviewer tasks lead-inline and move them onto the long-lived teammate session path
- Changes:
  - extended `InProcessTeammateAgent` with assigned-task dispatch so non-claiming host helpers can execute one explicitly assigned task at a time
  - registered host teammate session workers in the runtime engine and changed host transport dispatch so `peer_challenge` and `evidence_pack` run on the reviewer's long-lived session thread
  - preserved inline host execution for non-mailbox tasks and tagged host dispatch events with `execution_mode=inline` vs `execution_mode=session_thread`
  - added regression coverage for serial mailbox-task dispatch on a host session thread and for preserving inline behavior on non-mailbox host tasks
- Validation:
  - targeted host regression tests passed
  - full suite: `101/101` tests passed
  - real CLI host smoke passed: `.codex_tmp\\smoke_output_host_mailbox_task_session`
  - verifier passed for that smoke output
  - smoke event review confirmed `peer_challenge` and `evidence_pack` use `execution_mode=session_thread` while `llm_synthesis` stays `execution_mode=inline`
- Commit: `2cedafa`
- Next implication: the next mailbox-boundary step is a true external request/reply contract for reviewer mailbox flows, not more in-runtime session emulation

### 2026-03-10 - Mailbox transport views for worker/helper sessions
- Goal: make external-looking transport paths actually consume the file-backed mailbox backend instead of only sharing the lead runtime mailbox object
- Changes:
  - hardened file-backed mailbox pulls with atomic file-claim semantics so multiple mailbox instances can safely consume the same `_mailbox/` transport
  - added `Mailbox.transport_view()` and switched runtime worker/helper contexts plus host-dispatched task contexts to use transport-local mailbox views
  - narrowed non-claiming mailbox helpers to request subjects only, so helper loops stop stealing non-request replies that belong to task-owning reviewer flows
  - added regression coverage for shared-storage mailbox views, cross-instance `pull_matching` preservation, non-claiming helper filtering, and host transport mailbox-view usage
- Validation:
  - targeted mailbox/host regression tests passed
  - full suite: `100/100` tests passed
  - real CLI host smoke passed: `.codex_tmp\\smoke_output_host_mailbox_view`
  - verifier passed for that smoke output
  - smoke artifact review confirmed `run_summary.json` still reports `mailbox_model=asynchronous file-backed inbox`
- Commit: `0eb5619`
- Next implication: the next mailbox-boundary step is moving mailbox-driven reviewer handlers off parent-inline execution, not just off a shared mailbox object

### 2026-03-10 - File-backed runtime mailbox backend
- Goal: turn mailbox-boundary work into runtime behavior by making mailbox delivery available beyond a single in-memory mailbox object
- Changes:
  - extended `Mailbox` with an optional file-backed backend that preserves `send`, `broadcast`, `pull`, and `pull_matching` semantics across separate mailbox instances
  - switched runtime runs to use an output-scoped `_mailbox/` directory and surfaced the active mailbox model/storage path in `shared_state.json` and `run_summary.json`
  - added unit coverage for cross-instance mailbox delivery and `pull_matching` behavior, plus end-to-end assertions for mailbox metadata in CLI artifacts
  - updated `ACTIVE_PLAN.md`, `PARITY.md`, `PROJECT_HANDOFF.md`, `README.md`, and `RUNTIME_ROADMAP.md` to reflect that mailbox transport groundwork is now implemented
- Validation:
  - full suite: `98/98` tests passed
  - real CLI subprocess smoke passed: `.codex_tmp/smoke_output_file_mailbox`
  - verifier passed for that smoke output
  - smoke artifact review confirmed `run_summary.json` reports `mailbox_model=asynchronous file-backed inbox`
- Commit: `74f2bda`
- Next implication: the next mailbox-boundary step is wiring external teammate transports to the new file-backed backend instead of only using the parent runtime mailbox object

### 2026-03-10 - Official Claude parity review
- Goal: re-check the final target against the current Claude Code Agent Teams docs and decide whether the project backlog has drifted
- Changes:
  - reviewed the current official Agent Teams docs against local parity assumptions
  - confirmed the core target is still correct: long-lived teammates, shared coordination, direct messaging, and real independent teammate sessions
  - identified backlog drift: replay/rewind depth and workflow-specific debate mechanics are ahead of official parity features like lead-facing team interaction and plan approval
  - updated `ACTIVE_PLAN.md`, `PARITY.md`, `PROJECT_HANDOFF.md`, and `RUNTIME_ROADMAP.md` so plan approval and lead-facing interaction move ahead of replay-first work
- Validation:
  - docs reviewed against current official Claude Code Agent Teams documentation
  - internal doc set checked for priority consistency after the reorder
- Commit: `230c493`
- Next implication: after mailbox/host execution boundaries, the next parity-critical gap is runtime plan approval plus a lead-facing team interaction surface

### 2026-03-10 - Mailbox reviewer boundary review and guardrails
- Goal: perform the first explicit direction review after the recent transport rounds and prevent reviewer mailbox tasks from drifting into fake subprocess isolation
- Changes:
  - reviewed the recent transport work and confirmed it improved execution semantics instead of drifting into artifact-only work
  - codified `peer_challenge` and `evidence_pack` as mailbox-driven reviewer tasks that stay on the parent mailbox path in subprocess mode
  - added regression coverage so mailbox-driven reviewer tasks cannot silently overlap with subprocess reviewer worker tasks
  - updated `ACTIVE_PLAN.md`, `PROJECT_HANDOFF.md`, `README.md`, `PARITY.md`, and `RUNTIME_ROADMAP.md` so the next priority is the mailbox boundary design ahead of true external host sessions
- Validation:
  - full suite: `96/96` tests passed
  - real CLI subprocess smoke passed: `.codex_tmp/smoke_output_mailbox_guardrail`
  - verifier passed for that smoke output
  - smoke artifact review confirmed reviewer `task_history` contains `peer_challenge=in-process`, `evidence_pack=in-process`, and `llm_synthesis=subprocess`
- Commit: `ea1f405`
- Next implication: true external host-backed teammate execution now depends on a real mailbox transport contract instead of direct handler offload

### 2026-03-10 - Host teammate transport skeleton
- Goal: make `host` mode execute through a real transport path instead of only describing host posture in artifacts
- Changes:
  - added `agent_team/transports/host.py` and wired a distinct host transport path into the runtime engine and CLI
  - made `--teammate-mode host` dispatch teammate tasks through host transport bookkeeping instead of the in-process worker path
  - recorded `host_native_session` boundaries, `claude-code:<agent>` transport session names, and `host://claude-code/sessions/<session_id>/...` workspace descriptors from host-mode execution
  - added targeted host runner coverage plus CLI end-to-end assertions for `host` mode artifacts and reviewer history
- Validation:
  - full suite: `95/95` tests passed
  - real CLI host smoke passed: `.codex_tmp/smoke_output_host_mode`
  - verifier passed for that smoke output
  - smoke artifact review confirmed reviewer `task_history` contains `llm_synthesis=host` and `recommendation_pack=host`
- Commit: pending in working tree
- Next implication: the remaining host/session gap is true external host-backed execution, and the next runtime isolation question is mailbox-driven reviewer tasks

### 2026-03-10 - Reviewer llm_synthesis subprocess isolation
- Goal: finish the last large reviewer task still pinned in-process and keep transport work moving instead of adding more artifacts
- Changes:
  - added reviewer `llm_synthesis` to the existing subprocess worker path
  - passed model config into reviewer worker payloads and rebuilt the provider inside the worker subprocess
  - preserved the `llm_synthesis` shared-state contract so downstream `recommendation_pack` behavior stayed unchanged
  - added worker-payload, reviewer-session, and CLI subprocess coverage for the new path
- Validation:
  - full suite: `93/93` tests passed
  - real CLI subprocess smoke passed: `.codex_tmp/smoke_output_llm_subprocess`
  - verifier passed for that smoke output
  - smoke artifact review confirmed reviewer `task_history` contains `llm_synthesis=subprocess`
- Commit: pending in working tree
- Next implication: the next transport priority is host-native teammate execution, while mailbox-driven reviewer tasks remain the main isolation boundary question

### 2026-03-10 - Documentation handoff baseline
- Goal: add durable project handoff material so work can continue from another context without reconstructing state from memory
- Changes:
  - added `PROJECT_HANDOFF.md`
  - added `ACTIVE_PLAN.md`
  - added `OPERATING_RULES.md`
  - added this `WORKLOG.md`
  - linked the new docs from `README.md`
- Validation:
  - repo doc set reviewed against `README.md`, `PARITY.md`, and `RUNTIME_ROADMAP.md`
- Commit: this round should be the latest docs-only checkpoint after commit
- Next implication: future rounds now need to keep these docs in sync instead of relying on chat context

### 2026-03-10 - Reviewer planning/report subprocess isolation
- Goal: extend real execution isolation for reviewer work instead of adding more artifacts
- Changes:
  - reviewer `dynamic_planning` and `repo_dynamic_planning` already ran in subprocess workers
  - extended subprocess offload to reviewer `recommendation_pack` and `repo_recommendation_pack`
  - updated host enforcement wording from analyst-only isolation notes to selected-worker-task isolation notes
- Validation:
  - full suite: `90/90` tests passed
  - real CLI subprocess smoke passed
  - verifier passed
  - smoke artifact review confirmed reviewer history contains `recommendation_pack=subprocess`
- Commit: `2bc8f1a`
- Next implication: `llm_synthesis` is now the clearest remaining reviewer task still pinned in-process

### 2026-03-10 - Subprocess teammate mode for analyst workers
- Goal: create a real execution-isolation path instead of only reporting isolation posture
- Changes:
  - added first-class `subprocess` teammate mode for analyst workers
  - hardened subprocess host-enforcement fallback behavior
  - fixed worker target snapshots to avoid copying `.codex_tmp`
- Validation:
  - full suite passed at that checkpoint
  - real subprocess smoke passed
  - verifier passed
- Commit: `3e9f14f`
- Next implication: reviewer tasks became the next isolation bottleneck

### 2026-03-10 - Host runtime enforcement artifacts
- Goal: separate advertised host capabilities from real runtime behavior
- Changes:
  - added `host_enforcement.json`
  - emitted explicit runtime decisions for session/workspace enforcement
  - updated final report and verifier coverage
- Validation:
  - full suite passed
  - CLI smoke passed
  - verifier passed
- Commit: `3946124`
- Next implication: artifacts were clearer, but real host-native transport still remained missing

### 2026-03-10 - tmux execution isolation chain
- Goal: strengthen tmux worker isolation before broader transport work
- Changes:
  - added tmux session workspaces
  - restored workspace continuity on recovery
  - added session-local target snapshots
  - isolated tmux worker `cwd`, `HOME`, and cache/config roots
- Validation:
  - full suite passed across each checkpoint
  - tmux smoke / verifier paths were exercised during those rounds
- Commits:
  - `594454e`
  - `f2b7e67`
  - `eb7e8c4`
  - `02c1a7b`
- Next implication: tmux isolation became stronger, but subprocess / host paths were still behind

### 2026-03-10 - Session continuity and posture artifacts
- Goal: make teammate state and boundaries explicit before deeper transport work
- Changes:
  - added `context_boundaries.json`
  - added durable teammate session ledger and resume continuity counters
  - added `team_progress.json` / `team_progress.md`
  - added `session_boundaries.json`
- Validation:
  - full suite passed across those rounds
  - smoke and verifier were updated and rerun
- Commits:
  - `7bb016d`
  - `d54cd1d`
  - `3c2a761`
- Next implication: observability became strong enough to shift focus back to execution semantics

## Entry Template

### YYYY-MM-DD - Short Title
- Goal:
- Changes:
- Validation:
- Commit:
- Next implication:

