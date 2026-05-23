---
name: notion-task-manager
description: Manage Rocky's dedicated Notion task spine database with safe schema checks, action-aware upserts, lifecycle metadata, and sanitized task state.
---

# Notion Task Manager

Use this skill when Rocky needs to create, update, inspect, repair, or lifecycle-update the dedicated personal task database in Notion.

## Contract

- Inputs: normalized task objects, action fingerprint, dedupe key, optional existing page id, Notion config.
- Outputs: sanitized task records, page ids, dedupe keys, schema health, and write status.
- Permissions: Notion database read/write through configured integration token only.
- Side effects: creates or updates Rocky-owned Notion database/pages only when a live flag is supplied.
- Failure behavior: return blocked status with safe reason; never log tokens or raw source bodies.
- Identity: update by explicit page id, action-aware dedupe key, or action fingerprint; do not blindly merge by source ref.

## Runtime

```bash
/Users/clawdbot/.openclaw/workspace/.venv/bin/python scripts/rocky_runtime_tools.py notion-task-health --json
/Users/clawdbot/.openclaw/workspace/.venv/bin/python scripts/rocky_runtime_tools.py notion-task-schema-ensure --live --json
```

## Sprint 8 Contract Update

The task manager may update task status and due dates for explicit trusted commands after identity matching. Done, Cancelled, and Archived tasks remain terminal and must not be reopened automatically.
