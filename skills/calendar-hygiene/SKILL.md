---
name: calendar-hygiene
description: Inspect Rocky-owned calendar state and Apple Calendar read-only matches for duplicate blocks, stale state, orphan events, missing metadata, and policy violations.
---

# Calendar Hygiene Skill

Use this skill when Rocky reviews calendar cleanliness and recovery needs.

## Contract

Inputs are Rocky assistant calendar state plus read-only Apple Calendar event matches. Outputs are sanitized hygiene findings such as duplicate Rocky blocks, stale assistant state, orphan Rocky events, missing Rocky metadata, weekend-policy violations, overbooked days, and days with little focus space.

Runtime:

```bash
/Users/clawdbot/.openclaw/workspace/.venv/bin/python scripts/rocky_runtime_tools.py \
  weekly-calendar-hygiene --start-date 2026-05-25 --days 14 --json
```

## Mutation Boundary

`--mark-stale --live` may update Rocky assistant state from active to stale when read-only evidence shows the Calendar event is missing. It must never edit, create, move, or delete Apple Calendar events.

## Safety

- Never direct-write Apple Calendar SQLite.
- Never delete or force-delete orphan events in this skill.
- Do not include raw calendar descriptions, private notes, tokens, cookies, credentials, or secret-bearing URLs.
- Weekend-policy violations are reported for human review; automatic fixes remain out of scope.
