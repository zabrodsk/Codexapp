---
name: task-focus-calendar
description: Propose and book Rocky-owned task focus blocks using existing calendar policy, duplicate detection, state, and audit rails.
---

# Task Focus Calendar

Use this skill when Rocky should protect time for a high-confidence personal task.

## Contract

- Inputs: normalized Notion tasks, planning date, Apple Calendar availability.
- Outputs: dry-run proposals or supervised live booking result with audit id and idempotency key.
- Permissions: Apple Calendar read for proposals; Apple Calendar write only with explicit live scheduler/command.
- Side effects: creates only Rocky-owned `Rocky: Task focus - ...` blocks.
- Booking policy: Monday-Thursday only, minimum 30 minutes, no end after 19:30, never move existing meetings.
- Failure behavior: conflicts, duplicates, weekends, stale state, and missing live flag block before Calendar mutation.

## Example

```bash
/Users/clawdbot/.openclaw/workspace/.venv/bin/python scripts/rocky_runtime_tools.py task-focus-proposals --json
```
