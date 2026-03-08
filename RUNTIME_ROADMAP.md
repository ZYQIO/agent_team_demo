# Agent Team Runtime Roadmap

Current status snapshot: `2026-03-08`

This document tracks the working plan, completed refactors, validation status, and the recommended next tasks for `agent_team_demo`.

## 1. Current Objective

The project has already completed the original MVP goal in [agent_team_implementation_plan.md](/Users/zouxiaoyi/Desktop/project/学习总结/agent_team_demo/agent_team_implementation_plan.md): a runnable local lead-plus-teammates workflow with task board, mailbox, file locks, event logs, and final artifacts.

The current objective is different:

- Move from a runnable MVP to a reusable `Agent Team runtime`
- Reduce the size and coupling of [agent_team_runtime.py](/Users/zouxiaoyi/Desktop/project/学习总结/agent_team_demo/agent_team_runtime.py)
- Improve practical parity with Claude Code Agent Teams without breaking current behavior
- Keep CLI, tests, artifacts, and skill scripts stable during the refactor

## 2. Target Architecture

The intended architecture is now:

- `agent_team/core.py`
  Runtime primitives: task board, mailbox, file locks, shared state, event logger
- `agent_team/runtime/`
  Runtime internals: adjudication, persistence, engine
- `agent_team/transports/`
  Teammate execution transports: `in-process`, `tmux`, future host/session transport
- `agent_team/workflows/`
  Workflow packs: task graph and workflow-specific handlers
- `agent_team_runtime.py`
  Compatibility entrypoint, CLI, thin wrappers for tests and scripts

## 3. Progress Board

| Workstream | Status | Notes |
|---|---|---|
| MVP runtime runnable | Completed | Original local lead + teammates flow is working. |
| Baseline validation | Completed | Unit/integration tests, smoke run, verifier all passed before refactor. |
| Runtime rules extraction | Completed | Adjudication and persistence moved out of main file. |
| Transport extraction | Completed | `in-process` and `tmux` logic moved into transport modules. |
| Engine extraction | Completed | Main run loop and runtime context moved into `runtime/engine.py`. |
| Workflow handler extraction | Completed | Markdown-specific `handle_*` logic moved into workflow handler module. |
| Compatibility preservation | Completed | Existing tests still call symbols from `agent_team_runtime.py`. |
| Lead orchestration generalization | Completed | Workflow packs now declare lead task order and engine consumes that metadata. |
| Handler module split | Completed | Markdown workflow handlers are now split into shared, analysis, challenge, and reporting modules. |
| Workflow contract expansion | Completed | Workflow packs now declare runtime metadata beyond task graph and handlers. |
| Workflow plugin maturity | Pending | Only `markdown-audit` is fully implemented. |
| True independent teammate sessions | Pending | Still `Partial` per [PARITY.md](/Users/zouxiaoyi/Desktop/project/学习总结/agent_team_demo/PARITY.md). |

## 4. Completed Work

### Phase A: Runtime Rules Split

Completed:

- Added [adjudication.py](/Users/zouxiaoyi/Desktop/project/学习总结/agent_team_demo/agent_team/runtime/adjudication.py)
- Added [persistence.py](/Users/zouxiaoyi/Desktop/project/学习总结/agent_team_demo/agent_team/runtime/persistence.py)
- Moved:
  - adjudication scoring
  - evidence bonus logic
  - checkpoint read/write
  - rewind/replay helpers
  - artifact writing helpers
- Kept compatibility exports in [agent_team_runtime.py](/Users/zouxiaoyi/Desktop/project/学习总结/agent_team_demo/agent_team_runtime.py)

### Phase B: Transport Split

Completed:

- Added [inprocess.py](/Users/zouxiaoyi/Desktop/project/学习总结/agent_team_demo/agent_team/transports/inprocess.py)
- Added [tmux.py](/Users/zouxiaoyi/Desktop/project/学习总结/agent_team_demo/agent_team/transports/tmux.py)
- Moved:
  - in-process teammate execution logic
  - tmux worker payload helpers
  - subprocess fallback path
  - tmux task execution wrapper
- Preserved wrapper functions in [agent_team_runtime.py](/Users/zouxiaoyi/Desktop/project/学习总结/agent_team_demo/agent_team_runtime.py) because tests monkeypatch them directly

### Phase C: Engine and Workflow Split

Completed:

- Added [engine.py](/Users/zouxiaoyi/Desktop/project/学习总结/agent_team_demo/agent_team/runtime/engine.py)
- Added [markdown_audit_handlers.py](/Users/zouxiaoyi/Desktop/project/学习总结/agent_team_demo/agent_team/workflows/markdown_audit_handlers.py)
- Updated [workflows/__init__.py](/Users/zouxiaoyi/Desktop/project/学习总结/agent_team_demo/agent_team/workflows/__init__.py) to resolve workflow handlers
- Moved:
  - `AgentContext`
  - team helper functions
  - lead run loop
  - workflow-specific `handle_*` functions
- Reduced [agent_team_runtime.py](/Users/zouxiaoyi/Desktop/project/学习总结/agent_team_demo/agent_team_runtime.py) to CLI + wrappers + compatibility exports

### Phase D: Workflow-Driven Lead Scheduling

Completed:

- Extended [workflows/__init__.py](/Users/zouxiaoyi/Desktop/project/学习总结/agent_team_demo/agent_team/workflows/__init__.py) with workflow-declared `lead_task_order`
- Updated [engine.py](/Users/zouxiaoyi/Desktop/project/学习总结/agent_team_demo/agent_team/runtime/engine.py) to execute lead tasks from workflow metadata instead of hard-coded task ids
- Added regression coverage in [test_runtime_logic.py](/Users/zouxiaoyi/Desktop/project/学习总结/agent_team_demo/tests/test_runtime_logic.py)
- Preserved compatibility in [agent_team_runtime.py](/Users/zouxiaoyi/Desktop/project/学习总结/agent_team_demo/agent_team_runtime.py)

### Phase E: Handler Module Split

Completed:

- Added [markdown_audit_shared.py](/Users/zouxiaoyi/Desktop/project/学习总结/agent_team_demo/agent_team/workflows/markdown_audit_shared.py)
- Added [markdown_audit_analysis.py](/Users/zouxiaoyi/Desktop/project/学习总结/agent_team_demo/agent_team/workflows/markdown_audit_analysis.py)
- Added [markdown_audit_challenge.py](/Users/zouxiaoyi/Desktop/project/学习总结/agent_team_demo/agent_team/workflows/markdown_audit_challenge.py)
- Added [markdown_audit_reporting.py](/Users/zouxiaoyi/Desktop/project/学习总结/agent_team_demo/agent_team/workflows/markdown_audit_reporting.py)
- Reduced [markdown_audit_handlers.py](/Users/zouxiaoyi/Desktop/project/学习总结/agent_team_demo/agent_team/workflows/markdown_audit_handlers.py) to a compatibility aggregation layer

### Phase F: Workflow Contract Expansion

Completed:

- Extended [workflows/__init__.py](/Users/zouxiaoyi/Desktop/project/学习总结/agent_team_demo/agent_team/workflows/__init__.py) with `WorkflowRuntimeMetadata`
- Workflow packs now declare:
  - lead task order
  - report task ids
- Updated [engine.py](/Users/zouxiaoyi/Desktop/project/学习总结/agent_team_demo/agent_team/runtime/engine.py) to resolve and consume workflow pack objects directly
- Added regression coverage in [test_runtime_logic.py](/Users/zouxiaoyi/Desktop/project/学习总结/agent_team_demo/tests/test_runtime_logic.py)

## 5. Current File Layout

Key files after the refactor:

- [agent_team_runtime.py](/Users/zouxiaoyi/Desktop/project/学习总结/agent_team_demo/agent_team_runtime.py)
- [agent_team/runtime/adjudication.py](/Users/zouxiaoyi/Desktop/project/学习总结/agent_team_demo/agent_team/runtime/adjudication.py)
- [agent_team/runtime/persistence.py](/Users/zouxiaoyi/Desktop/project/学习总结/agent_team_demo/agent_team/runtime/persistence.py)
- [agent_team/runtime/engine.py](/Users/zouxiaoyi/Desktop/project/学习总结/agent_team_demo/agent_team/runtime/engine.py)
- [agent_team/transports/inprocess.py](/Users/zouxiaoyi/Desktop/project/学习总结/agent_team_demo/agent_team/transports/inprocess.py)
- [agent_team/transports/tmux.py](/Users/zouxiaoyi/Desktop/project/学习总结/agent_team_demo/agent_team/transports/tmux.py)
- [agent_team/workflows/__init__.py](/Users/zouxiaoyi/Desktop/project/学习总结/agent_team_demo/agent_team/workflows/__init__.py)
- [agent_team/workflows/markdown_audit.py](/Users/zouxiaoyi/Desktop/project/学习总结/agent_team_demo/agent_team/workflows/markdown_audit.py)
- [agent_team/workflows/markdown_audit_shared.py](/Users/zouxiaoyi/Desktop/project/学习总结/agent_team_demo/agent_team/workflows/markdown_audit_shared.py)
- [agent_team/workflows/markdown_audit_analysis.py](/Users/zouxiaoyi/Desktop/project/学习总结/agent_team_demo/agent_team/workflows/markdown_audit_analysis.py)
- [agent_team/workflows/markdown_audit_challenge.py](/Users/zouxiaoyi/Desktop/project/学习总结/agent_team_demo/agent_team/workflows/markdown_audit_challenge.py)
- [agent_team/workflows/markdown_audit_reporting.py](/Users/zouxiaoyi/Desktop/project/学习总结/agent_team_demo/agent_team/workflows/markdown_audit_reporting.py)
- [agent_team/workflows/markdown_audit_handlers.py](/Users/zouxiaoyi/Desktop/project/学习总结/agent_team_demo/agent_team/workflows/markdown_audit_handlers.py)

Current size snapshot:

- [agent_team_runtime.py](/Users/zouxiaoyi/Desktop/project/学习总结/agent_team_demo/agent_team_runtime.py): `858` lines
- [engine.py](/Users/zouxiaoyi/Desktop/project/学习总结/agent_team_demo/agent_team/runtime/engine.py): `765` lines
- [workflows/__init__.py](/Users/zouxiaoyi/Desktop/project/学习总结/agent_team_demo/agent_team/workflows/__init__.py): `82` lines
- [markdown_audit_handlers.py](/Users/zouxiaoyi/Desktop/project/学习总结/agent_team_demo/agent_team/workflows/markdown_audit_handlers.py): `44` lines
- [markdown_audit_shared.py](/Users/zouxiaoyi/Desktop/project/学习总结/agent_team_demo/agent_team/workflows/markdown_audit_shared.py): `40` lines
- [markdown_audit_analysis.py](/Users/zouxiaoyi/Desktop/project/学习总结/agent_team_demo/agent_team/workflows/markdown_audit_analysis.py): `179` lines
- [markdown_audit_challenge.py](/Users/zouxiaoyi/Desktop/project/学习总结/agent_team_demo/agent_team/workflows/markdown_audit_challenge.py): `351` lines
- [markdown_audit_reporting.py](/Users/zouxiaoyi/Desktop/project/学习总结/agent_team_demo/agent_team/workflows/markdown_audit_reporting.py): `245` lines

This is already better than a single monolithic runtime file. The next obvious structural hotspot is now [engine.py](/Users/zouxiaoyi/Desktop/project/学习总结/agent_team_demo/agent_team/runtime/engine.py).

## 6. Validation Status

Latest verified on `2026-03-08`.

### Tests

Command:

```bash
python3 -m unittest discover -s agent_team_demo/tests -v
```

Result:

- `37/37` tests passed

### Smoke Runs

Standard smoke run:

```bash
python3 agent_team_demo/skills/agent-team-runtime/scripts/run_runtime.py \
  --preset fast \
  --target agent_team_demo \
  --output agent_team_demo/output_analysis_m7_fast
```

tmux smoke run:

```bash
python3 agent_team_demo/skills/agent-team-runtime/scripts/run_runtime.py \
  --preset tmux \
  --target agent_team_demo \
  --output agent_team_demo/output_analysis_m5_tmux \
  --extra-arg=--peer-wait-seconds \
  --extra-arg=1 \
  --extra-arg=--evidence-wait-seconds \
  --extra-arg=1 \
  --extra-arg=--no-auto-round3-on-challenge
```

Artifact verification:

```bash
python3 agent_team_demo/skills/agent-team-runtime/scripts/verify_run.py \
  --output agent_team_demo/output_analysis_m7_fast

python3 agent_team_demo/skills/agent-team-runtime/scripts/verify_run.py \
  --output agent_team_demo/output_analysis_m5_tmux
```

Verified output directories:

- [output_analysis_m7_fast](/Users/zouxiaoyi/Desktop/project/学习总结/agent_team_demo/output_analysis_m7_fast)
- [output_analysis_m5_tmux](/Users/zouxiaoyi/Desktop/project/学习总结/agent_team_demo/output_analysis_m5_tmux)

## 7. Recommended Next Tasks

Priority order for the next work:

1. Improve tmux parity from `Partial` toward `Implemented`
   Goal: stabilize lifecycle, errors, and diagnostics, then expand coverage beyond the current analyst path.

2. Add a second workflow pack
   Goal: prove the runtime is not tightly coupled to `markdown-audit`.

## 8. Proposed Next Milestones

### M5: Workflow-Driven Lead Scheduling

Status: `Completed on 2026-03-08`

Completed outcomes:

- Engine no longer hard-codes `lead_adjudication` and `lead_re_adjudication`
- Workflow pack now declares lead-managed task order
- Added regression tests and reran smoke/verify checks

Validation summary:

- `36/36` tests passed
- fast smoke run passed
- tmux smoke run passed
- both verifier runs passed

### M6: Handler Module Split

Status: `Completed on 2026-03-08`

Completed outcomes:

- Split Markdown workflow logic into:
  - shared helpers
  - analysis handlers
  - challenge/evidence handlers
  - reporting handlers
- Reduced [markdown_audit_handlers.py](/Users/zouxiaoyi/Desktop/project/学习总结/agent_team_demo/agent_team/workflows/markdown_audit_handlers.py) to a small aggregation layer
- Kept all existing runtime imports stable

Validation summary:

- `36/36` tests passed
- fast smoke run passed
- verifier passed

### M7: Workflow Contract Expansion

Status: `Completed on 2026-03-08`

Completed outcomes:

- Extended workflow packs beyond just `build_tasks` + `build_handlers`
- Introduced workflow runtime metadata for lead task order and report task ids
- Updated engine to resolve workflow pack objects directly

Validation summary:

- `37/37` tests passed
- fast smoke run passed
- verifier passed

### M8: Claude Parity Work

Definition:

- Continue items marked `Partial` in [PARITY.md](/Users/zouxiaoyi/Desktop/project/学习总结/agent_team_demo/PARITY.md)

Focus:

- stronger tmux execution isolation
- better interruption/recovery semantics
- clearer teammate context boundaries

### M9: Second Workflow Pack

Definition:

- Add a second workflow pack to prove the runtime is no longer `markdown-audit` specific

Done when:

- a second workflow pack runs on the same engine
- no engine edits are required for that workflow to run
- tests or smoke coverage exist for both workflow packs

## 9. Resume Checklist

When resuming work later, start here:

1. Read this file: [RUNTIME_ROADMAP.md](/Users/zouxiaoyi/Desktop/project/学习总结/agent_team_demo/RUNTIME_ROADMAP.md)
2. Re-read current parity snapshot: [PARITY.md](/Users/zouxiaoyi/Desktop/project/学习总结/agent_team_demo/PARITY.md)
3. Inspect current runtime split:
   - [agent_team_runtime.py](/Users/zouxiaoyi/Desktop/project/学习总结/agent_team_demo/agent_team_runtime.py)
   - [engine.py](/Users/zouxiaoyi/Desktop/project/学习总结/agent_team_demo/agent_team/runtime/engine.py)
   - [markdown_audit_handlers.py](/Users/zouxiaoyi/Desktop/project/学习总结/agent_team_demo/agent_team/workflows/markdown_audit_handlers.py)
4. Run full validation:

```bash
python3 -m unittest discover -s agent_team_demo/tests -v
python3 agent_team_demo/skills/agent-team-runtime/scripts/run_runtime.py --preset fast --target agent_team_demo --output agent_team_demo/output_resume_check
python3 agent_team_demo/skills/agent-team-runtime/scripts/verify_run.py --output agent_team_demo/output_resume_check
```

5. Continue from `M8: Claude Parity Work`

## 10. Notes for Future Changes

- Do not remove compatibility symbols from [agent_team_runtime.py](/Users/zouxiaoyi/Desktop/project/学习总结/agent_team_demo/agent_team_runtime.py) unless tests are updated at the same time.
- Tests rely on direct access to:
  - `handle_*` functions
  - `TaskBoard`, `Mailbox`, `CHECKPOINT_FILENAME`, and similar exports
  - tmux wrapper helpers such as `_execute_worker_tmux`
- Prefer non-behavioral refactors first, then feature changes.
- After each structural change, always run:
  - unit tests
  - one smoke run
  - artifact verifier
