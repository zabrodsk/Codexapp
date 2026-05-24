---
name: priority-arbitrator
description: Rank Dusan's daily work across meetings, email, tasks, coding, training, and assistant health without overbooking the calendar.
---

# Priority Arbitrator Skill

Use this skill when Rocky needs to decide what Dusan should do first today.

## Contract

Inputs are sanitized daily personal signals. The arbitrator returns top priority, do-first items, protected time, decision needs, risks, suggested focus, what Rocky handled, and optional safe booking actions.

## Policy

- Meetings and personal commitments are immovable.
- Training status is surfaced early when present.
- Urgent or overdue tasks and time-sensitive unread email outrank coding.
- High-confidence coding work outranks routine tasks when there is enough free time.
- If the day is overloaded, report what does not fit instead of forcing more calendar blocks.
- Booking actions are only suggestions to existing lane rails; they must not bypass policy, duplicate, conflict, or weekend guards.

## Example Invocation

```bash
/Users/clawdbot/.openclaw/workspace/.venv/bin/python scripts/rocky_runtime_tools.py \
  daily-priority-arbitrate --signals-file /tmp/daily-signals.json --json
```
