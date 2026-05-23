---
name: task-deduper
description: Resolve Rocky task identity using source references plus action fingerprints so one source can create multiple distinct tasks without duplicate noise.
---

# Task Deduper

Use this skill when Rocky needs to decide whether a detected task should create a new Notion task, update an existing task, skip a terminal task, or go to manual review.

## Contract

- Inputs: normalized task candidates, existing Notion task summaries, source refs, action fingerprints, dedupe keys.
- Outputs: identity decisions: `create`, `update`, `duplicate`, `terminal_match_skipped`, or `manual_review_required`.
- Permissions: Notion read through the task manager; no direct Notion writes.
- Side effects: none directly.
- Failure behavior: return `manual_review_required` when legacy source-ref matching is ambiguous.
- Safety: never merge tasks solely because they came from the same email or source ref.

## Runtime

```bash
/Users/clawdbot/.openclaw/workspace/.venv/bin/python scripts/rocky_runtime_tools.py task-detect --source email --limit 5 --json
```
