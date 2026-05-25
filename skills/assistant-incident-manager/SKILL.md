# Assistant Incident Manager Skill

## Purpose

Use this skill when Rocky finds open dead letters, missed briefing delivery, known business blocks, or recovery-needed assistant failures.

## Operating Rule

Rocky must try to complete the action. If blocked, Rocky must communicate the issue through the notification router. If guidance is needed, Rocky asks Dusan with concrete options. If a retry or compensating notification succeeds, Rocky may mark the incident recovered with an audit trail.

## Incident Statuses

- `open`: failure detected but not yet handled.
- `notified`: Dusan was told and no immediate decision is needed.
- `waiting_for_user`: Dusan needs to choose a safe next step.
- `retrying`: a live retry is in progress.
- `recovered`: retry or compensation was verified.
- `acknowledged`: operator accepted/closed the incident.
- `ignored`: operator intentionally ignored it.

## Recovery Boundaries

- State-only recovery is allowed for dead-letter status updates.
- Missed daily/weekly brief delivery may be resent without redoing bookings or Notion writes.
- Email triage `no_available_slot` must ask Dusan whether to split the block, book the next allowed working day, or skip.
- Do not create, move, or delete Apple Calendar events here.
- Do not mutate Notion tasks here.
- Do not touch Mission Control, Hermes, OpenClaw cron, or Betty.

## Commands

```bash
/Users/clawdbot/.openclaw/workspace/.venv/bin/python scripts/rocky_runtime_tools.py \
  assistant-incident-manager-run --live --json
```

```bash
/Users/clawdbot/.openclaw/workspace/.venv/bin/python scripts/rocky_runtime_tools.py \
  assistant-incident-recent --limit 20 --json
```

```bash
/Users/clawdbot/.openclaw/workspace/.venv/bin/python scripts/rocky_runtime_tools.py \
  assistant-incident-retry --dead-letter-id DEAD_LETTER_ID --live --json
```

```bash
/Users/clawdbot/.openclaw/workspace/.venv/bin/python scripts/rocky_runtime_tools.py \
  assistant-incident-respond --dead-letter-id DEAD_LETTER_ID --action acknowledge --live --json
```
