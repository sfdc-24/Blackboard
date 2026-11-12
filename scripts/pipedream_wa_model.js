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

function isTimeout(err) {
  if (!err || typeof err !== 'object') return false;
  if (err.code === 'MODEL_TIMEOUT' || err.name === 'TimeoutError') return true;
  if (err.name === 'AbortError') return true;
  return false;
}

function safeError(err) {
  let msg = String((err && err.message) || err || 'model turn failed');
  msg = msg.replace(/Bearer\s+\S+/gi, 'Bearer [redacted]');
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

function andonPayload({ kind, status, job, error }) {
  return [
    'ANDON',
    'v=1',
    'phase=ANDON',
    'source=whatsapp-gateway',
    'kind=' + kind,
    'status=' + status,
    'wamid=' + job.wamid,
    'lane=' + laneOf(job),
    'error=' + safeError(error),
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
  },
  async run({ $ }) {
    const job = this.job;
    assertJob(job);
    const callModel = typeof this.callModel === 'function' ? this.callModel : defaultCallModel;
    const postAndon = typeof this.postAndon === 'function' ? this.postAndon : defaultPostAndon;

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
        await postAndon(andonPayload({ kind, status, job, error: err }));
        andonPosted = true;
      } catch {
        andonPosted = false;
      }
      if (typeof $?.respond === 'function') {
        await $.respond({ status, body: timeout ? 'model turn timed out' : 'model turn failed' });
      }
      const failure = new Error(`model turn failed (${status}): ${safeError(err)}`);
      failure.status = status;
      failure.andonPosted = andonPosted;
      throw failure;
    }

    if (typeof $?.respond === 'function') {
      await $.respond({ status: 200, body: 'ok' });
    }
    return { status: 200, modelText };
  },
});
