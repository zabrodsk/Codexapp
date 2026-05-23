# TrainingPeaks Read Path Discovery - Sprint 4.0

Generated: 2026-05-22 23:45 Europe/Prague

## Recommendation

Sprint 4.0 is unblocked. Rocky now has a real read-only TrainingPeaks planned-workout source through the TrainingPeaks Premium `.ics` calendar sync URL stored as a local secret.

Current recommendation: use `/Users/clawdbot/.openclaw/secrets/trainingpeaks-webcal-url` as the Sprint 4.1 v1 read path. Keep the community MCP, browser automation, scraping, and official API paths out of the default path unless separately approved and audited.

## Live Evidence

Read-only command run:

```bash
cd /Users/clawdbot/.openclaw/workspace
/Users/clawdbot/.openclaw/workspace/.venv/bin/python scripts/rocky_runtime_tools.py \
  trainingpeaks-read-path-check --json
```

Latest result:

- Apple Calendar subscribed TrainingPeaks feed: not found.
- Direct TrainingPeaks webcal secret file: configured at `/Users/clawdbot/.openclaw/secrets/trainingpeaks-webcal-url`.
- Secret permissions: parent directory `0700`, secret file `0600`.
- Read-path recommendation: `direct_ics_webcal_url_file`.
- `trainingpeaks-ics-preview` returned 11 normalized workouts for the 2026-05-22 through 2026-06-05 window.
- Starting 2026-05-25, preview returned 7 normalized workouts.
- Official TrainingPeaks API: not configured; public TrainingPeaks docs say API access is approved-developer only and not available for personal use.
- Community `trainingpeaks-mcp`: not installed; not trusted for Rocky yet because it exposes write tools by default.
- WHOOP timing evidence: available in `/Users/clawdbot/.openclaw/events.db`, 26 WHOOP events from 2026-04-28 20:40:20 UTC through 2026-05-22 05:33:21 UTC.

No calendar write was attempted. The webcal URL was not printed in command output, logs, or this document.

Sample normalized workouts from the real feed:

- 2026-05-25: `Bike: Recovery Miles 1.00hrs`, sport `bike`, confidence `low`, date-only.
- 2026-05-26: `Run: Recovery Run 60 min`, sport `run`, confidence `low`, date-only.
- 2026-05-27: `Run: Steady State Run 3x14min`, sport `run`, confidence `low`, date-only.
- 2026-05-28: `Run: Recovery Run 60 min`, sport `run`, confidence `low`, date-only.

The current feed mostly returns future workouts as all-day/date-only items. Sprint 4.1 must infer a conservative protected time window and keep low confidence visible in proposals.

## Source Ranking

1. Apple Calendar subscribed TrainingPeaks feed
   - Best if the feed is already subscribed into Apple Calendar.
   - No new secrets for Rocky.
   - Current status: not found.

2. Direct TrainingPeaks `.ics` webcal URL file
   - Best next option if TrainingPeaks Premium calendar sync is available.
   - The URL is a secret and must never be passed as a CLI argument or logged.
   - Current status: configured, permission-safe, and returning normalized workouts.

3. Official TrainingPeaks API
   - Technically strong, but not realistic for personal use unless approved API access exists.
   - Current status: not configured.

4. Community TrainingPeaks MCP
   - Useful candidate for later source audit.
   - Not acceptable as a default Sprint 4.1 path because it exposes broad write capabilities and uses cookie-derived auth.
   - Current status: not installed and not used.

5. Browser automation or scraping
   - Out of scope.
   - Requires explicit legal and technical approval.

## Parser Capability

Sprint 4.0 added a dependency-free `.ics` preview path that normalizes workouts into:

- `source`
- `source_ref`
- `date`
- `planned_start_local`
- `planned_end_local`
- `planned_duration_minutes`
- `title`
- `sport`
- `confidence`
- `warnings`
- `observed_at`

Supported now:

- timed workouts with `DTSTART` and `DTEND`
- timed workouts with `DTSTART` and `DURATION`
- untimed/all-day workouts as low-confidence date-only items
- folded `.ics` lines
- unsupported recurring workouts are skipped instead of guessed
- descriptions, cookies, tokens, passwords, auth-like fields, and raw webcal URLs are not returned

## Freshness And Staleness Risks

- TrainingPeaks calendar sync can lag by up to 24 hours.
- Calendar sync exposes a limited horizon: 5 days history and 14 days future workouts.
- Untimed workouts may appear as date-only notes; Sprint 4.1 should not auto-book those without conservative defaults or approval.
- WHOOP is useful for historical timing sanity checks, but it is not a source of planned workouts.

## Security Review

Sprint 4.0 obeyed these boundaries:

- no TrainingPeaks password prompt
- no cookie extraction
- no browser automation
- no MCP installation
- no TrainingPeaks write
- no Apple Calendar write
- no Mission Control, Hermes, Betty, cron, or LaunchAgent changes

The direct webcal path is now configured:

```bash
drwx------ /Users/clawdbot/.openclaw/secrets
-rw------- /Users/clawdbot/.openclaw/secrets/trainingpeaks-webcal-url
```

Validate with:

```bash
cd /Users/clawdbot/.openclaw/workspace
/Users/clawdbot/.openclaw/workspace/.venv/bin/python scripts/rocky_runtime_tools.py \
  trainingpeaks-ics-preview \
  --webcal-url-file /Users/clawdbot/.openclaw/secrets/trainingpeaks-webcal-url \
  --json
```

## Go/No-Go For Sprint 4.1

Go for Sprint 4.1.

Sprint 4.1 should build dry-run training block proposals from real TrainingPeaks `.ics` data. It must keep the existing calendar policy: Monday-Thursday only, no Friday/Saturday/Sunday proactive booking, no duplicate Rocky blocks, and no live calendar writes until a later approved sprint.
