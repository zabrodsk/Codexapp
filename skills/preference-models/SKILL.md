---
name: preference-models
description: Maintain bounded Rocky preference models from observed outcomes and create review-only learning proposals for higher-risk behavior changes.
---

# Preference Models

Use this skill when Rocky updates or previews self-learning preferences.

## Contract

- Inputs: sanitized outcome observations from the outcome observer.
- Outputs: bounded preference models, evidence counts, confidence, and review-only proposals.
- Auto-apply allowed only for low-risk estimates: `email_triage.duration_multiplier` and `coding_focus.default_duration_minutes` after evidence and confidence thresholds are met.
- Review-only: training timing, reminder cadence, task completion patterns, and daily priority weights.
- Safety: learned preferences never override Friday/Saturday/Sunday booking rules, calendar conflict checks, ownership checks, notification policy, task completion rules, or security policy.

## Runtime

```bash
/Users/clawdbot/.openclaw/workspace/.venv/bin/python scripts/rocky_runtime_tools.py \
  assistant-learning-summary --json
```

## Weekly Review Boundary

Weekly review may summarize learning calibration and recommend preference adjustments. It must not activate review-only proposals or expand learning authority; bounded active preferences remain limited to the Sprint 10 rules.
