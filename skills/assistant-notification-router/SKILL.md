# Assistant Notification Router Skill

## Purpose

Use this skill when Rocky needs to deliver a user-facing assistant notification, failure alert, missed-brief resend, or guidance request.

## Contract

- Route Discord first and AgentMail email fallback second.
- Classify Discord `403` as `discord_permission_denied`.
- Send fallback email from Rocky's AgentMail account to `dusan.zabrodsky@rockaway.cz`.
- Preserve useful daily/weekly brief formatting.
- Store only safe metadata: delivery status, hashes, audit IDs, channel names, and failure classes.
- Never log tokens, credentials, cookies, raw email bodies, raw transcripts, raw calendar descriptions, TrainingPeaks webcal URLs, diffs, or private note bodies.

## Operational Commands

```bash
/Users/clawdbot/.openclaw/workspace/.venv/bin/python scripts/rocky_runtime_tools.py \
  assistant-notification-health --json
```

```bash
/Users/clawdbot/.openclaw/workspace/.venv/bin/python scripts/rocky_runtime_tools.py \
  assistant-notification-dispatch --status blocked --reason manual_review_required --dry-run --json
```

## Recovery Rules

- If Discord succeeds, do not send fallback email.
- If Discord fails and AgentMail succeeds, treat the notification as delivered with fallback evidence.
- If both fail, create one deduped dead letter and leave it open for the incident manager.
- Notification failure must not roll back tasks, bookings, brief generation, or other completed lane work.
