import fs from 'node:fs';
import path from 'node:path';

const DEFAULT_RULES_PATH = '/Users/clawdbot/.openclaw/email-security/rules.json';
const RISK_ORDER = ['low', 'medium', 'high', 'critical'];

function loadRules(rulesPath = DEFAULT_RULES_PATH) {
  return JSON.parse(fs.readFileSync(rulesPath, 'utf-8'));
}

function riskRank(level) {
  const idx = RISK_ORDER.indexOf(level);
  return idx === -1 ? 0 : idx;
}

function maxRisk(levels) {
  return levels.reduce((highest, level) => (
    riskRank(level) > riskRank(highest) ? level : highest
  ), 'low');
}

function clipMatch(value) {
  const s = String(value || '').replace(/\s+/g, ' ').trim();
  return s.length > 140 ? `${s.slice(0, 137)}...` : s;
}

function fieldsForScan(input) {
  const fields = [
    ['subject', input.subject || ''],
    ['body', input.body || ''],
  ];
  for (const att of input.attachments || []) {
    fields.push(['attachment_filename', att.filename || att.name || '']);
  }
  return fields;
}

export function scanEmail(input, options = {}) {
  const rules = options.rules || loadRules(options.rulesPath);
  const signals = [];

  for (const [location, value] of fieldsForScan(input)) {
    const text = String(value || '');
    if (!text) continue;
    for (const rule of rules.rules || []) {
      for (const pattern of rule.patterns || []) {
        const re = new RegExp(pattern, 'iu');
        const match = text.match(re);
        if (!match) continue;
        signals.push({
          code: rule.code,
          category: rule.category,
          severity: rule.severity,
          match: clipMatch(match[0]),
          location,
        });
        break;
      }
    }
  }

  const riskLevel = maxRisk(signals.map((s) => s.severity));
  const restricted = riskRank(riskLevel) >= riskRank('high');
  const safeSummary = signals.length
    ? `${riskLevel} email security risk: ${signals.map((s) => s.code).slice(0, 4).join(', ')}${signals.length > 4 ? ` +${signals.length - 4} more` : ''}`
    : 'low email security risk: no prompt-injection patterns detected';

  return {
    risk_level: riskLevel,
    signals,
    safe_summary: safeSummary,
    restrictions: {
      url_open_requires_approval: restricted,
      attachment_processing_requires_approval: restricted,
      memory_promotion_requires_approval: restricted,
    },
  };
}

export function appendAuditEvent(event, logPath = '/Users/clawdbot/.openclaw/logs/email-security.jsonl') {
  fs.mkdirSync(path.dirname(logPath), { recursive: true });
  fs.appendFileSync(logPath, `${JSON.stringify({ ts: new Date().toISOString(), ...event })}\n`);
}

export function formatRiskForPrompt(risk) {
  const lines = [
    'Email security assessment:',
    `- Risk: ${risk.risk_level}`,
    `- Summary: ${risk.safe_summary}`,
    `- Restrictions: URLs require approval=${risk.restrictions.url_open_requires_approval}; attachments require approval=${risk.restrictions.attachment_processing_requires_approval}; memory promotion requires approval=${risk.restrictions.memory_promotion_requires_approval}`,
  ];
  if (risk.signals.length) {
    lines.push('- Signals:');
    for (const sig of risk.signals.slice(0, 8)) {
      lines.push(`  - ${sig.code} (${sig.severity}, ${sig.location}): ${sig.match}`);
    }
  }
  return lines.join('\n');
}
