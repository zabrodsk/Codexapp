---
name: coding-session-inspector
description: Inspect sanitized Codex, Claude, and git metadata to identify active coding work without copying raw transcripts, diffs, or secrets.
---

# Coding Session Inspector

Use this skill when Rocky needs to understand where Dusan left off in coding work.

## Contract

- Inputs: sanitized laptop manifest, local Codex/Claude session metadata, and optional repo status summaries.
- Outputs: coding signals with project, source refs, branch, last seen time, where-left-off summary, and recommended next step.
- Permissions: read local session metadata and git status; laptop sync may write only the sanitized manifest into Rocky's inbox.
- Side effects: none except `coding-signal-sync`, which writes or pushes sanitized JSON.
- Signal priority: Codex/Claude sessions are the freshness source of truth; git/repo metadata is weak supporting evidence and must not dominate selection.
- Safety: never copy raw transcripts, diffs, credentials, cookies, tokens, browser state, or full private content.

## Runtime

```bash
/Users/clawdbot/.openclaw/workspace/.venv/bin/python scripts/rocky_runtime_tools.py coding-signal-inspect --json
```
