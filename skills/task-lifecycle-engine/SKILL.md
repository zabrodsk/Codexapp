---
name: task-lifecycle-engine
description: Update Rocky task lifecycle metadata such as reminder dates and counts while respecting terminal task statuses.
---

# Task Lifecycle Engine

Use this skill when Rocky needs to update task reminder metadata, skip completed/cancelled/archived tasks, or prepare safe lifecycle state for reminders.

## Contract

- Inputs: Notion task rows or normalized task payloads, local date, live flag.
- Outputs: due lifecycle updates, skipped terminal counts, next reminder dates, and safe audit ids.
- Permissions: Notion read/write only when `--live` is supplied.
- Side effects: updates `Last reminded date`, `Reminder count`, `Next reminder date`, and `Last lifecycle reason`.
- Failure behavior: skip tasks without page ids and report blocked update records.
- Safety: never infer completion from source silence, read email state, or LLM guesses.

## Runtime

```bash
/Users/clawdbot/.openclaw/workspace/.venv/bin/python scripts/rocky_runtime_tools.py task-lifecycle-run --live --json
```
