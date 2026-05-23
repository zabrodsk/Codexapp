# Rocky Assistant Calendar Runbook

Sprint 3.1 hardens the manual live-write foundation for Rocky-owned Apple Calendar blocks.
It does not enable scheduled proactive booking.

## Safety Rules

- Live calendar create/delete requires `--live`.
- Rocky may only create titles beginning with `Rocky:`.
- Rocky may only delete events with `Rocky:` title, `Booked by: Rocky`, and matching idempotency key.
- Never direct-write Apple Calendar SQLite.
- Never proactively book Friday, Saturday, or Sunday.
- Mission Control, Hermes Kanban, Betty LaunchAgents, cron, and new LaunchAgents are out of scope for Sprint 3.1.

## Health Check

```bash
cd /Users/clawdbot/.openclaw/workspace
/Users/clawdbot/.openclaw/workspace/.venv/bin/python scripts/rocky_runtime_tools.py \
  calendar-write-health --json
```

This checks Apple Calendar DB readability, Swift availability, EventKit authorization, and AppleScript Calendar access.
It does not create, update, or delete events.

## Calendar TCC Probe

Use this when a LaunchAgent can run Rocky but direct Calendar access differs from the SSH shell:

```bash
cd /Users/clawdbot/.openclaw/workspace
/Users/clawdbot/.openclaw/workspace/.venv/bin/python scripts/rocky_runtime_tools.py \
  calendar-tcc-probe --json
```

This is read-only. It reports the current execution context (`ssh`, `launchd`, or `interactive`), Calendar DB access, EventKit authorization, and AppleScript Calendar access.

Sprint 4.3.1 verified that direct launchd venv Python for `com.openclaw.rocky-calendar-tcc-probe` is blocked by macOS TCC:

- Calendar DB: `authorization denied`
- EventKit: `not_determined`
- AppleScript Calendar: timed out

Because the SSH context has verified Calendar access, the production training scheduler intentionally uses a localhost SSH bridge until macOS permissions can be granted cleanly to the direct launchd executable.

```bash
cd /Users/clawdbot/.openclaw/workspace
/Users/clawdbot/.openclaw/workspace/.venv/bin/python scripts/rocky_runtime_tools.py \
  assistant-scheduler-health \
  --job training_calendar_booking \
  --json
```

The health output should report `execution_mode: localhost_ssh_bridge`, a clean stderr log, clean scheduler state, and available localhost SSH.

## Status Check

```bash
cd /Users/clawdbot/.openclaw/workspace
/Users/clawdbot/.openclaw/workspace/.venv/bin/python scripts/rocky_runtime_tools.py \
  calendar-block-status \
  --idempotency-key rocky:task_focus:2026-05-25:example \
  --calendar Calendar \
  --json
```

Expected safe states:

- `active_verified`: Rocky state and Apple Calendar agree.
- `deleted_verified`: Rocky state says deleted and no matching event remains.

States needing review:

- `stale_state_candidate`: Rocky state is active but no matching Calendar event exists.
- `orphan_calendar_event`: Rocky state says deleted but a matching event remains.
- `calendar_mismatch`: requested calendar and stored calendar disagree.
- `state_missing`: no Rocky state row exists for that idempotency key.

Status output is sanitized and does not include raw Calendar notes.

## Reconcile State

Read-only:

```bash
cd /Users/clawdbot/.openclaw/workspace
/Users/clawdbot/.openclaw/workspace/.venv/bin/python scripts/rocky_runtime_tools.py \
  calendar-block-reconcile \
  --calendar Calendar \
  --json
```

Mark active missing events stale in Rocky state only:

```bash
cd /Users/clawdbot/.openclaw/workspace
/Users/clawdbot/.openclaw/workspace/.venv/bin/python scripts/rocky_runtime_tools.py \
  calendar-block-reconcile \
  --calendar Calendar \
  --mark-stale \
  --json
```

`--mark-stale` only updates `improvement/assistant_calendar.sqlite3`; it never edits Apple Calendar.

## Create A Manual Rocky Block

```bash
cd /Users/clawdbot/.openclaw/workspace
/Users/clawdbot/.openclaw/workspace/.venv/bin/python scripts/rocky_runtime_tools.py \
  calendar-block-create \
  --kind task_focus \
  --date 2026-05-25 \
  --window-start 07:15 \
  --window-end 07:30 \
  --duration-minutes 15 \
  --label "Sprint 3.1 smoke" \
  --reason "Sprint 3.1 approved live create-delete smoke" \
  --confidence high \
  --source-ref sprint3.1-live-smoke \
  --calendar Calendar \
  --live \
  --json
```

## Delete A Manual Rocky Block

```bash
cd /Users/clawdbot/.openclaw/workspace
/Users/clawdbot/.openclaw/workspace/.venv/bin/python scripts/rocky_runtime_tools.py \
  calendar-block-delete \
  --idempotency-key rocky:task_focus:2026-05-25:example \
  --calendar Calendar \
  --live \
  --json
```

Sprint 3 showed that AppleScript event enumeration can time out during delete.
Sprint 3.1 therefore treats EventKit via Swift as the primary delete path and keeps AppleScript delete only as a fallback.

## Recovery Notes

- If create fails with AppleScript/TCC errors, run `calendar-write-health --json` and inspect macOS Automation permissions for Calendar.
- If delete fails with EventKit errors, run `calendar-write-health --json` and inspect Calendar permission status.
- If status shows `stale_state_candidate`, run read-only reconcile first, then `--mark-stale` if the missing event is expected.
- If status shows `orphan_calendar_event`, manually inspect Apple Calendar. Sprint 3.1 intentionally does not add force-delete behavior.
- Every successful live create/delete and every stale-state mutation must be traceable in `improvement/assistant_audit.jsonl`.
