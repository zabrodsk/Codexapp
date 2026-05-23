# Email Command Capture

Purpose: mirror trusted email-to-Rocky task commands into Rocky's task spine without altering normal AgentMail routing.

Inputs:
- Trusted sender messages from the AgentMail bridge.
- Command-like subjects or bodies such as `remember`, `add task`, `mark done`, or `cancel task`.

Outputs:
- Local sanitized JSONL command records under Rocky's task-command inbox.
- No success email replies, raw tokens, cookies, credentials, or third-party auto-created tasks.

Rules:
- Only trusted user senders may be mirrored.
- Third-party email never auto-creates tasks in Sprint 8.
- The bridge sidecar must not change normal AgentMail reply, approval, or routing behavior.
- Commands are applied by the task command interpreter and Notion identity resolver.

Example invocation:

```bash
/Users/clawdbot/.openclaw/workspace/.venv/bin/python scripts/rocky_runtime_tools.py \
  task-command-capture-run --source email --live --notify-failures --json
```
