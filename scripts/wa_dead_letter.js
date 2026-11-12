// Dead-letter queue and P0 alert for the WhatsApp gateway (Case 00001052, G16).
// Records failed turns with a retry count. Posts a P0 row on the first DLQ
// entry only. P5: no secrets and no raw message prose in records, logs or
// P0 rows. Env var names only; values never appear in this file.
//
// This module is also inlined into the Pipedream steps via the test loader.
// Pipedream production can bind the same get/set store and postP0 hook.

const DLQ_META = 'dlq:__meta__';

function dlqKey(wamid) {
  return 'dlq:' + wamid;
}

function sanitizeForLog(value, prose) {
  let msg = String((value && value.message) || value || '');
  msg = msg.replace(/Bearer\s+\S+/gi, 'Bearer [redacted]');
  msg = msg.replace(/(api[_-]?key|secret|token|password|authorization)[=:]\s*\S+/gi, '$1=[redacted]');
  msg = msg.replace(/\bsk-[A-Za-z0-9_-]+/g, '[redacted]');
  msg = msg.replace(/\bAIza[A-Za-z0-9_-]+/g, '[redacted]');
  for (const raw of prose || []) {
    const snippet = String(raw || '');
    if (snippet.trim().length < 3) continue;
    const escaped = snippet.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
    msg = msg.replace(new RegExp(escaped, 'g'), '[redacted]');
  }
  msg = msg.replace(/\|/g, '/').replace(/\s+/g, ' ').trim().slice(0, 200);
  return msg || 'failed turn';
}

function safeLog(log, fields) {
  const allowed = ['wamid', 'kind', 'status', 'retryCount', 'lane', 'error', 'event'];
  const row = {};
  for (const key of allowed) {
    if (fields[key] != null && fields[key] !== '') {
      row[key] = key === 'error' ? sanitizeForLog(fields[key]) : fields[key];
    }
  }
  const line = Object.keys(row).map(k => k + '=' + row[k]).join(' ');
  if (typeof log === 'function') log(line);
  return line;
}

function p0Payload(record) {
  return [
    'P0',
    'v=1',
    'phase=P0',
    'class=ALERT',
    'source=whatsapp-gateway',
    'kind=dlq_first',
    'wamid=' + record.wamid,
    'retry_count=' + record.retryCount,
    'status=' + record.status,
    'error=' + sanitizeForLog(record.error),
  ].join('|');
}

async function enqueueDeadLetter(store, entry) {
  const record = {
    wamid: entry.wamid,
    kind: entry.kind,
    status: entry.status,
    lane: entry.lane || null,
    error: sanitizeForLog(entry.error, entry.prose),
    retryCount: 1,
    failedAt: entry.failedAt || new Date().toISOString(),
    stored: false,
    firstEntry: false,
  };
  if (!store || typeof store.get !== 'function' || typeof store.set !== 'function') {
    return record;
  }
  const key = dlqKey(record.wamid);
  const prev = await store.get(key);
  record.retryCount = ((prev && prev.retryCount) || 0) + 1;
  const meta = (await store.get(DLQ_META)) || { entries: 0, p0Posted: false };
  record.firstEntry = meta.entries === 0;
  await store.set(key, {
    wamid: record.wamid,
    kind: record.kind,
    status: record.status,
    lane: record.lane,
    error: record.error,
    retryCount: record.retryCount,
    failedAt: record.failedAt,
  });
  if (!prev) meta.entries += 1;
  if (record.firstEntry) meta.p0Posted = true;
  await store.set(DLQ_META, meta);
  record.stored = true;
  return record;
}

const api = {
  DLQ_META, dlqKey, sanitizeForLog, safeLog, p0Payload, enqueueDeadLetter,
};

if (typeof module !== 'undefined' && module.exports) {
  module.exports = api;
} else if (typeof globalThis !== 'undefined') {
  Object.assign(globalThis, api);
}

