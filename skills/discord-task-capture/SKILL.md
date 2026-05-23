# Discord Task Capture

Purpose: capture explicit task commands from Dusan in approved Discord contexts.

Inputs:
- Approved Discord channels from OpenClaw configuration.
- Messages authored by the configured Dusan user id.
- Messages that mention Rocky or start with an explicit task-command pattern.

Outputs:
- Sanitized command payloads with Discord source refs.
- No Discord token, raw secrets, cookies, credentials, or unrelated channel content.

Rules:
- Ignore bots and non-Dusan authors.
- Do not infer tasks from ordinary chat in Sprint 8; require an explicit command shape.
- Apply commands through the task command interpreter and Notion identity resolver.
- Discord capture may update Notion any day, but it must never create Friday/Saturday/Sunday proactive calendar blocks.

Example invocation:

```bash
/Users/clawdbot/.openclaw/workspace/.venv/bin/python scripts/rocky_runtime_tools.py \
  task-command-capture-run --source discord --live --notify-failures --json
```
