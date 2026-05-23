# AgentMail Bridge Runbook

The AgentMail bridge source of truth lives in `services/agentmail-bridge/`.
Production still runs from `/Users/clawdbot/.openclaw/agentmail-bridge/bridge.mjs` via LaunchAgent `ai.openclaw.agentmail-bridge`.

## Secret Boundaries

Commit source, tests, package files, email-security rules, and the sanitized config template only.
Do not commit live `config.json`, `pending-approvals.json`, `node_modules`, logs, backups, AgentMail credentials, Discord tokens, raw emails, or approval payloads.

## Health

```bash
cd /Users/clawdbot/.openclaw/workspace
/Users/clawdbot/.openclaw/workspace/.venv/bin/python scripts/rocky_runtime_tools.py agentmail-bridge-health --run-tests --json
```

Healthy means tracked source hashes match deployed runtime hashes, the LaunchAgent points at `/Users/clawdbot/.openclaw/agentmail-bridge/bridge.mjs`, and the Node tests pass.

## Deploy

```bash
cd /Users/clawdbot/.openclaw/workspace
/Users/clawdbot/.openclaw/workspace/.venv/bin/python scripts/agentmail_bridge_deploy.py --json
/Users/clawdbot/.openclaw/workspace/.venv/bin/python scripts/agentmail_bridge_deploy.py --apply --json
launchctl kickstart -k gui/$(id -u)/ai.openclaw.agentmail-bridge
/Users/clawdbot/.openclaw/workspace/.venv/bin/python scripts/rocky_runtime_tools.py agentmail-bridge-health --run-tests --json
```

The deploy helper snapshots overwritten runtime files under `/Users/clawdbot/.openclaw/backups/agentmail-bridge/` before copying tracked files.

## Rollback

Copy the relevant timestamped backup files from `/Users/clawdbot/.openclaw/backups/agentmail-bridge/` back into `/Users/clawdbot/.openclaw/agentmail-bridge/` and `/Users/clawdbot/.openclaw/email-security/`, then kickstart the LaunchAgent.
