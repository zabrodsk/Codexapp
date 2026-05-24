# Production Readiness

Use this skill when checking whether Rocky's production assistant lanes are healthy, pending, degraded, or blocked.

## Contract

- Primary command: `assistant-production-readiness --json`.
- This is an operator/status layer, not a booking authority.
- It aggregates existing lane health, readiness gates, dead letters, calendar hygiene, Notion health, AgentMail bridge health, and Calendar write health.
- It must not create, move, delete, or update Calendar events.
- It must not mutate Notion, Discord, TrainingPeaks, Mail, LaunchAgents, Mission Control, Hermes, or OpenClaw cron.

## Safety

- Treat all lane summaries as internal metadata.
- Never surface raw email bodies, meeting transcripts, calendar descriptions, webcal URLs, secrets, tokens, cookies, credentials, diffs, or screen-recording content.
- Friday/Saturday/Sunday proactive booking remains forbidden regardless of readiness status.

## Expected Statuses

- `ready_verified`: all critical lanes are healthy and verified.
- `ready_pending_natural_runs`: lanes are healthy but waiting for first natural-run proof.
- `manual_review_required`: operator attention is needed, but the system is not necessarily broken.
- `not_ready`: at least one blocking production signal is broken.
