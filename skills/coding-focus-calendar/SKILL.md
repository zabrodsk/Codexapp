---
name: coding-focus-calendar
description: Propose and book Rocky-owned coding focus blocks with useful calendar descriptions grounded in sanitized coding signals.
---

# Coding Focus Calendar

Use this skill when Rocky should protect time for coding work.

## Contract

- Inputs: ranked coding work items, planning date, Apple Calendar availability.
- Outputs: dry-run proposals or live booking results with audit id, idempotency key, and a human-readable focus description.
- Permissions: Apple Calendar read for proposals; Apple Calendar write only through live scheduler or explicit live command.
- Side effects: creates only Rocky-owned `Rocky: Coding focus - ...` calendar blocks.
- Booking policy: Monday-Thursday only, same-day only, minimum 60 minutes, no end after 19:30, no meeting moves.
- Calendar description: includes focus, where Dusan left off, recommended next step, done signal, source refs, and Rocky metadata.
- Safety: never include raw transcripts, diffs, secrets, tokens, cookies, credentials, or auth strings.

## Runtime

```bash
/Users/clawdbot/.openclaw/workspace/.venv/bin/python scripts/rocky_runtime_tools.py coding-focus-proposals --json
```
