---
name: task-reminder-engine
description: Select due Rocky tasks and dispatch concise reminder summaries without duplicating normal success noise.
---

# Task Reminder Engine

Use this skill when Rocky needs to remind Dusan about open tasks.

## Contract

- Inputs: Notion task rows or normalized task payloads, current local date, notification options.
- Outputs: due reminder set, notification result, and safe counts.
- Permissions: Notion read; Discord write only when notify is enabled.
- Side effects: Discord reminder notification when explicitly enabled by scheduler or command.
- Failure behavior: blocked status on Notion read failure; failed Discord delivery creates assistant dead letter through notification dispatcher.
- Noise policy: summarize due tasks together rather than sending one message per task.

## Example

```bash
/Users/clawdbot/.openclaw/workspace/.venv/bin/python scripts/rocky_runtime_tools.py task-reminders-run --notify --json
```
