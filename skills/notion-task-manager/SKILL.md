---
name: notion-task-manager
description: Manage Rocky's dedicated Notion task spine database with safe schema checks, upserts, dedupe keys, and sanitized task state.
---

# Notion Task Manager

Use this skill when Rocky needs to create, update, inspect, or repair the dedicated personal task database in Notion.

## Contract

- Inputs: normalized task objects, Notion config, optional database or parent page state.
- Outputs: sanitized task records, page ids, dedupe keys, schema health, and write status.
- Permissions: Notion database read/write through configured integration token only.
- Side effects: creates or updates Rocky-owned Notion database/pages only when a live flag is supplied.
- Failure behavior: return blocked status with safe reason; never log tokens or raw source bodies.
- Audit: task creation/update should be represented through the assistant audit log or scheduler run state.

## Example

```bash
/Users/clawdbot/.openclaw/workspace/.venv/bin/python scripts/rocky_runtime_tools.py notion-task-health --json
/Users/clawdbot/.openclaw/workspace/.venv/bin/python scripts/rocky_runtime_tools.py notion-task-schema-ensure --live --json
```
