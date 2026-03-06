# Preset Guide

## Presets

- `default`
  - Use for normal repository scans and baseline behavior.
  - Keep default thresholds and waits from runtime.

- `static`
  - Use to keep the previous fixed task graph behavior.
  - Applies `--no-dynamic-tasks`.

- `tmux`
  - Use to run analyst tasks through tmux-mode worker transport.
  - Applies `--teammate-mode tmux` (falls back to subprocess when tmux is unavailable).
  - Tune with `--tmux-worker-timeout-sec` and `--no-tmux-fallback-on-error` when debugging transport.

- `challenge`
  - Use for high-bar review where acceptance should be harder.
  - Applies `accept=95`, `challenge=60`, `peer_wait=2`, `evidence_wait=2`.

- `forced-challenge`
  - Use for deterministic challenge/evidence-loop validation.
  - Applies `accept=95`, `challenge=0`, `peer_wait=0.01`, `evidence_wait=1`.
  - Expect `evidence_round_started` and `lead_re_adjudication_published` events.

- `fast`
  - Use for quick smoke checks during frequent iteration.
  - Applies shorter waits and disables auto round3.

## Common Invocation Patterns

Config-driven run:

```bash
python3 scripts/run_runtime.py \
  --config agent_team_demo/examples/agent-team.config.json \
  --host-kind codex \
  --target . \
  --output agent_team_demo/output_skill_config
```

Default run:

```bash
python3 scripts/run_runtime.py --preset default --target . --output agent_team_demo/output_skill_default
```

Static run (dynamic insertion disabled):

```bash
python3 scripts/run_runtime.py --preset static --target . --output agent_team_demo/output_skill_static
```

Tmux-mode run:

```bash
python3 scripts/run_runtime.py \
  --preset tmux \
  --target . \
  --output agent_team_demo/output_skill_tmux \
  --tmux-worker-timeout-sec 180
```

Resume pattern:

```bash
python3 scripts/run_runtime.py \
  --preset default \
  --target . \
  --output agent_team_demo/output_skill_resume \
  --max-completed-tasks 3
python3 scripts/run_runtime.py \
  --preset default \
  --target . \
  --output agent_team_demo/output_skill_resume \
  --resume-from agent_team_demo/output_skill_resume/run_checkpoint.json
python3 scripts/run_runtime.py \
  --preset default \
  --target . \
  --output agent_team_demo/output_skill_resume \
  --rewind-to-history-index 0 \
  --max-completed-tasks 1
python3 scripts/run_runtime.py \
  --preset default \
  --target . \
  --output agent_team_demo/output_skill_resume \
  --rewind-to-event-index 0 \
  --max-completed-tasks 1
python3 scripts/run_runtime.py \
  --preset default \
  --target . \
  --output agent_team_demo/output_skill_resume \
  --rewind-to-history-index 0 \
  --rewind-branch \
  --max-completed-tasks 1
python3 scripts/run_runtime.py \
  --preset default \
  --output agent_team_demo/output_skill_resume \
  --history-replay-report
python3 scripts/run_runtime.py \
  --preset default \
  --output agent_team_demo/output_skill_resume \
  --event-replay-report
```

Forced challenge run:

```bash
python3 scripts/run_runtime.py --preset forced-challenge --target . --output agent_team_demo/output_skill_forced
```

Verify default run:

```bash
python3 scripts/verify_run.py --output agent_team_demo/output_skill_default
```

Verify forced challenge run:

```bash
python3 scripts/verify_run.py --output agent_team_demo/output_skill_forced --require-evidence-events
```

## Extra Runtime Args

Pass extra runtime flags through `--extra-arg` (repeatable):

```bash
python3 scripts/run_runtime.py \
  --preset challenge \
  --target . \
  --output agent_team_demo/output_skill_custom \
  --extra-arg --re-adjudication-max-bonus \
  --extra-arg 20 \
  --extra-arg --adjudication-weight-completeness \
  --extra-arg 0.5 \
  --extra-arg --teammate-provider-replies \
  --extra-arg --teammate-memory-turns \
  --extra-arg 6
```
