---
name: discord-context-note-capture
description: Capture Dusan's Discord notes to Rocky for day planning and meeting preparation.
---

# Discord Context Note Capture

Use this skill when Dusan sends Rocky contextual instructions or notes by
Discord, such as on the way to the office.

Behavior:
- Capture only approved Dusan identity/channel messages.
- Prefer explicit phrases such as `Rocky`, `context`, `note`, `prep`, `brief`, `for my meeting`, or `for today`.
- Keep this separate from task commands, while allowing clearly actionable
  commands to continue through the existing task-command lane.
- Store a sanitized note, hash, source ref, target date, and classification.
- Acknowledge once in Discord when live.

Captured notes are high-priority intent signals for same-day planning and
meeting prep, but they cannot override security, calendar booking, notification,
or Notion mutation policy.
