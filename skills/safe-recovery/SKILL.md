# Safe Recovery

Use this skill when inspecting or applying Rocky recovery actions that are explicitly state-only.

## Contract

- Primary command: `assistant-safe-recovery --json`.
- Mutation requires `--live`.
- Allowed actions:
  - `mark-calendar-stale` for verified missing Rocky Calendar state.
  - `update-dead-letter` for changing a dead-letter status to `recovered`, `acknowledged`, or `ignored`.
- Orphan Apple Calendar events are report-only in this skill.

## Boundaries

- Never edit Apple Calendar events.
- Never mutate Notion tasks.
- Never send Discord messages.
- Never touch TrainingPeaks, Apple Mail, Betty, LaunchAgents, Mission Control, Hermes, or OpenClaw cron.
- Write only Rocky assistant state and audit records when `--live` is supplied.

## Evidence

- Prefer state ids, idempotency keys, audit ids, dead-letter ids, and hashes.
- Do not include raw email bodies, transcripts, calendar descriptions, webcal URLs, secrets, tokens, cookies, credentials, diffs, or screen-recording content.
