---
name: meeting-prep-notion
description: Manage Rocky's Notion meeting prep database and idempotent prep-page upserts.
---

# Meeting Prep Notion

Use this skill when Rocky stores a meeting prep artifact in Notion.

Database: `Rocky Meeting Prep`.

Required properties:
- Title
- Meeting date
- Meeting key
- Calendar event ref
- Status
- Context confidence
- People / companies
- Source refs
- Dusan notes
- Brief
- Questions
- Open loops
- Discord status
- Last sent at
- Message hash
- Updated date

Upsert by `Meeting key`. Creating/updating prep notes requires explicit live
mode. This skill must not mutate Rocky's personal task database except through
separate task-spine command paths.
