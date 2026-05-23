---
name: task-reminder-engine
description: Select due Rocky tasks and dispatch concise reminder summaries, optionally updating reminder metadata through the lifecycle engine.
---

# Task Reminder Engine

Use this skill when Rocky needs to pick due tasks, batch reminders, and optionally update reminder lifecycle metadata.

## Contract

- Inputs: Notion task rows or normalized task payloads, current local date, notification options, live flag.
- Outputs: due reminder set, notification result, lifecycle update result, and safe counts.
- Permissions: Notion read; Notion write only with `--live`; Discord write only when notify is enabled.
- Side effects: Discord reminder notification when requested; reminder metadata updates only with `--live`.
- Failure behavior: blocked status on Notion read failure; failed Discord delivery creates assistant dead letter through notification dispatcher.
- Safety: terminal tasks are not reminded or reopened.

## Runtime

```bash
/Users/clawdbot/.openclaw/workspace/.venv/bin/python scripts/rocky_runtime_tools.py task-reminders-run --notify --live --json
```

## Sprint 8 Contract Update

Command capture may update task lifecycle metadata, but proactive task focus calendar booking remains subject to the no-Friday/Saturday/Sunday guardrail. Reminder notifications should stay failure/manual-review oriented unless explicitly requested.
