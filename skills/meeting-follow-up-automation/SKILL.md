---
name: meeting-follow-up-automation
description: Turn meeting outcome follow-ups into identity-safe Notion tasks and optional safe task-focus calendar proposals.
---

# Meeting Follow-Up Automation

Use this skill when Rocky needs to chase post-meeting commitments.

## Contract

- Inputs: sanitized meeting outcome follow-up tasks.
- Outputs: identity-resolved Notion task creates/updates, skipped duplicates, terminal-match skips, and optional task-focus booking results.
- Side effects: Notion writes only in live mode. Calendar writes only through the existing task-focus rails and only when explicitly requested with `--apply-safe-followups`.
- No auto-completion: never mark a task done, cancelled, or archived from transcript silence or an LLM guess.

## Calendar Policy

Task focus booking remains bound by Rocky calendar policy. No proactive Friday, Saturday, or Sunday booking is allowed.

## Logging

Log task refs, dedupe keys, action fingerprints, audit ids, and safe summaries. Do not log raw transcript bodies or sensitive content.
