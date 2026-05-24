---
name: outcome-observer
description: Collect sanitized Rocky assistant outcomes from scheduler, calendar, task, command, audit, and dead-letter state without widening automation authority.
---

# Outcome Observer

Use this skill when Rocky compares predictions with outcomes for bounded assistant learning.

## Contract

- Inputs: assistant scheduler runs, assistant calendar state, task/command summaries, audit refs, dead letters, and optional fixture outcomes.
- Outputs: sanitized outcome observations with lane, source ref, idempotency key, predicted/booked/actual minutes when available, status, confidence, and safe summary.
- Side effects: writes `improvement/assistant_learning.sqlite3` only when `--live` is supplied; never writes Calendar, Notion, email, TrainingPeaks, or raw source artifacts.
- Safety: no raw email bodies, transcripts, calendar descriptions, secrets, tokens, cookies, credentials, diffs, webcal URLs, or screen recording frames.

## Runtime

```bash
/Users/clawdbot/.openclaw/workspace/.venv/bin/python scripts/rocky_runtime_tools.py \
  assistant-outcomes-collect --since-days 7 --live --json
```
