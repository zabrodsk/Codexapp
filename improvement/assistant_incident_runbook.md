# Rocky Assistant Incident Runbook

## Check Notification Health

```bash
/Users/clawdbot/.openclaw/workspace/.venv/bin/python scripts/rocky_runtime_tools.py \
  assistant-notification-health --json
```

Healthy means Discord is available or AgentMail fallback is ready. Discord `403` is `discord_permission_denied`; AgentMail must then be ready.

## List Incidents

```bash
/Users/clawdbot/.openclaw/workspace/.venv/bin/python scripts/rocky_runtime_tools.py \
  assistant-incident-recent --limit 20 --json
```

Statuses: `open`, `notified`, `waiting_for_user`, `retrying`, `recovered`, `acknowledged`, `ignored`.

## Process Incidents

```bash
/Users/clawdbot/.openclaw/workspace/.venv/bin/python scripts/rocky_runtime_tools.py \
  assistant-incident-manager-run --live --json
```

This may resend missed daily or weekly briefings through the notification router. It may also ask Dusan for guidance on email triage no-slot cases. It does not create Calendar events or mutate Notion.

## Retry One Incident

```bash
/Users/clawdbot/.openclaw/workspace/.venv/bin/python scripts/rocky_runtime_tools.py \
  assistant-incident-retry --dead-letter-id DEAD_LETTER_ID --live --json
```

Retry only after checking the incident source. Successful retry or fallback delivery may mark the dead letter `recovered`.

## Respond To User-Action Incidents

```bash
/Users/clawdbot/.openclaw/workspace/.venv/bin/python scripts/rocky_runtime_tools.py \
  assistant-incident-respond --dead-letter-id DEAD_LETTER_ID --action acknowledge --live --json
```

Allowed actions are `acknowledge`, `ignore`, and `recover`. These are state-only and audit-only.

## Email Triage No-Slot Playbook

When email triage cannot fit into the same-day window, Rocky asks Dusan to choose:

- split email triage into smaller available chunks if possible;
- book the next allowed working-day slot;
- skip or acknowledge for today.

No alternate booking is made until Dusan gives guidance.

## Production Readiness

```bash
/Users/clawdbot/.openclaw/workspace/.venv/bin/python scripts/rocky_runtime_tools.py \
  assistant-production-readiness --expected-date 2026-05-25 --expected-week 2026-W22 --json
```

`manual_review_required` is acceptable when the incident was communicated and Rocky is waiting for Dusan. `not_ready` means uncommunicated failure or broken infrastructure.
