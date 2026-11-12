// Model-turn step for the WhatsApp gateway.
// Success stays HTTP 200 and returns the model text for the reply step.
// A model error returns 502 and posts an ANDON row; a timeout returns 504
// and posts an ANDON row. Board writes go through an injectable postAndon
// hook (default: the same ALPHA_URL append the inbound step uses). Tests
// inject the hook and must not perform real network I/O.
//
// This step reports the model-turn HTTP status. It is not the Meta webhook
// ACK; inbound already acknowledges deliveries separately.

const DEFAULT_TIMEOUT_MS = 30000;

// G16 dead-letter helpers (keep in sync with scripts/wa_dead_letter.js).
// Inlined so this Pipedream step is a single file. Tests inject the module.
const _DLQ_META = 'dlq:__meta__';
function _sanitizeForLog(value, prose) {
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
function _safeLog(log, fields) {
  const allowed = ['wamid', 'kind', 'status', 'retryCount', 'lane', 'error', 'event'];
  const row = {};
  for (const key of allowed) {
    if (fields[key] != null && fields[key] !== '') {
      row[key] = key === 'error' ? _sanitizeForLog(fields[key]) : fields[key];
    }
  }
  const line = Object.keys(row).map(k => k + '=' + row[k]).join(' ');
  if (typeof log === 'function') log(line);
  return line;
}
function _p0Payload(record) {
  return [
    'P0', 'v=1', 'phase=P0', 'class=ALERT', 'source=whatsapp-gateway', 'kind=dlq_first',
    'wamid=' + record.wamid, 'retry_count=' + record.retryCount, 'status=' + record.status,
    'error=' + _sanitizeForLog(record.error),
  ].join('|');
}
async function _enqueueDeadLetter(store, entry) {
  const record = {
    wamid: entry.wamid, kind: entry.kind, status: entry.status, lane: entry.lane || null,
    error: _sanitizeForLog(entry.error, entry.prose), retryCount: 1,
    failedAt: entry.failedAt || new Date().toISOString(), stored: false, firstEntry: false,
  };
  if (!store || typeof store.get !== 'function' || typeof store.set !== 'function') return record;
  const key = 'dlq:' + record.wamid;
  const prev = await store.get(key);
  record.retryCount = ((prev && prev.retryCount) || 0) + 1;
  const meta = (await store.get(_DLQ_META)) || { entries: 0, p0Posted: false };
  record.firstEntry = meta.entries === 0;
  await store.set(key, {
    wamid: record.wamid, kind: record.kind, status: record.status, lane: record.lane,
    error: record.error, retryCount: record.retryCount, failedAt: record.failedAt,
  });
  if (!prev) meta.entries += 1;
  if (record.firstEntry) meta.p0Posted = true;
  await store.set(_DLQ_META, meta);
  record.stored = true;
  return record;
}


function isTimeout(err) {
  if (!err || typeof err !== 'object') return false;
  if (err.code === 'MODEL_TIMEOUT' || err.name === 'TimeoutError') return true;
  if (err.name === 'AbortError') return true;
  return false;
}

function safeError(err, prose) {
  if (typeof sanitizeForLog === 'function') return sanitizeForLog(err, prose);
  let msg = String((err && err.message) || err || 'model turn failed');
  msg = msg.replace(/Bearer\s+\S+/gi, 'Bearer [redacted]');
  msg = msg.replace(/(api[_-]?key|secret|token|password|authorization)[=:]\s*\S+/gi, '$1=[redacted]');
  msg = msg.replace(/\bsk-[A-Za-z0-9_-]+/g, '[redacted]');
  msg = msg.replace(/\|/g, '/').replace(/\s+/g, ' ').trim().slice(0, 200);
  return msg || 'model turn failed';
}

function laneOf(job) {
  const match = /^gateway\/(claude|gemini|chatgpt|meta)$/.exec(job?.responseIdentity?.instance || '');
  return match ? match[1] : null;
}

function assertJob(job) {
  const identity = job?.responseIdentity;
  if (job?.callModel !== true || !laneOf(job) || identity?.replyTo !== job.wamid ||
      !/^wamid\.[A-Za-z0-9+/=_-]{1,500}$/.test(job.wamid || '') ||
      typeof job.system !== 'string' || !job.system.trim() ||
      job.input == null || typeof job.input !== 'object') {
    throw new Error('invalid prepared gateway model job');
  }
}

function timeoutMs() {
  const raw = process.env.MODEL_TIMEOUT_MS;
  if (raw == null || raw === '') return DEFAULT_TIMEOUT_MS;
  const value = Number(raw);
  if (!Number.isInteger(value) || value < 1 || value > 120000) {
    throw new Error('MODEL_TIMEOUT_MS must be an integer timeout in milliseconds');
  }
  return value;
}

async function defaultCallModel(job, { signal }) {
  const url = process.env.MODEL_URL;
  if (!url) throw new Error('MODEL_URL is not configured');
  const headers = { 'Content-Type': 'application/json' };
  const token = process.env.MODEL_TOKEN;
  if (token) headers.Authorization = 'Bearer ' + token;
  const res = await fetch(url, {
    method: 'POST',
    headers,
    body: JSON.stringify({ system: job.system, input: job.input }),
    signal,
  });
  let data;
  try { data = await res.json(); }
  catch { throw new Error('model returned invalid JSON'); }
  if (!res.ok || data?.error) {
    throw new Error(String(data?.error?.message || data?.error || `model HTTP ${res.status}`));
  }
  const text = typeof data.text === 'string' ? data.text : '';
  if (!text.trim()) throw new Error('model returned no text');
  return text.trim();
}

async function defaultPostAndon(payload) {
  const url = process.env.ALPHA_URL;
  const secret = process.env.ALPHA_SECRET;
  if (!url || !secret) throw new Error('ALPHA_URL / ALPHA_SECRET env vars missing');
  const res = await fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      secret,
      source_tag: 'whatsapp',
      target_surface: 'Blackboard Alpha DB',
      action_type: 'APPEND',
      payload,
    }),
    signal: AbortSignal.timeout(30000),
  });
  let reply;
  try { reply = await res.json(); }
  catch { throw new Error('ANDON append returned invalid JSON'); }
  if (!res.ok || reply?.result !== 'success' || reply.error ||
      typeof reply.rowId !== 'string' || !/^WRK-[A-Za-z0-9-]+$/.test(reply.rowId)) {
    throw new Error('ANDON append was not acknowledged');
  }
}

function andonPayload({ kind, status, job, error, prose }) {
  return [
    'ANDON',
    'v=1',
    'phase=ANDON',
    'source=whatsapp-gateway',
    'kind=' + kind,
    'status=' + status,
    'wamid=' + job.wamid,
    'lane=' + laneOf(job),
    'error=' + safeError(error, prose),
  ].join('|');
}

async function runTimed(callModel, job, ms) {
  const signal = AbortSignal.timeout(ms);
  const aborted = new Promise((_, reject) => {
    const fail = () => {
      const err = new Error('model turn timed out');
      err.code = 'MODEL_TIMEOUT';
      reject(signal.reason || err);
    };
    if (signal.aborted) {
      fail();
      return;
    }
    signal.addEventListener('abort', fail, { once: true });
  });
  try {
    return await Promise.race([Promise.resolve().then(() => callModel(job, { signal })), aborted]);
  } catch (err) {
    if (signal.aborted || isTimeout(err)) {
      const timeout = new Error('model turn timed out');
      timeout.code = 'MODEL_TIMEOUT';
      throw timeout;
    }
    throw err;
  }
}

export default defineComponent({
  props: {
    job: { type: 'object', description: 'One prepared callModel job from the context step' },
    // Optional injectable hooks. Production may omit them; the defaults use
    // fetch. Tests must inject postAndon (and usually callModel) so no real
    // network occurs.
    callModel: { type: 'object', optional: true, description: 'Optional model-call hook; defaults to MODEL_URL fetch' },
    postAndon: { type: 'object', optional: true, description: 'Optional ANDON hook; defaults to the board append' },
    postP0: { type: 'object', optional: true, description: 'Optional P0 hook; defaults to the board append' },
    deadLetterStore: { type: 'object', optional: true, description: 'Optional dead-letter get/set store' },
    log: { type: 'object', optional: true, description: 'Optional P5-safe logger' },
  },
  async run({ $ }) {
    const job = this.job;
    assertJob(job);
    const callModel = typeof this.callModel === 'function' ? this.callModel : defaultCallModel;
    const postAndon = typeof this.postAndon === 'function' ? this.postAndon : defaultPostAndon;
    const postP0 = typeof this.postP0 === 'function' ? this.postP0 : defaultPostAndon;
    const log = typeof this.log === 'function' ? this.log : () => {};
    const enqueue = typeof enqueueDeadLetter === 'function' ? enqueueDeadLetter : _enqueueDeadLetter;
    const p0 = typeof p0Payload === 'function' ? p0Payload : _p0Payload;
    const slog = typeof safeLog === 'function' ? safeLog : _safeLog;
    const prose = [];
    const msgText = job?.input?.message?.text;
    if (typeof msgText === 'string' && msgText.trim()) prose.push(msgText);

    let modelText;
    try {
      const text = await runTimed(callModel, job, timeoutMs());
      if (typeof text !== 'string' || !text.trim()) {
        throw new Error('model returned no text');
      }
      modelText = text.trim();
    } catch (err) {
      const timeout = isTimeout(err);
      const status = timeout ? 504 : 502;
      const kind = timeout ? 'model_timeout' : 'model_error';
      let andonPosted = false;
      try {
        await postAndon(andonPayload({ kind, status, job, error: err, prose }));
        andonPosted = true;
      } catch {
        andonPosted = false;
      }
      let dlq = null;
      let p0Posted = false;
      if (enqueue) {
        try {
          dlq = await enqueue(this.deadLetterStore, {
            wamid: job.wamid, kind, status, lane: laneOf(job), error: err, prose,
          });
          slog(log, { event: 'dlq', wamid: dlq.wamid, kind: dlq.kind, status: dlq.status,
            retryCount: dlq.retryCount, lane: dlq.lane, error: dlq.error });
          if (dlq.firstEntry && p0) {
            try {
              await postP0(p0(dlq));
              p0Posted = true;
            } catch {
              p0Posted = false;
            }
          }
        } catch {
          dlq = dlq || { stored: false };
        }
      }
      if (typeof $?.respond === 'function') {
        await $.respond({ status, body: timeout ? 'model turn timed out' : 'model turn failed' });
      }
      const failure = new Error(`model turn failed (${status}): ${safeError(err, prose)}`);
      failure.status = status;
      failure.andonPosted = andonPosted;
      failure.dlq = dlq;
      failure.p0Posted = p0Posted;
      throw failure;
    }

    if (typeof $?.respond === 'function') {
      await $.respond({ status: 200, body: 'ok' });
    }
    return { status: 200, modelText };
  },
});
