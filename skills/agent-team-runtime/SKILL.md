---
name: agent-team-runtime
description: Orchestrate, tune, and validate a local lead-plus-teammates agent workflow powered by `agent_team_demo/agent_team_runtime.py`, including task-board coordination, peer challenge rounds, lead adjudication, evidence pack, and re-adjudication. Use when users ask to run an Agent Team demo, reproduce or force challenge/evidence paths, tune thresholds/weights/wait times, verify output artifacts, or package this capability for reuse across AI coding tools.
---

# Agent Team Runtime

Run the local Agent Team runtime end-to-end with repeatable presets and artifact checks.

Use bundled scripts instead of rebuilding command lines by hand.

## Execute Runtime

1. Resolve repository root and runtime location.
Use `scripts/run_runtime.py` and let it auto-detect `agent_team_demo/agent_team_runtime.py`.

2. Choose a preset based on intent.
- `default`: baseline behavior.
- `static`: disable dynamic task insertion and keep fixed task graph behavior.
- `tmux`: execute analyst tasks via tmux worker transport (with subprocess fallback).
- `challenge`: higher acceptance threshold to increase challenge probability.
- `forced-challenge`: deterministic evidence-loop path for regression checks.
- `fast`: shorter waits and no auto round3 for quicker smoke runs.

3. Run the command via script.

```bash
python3 scripts/run_runtime.py \
  --preset forced-challenge \
  --target . \
  --output agent_team_demo/output_skill_forced
```

4. Run OpenAI-compatible provider when required.

```bash
export OPENAI_API_KEY="your_key"
python3 scripts/run_runtime.py \
  --provider openai \
  --model gpt-4.1-mini \
  --preset challenge \
  --target . \
  --output agent_team_demo/output_skill_openai
```

## Verify Artifacts

Validate required files, task status, events, and report sections:

```bash
python3 scripts/verify_run.py --output agent_team_demo/output_skill_forced --require-evidence-events
```

Run minimal validation:

```bash
python3 scripts/verify_run.py --output agent_team_demo/output_skill_default
```

## Tune Runtime

Tune these controls through runtime flags or presets:
- teammate mode: `--teammate-mode in-process|tmux`
- tmux worker transport controls: `--tmux-worker-timeout-sec`, `--tmux-fallback-on-error` / `--no-tmux-fallback-on-error`
- dynamic task insertion: `--dynamic-tasks` / `--no-dynamic-tasks`
- teammate reply mode: `--teammate-provider-replies` / `--no-teammate-provider-replies`
- teammate local memory window: `--teammate-memory-turns`
- checkpoint/resume: `--max-completed-tasks`, `--resume-from` (directly supported by `scripts/run_runtime.py`)
- checkpoint rewind: `--rewind-to-history-index`
- event-index rewind: `--rewind-to-event-index`
- rewind branch output: `--rewind-branch`, `--rewind-branch-output`
- checkpoint history replay report: `--history-replay-report`, `--history-replay-report-path`, `--history-replay-start-index`, `--history-replay-end-index`
- event replay report: `--event-replay-report`, `--event-replay-report-path`, `--event-replay-max-transitions`
- adjudication thresholds: `--adjudication-accept-threshold`, `--adjudication-challenge-threshold`
- adjudication weights: `--adjudication-weight-*`
- evidence timings: `--peer-wait-seconds`, `--evidence-wait-seconds`
- round3 toggle: `--auto-round3-on-challenge` / `--no-auto-round3-on-challenge`
- re-adjudication bonus: `--re-adjudication-max-bonus`, `--re-adjudication-weight-*`

Use `--extra-arg` in `run_runtime.py` for custom runtime options not covered by presets.

## Resources

- `references/presets.md`: preset selection and tuning guidance.
- `scripts/run_runtime.py`: portable runtime launcher.
- `scripts/verify_run.py`: deterministic artifact and event validator.

If runtime files are missing in a target repository, copy `agent_team_demo/agent_team_runtime.py` and `agent_team_demo/llm_provider.py` first, then use this skill.
