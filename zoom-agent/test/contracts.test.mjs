// The contract tests: one per accepted blocker, all offline.
//
// Every test here corresponds to a defect an independent reviewer found on a
// previous head. They exist so the defect cannot come back silently — a comment
// saying "we now send ct" is a claim; a test that fails when we stop sending it
// is a control.
//
// NO NETWORK, NO CREDENTIALS, ANY PLATFORM. `fetch` is stubbed per test and
// restored afterwards; nothing here reaches Zoom, Graph, Apps Script or the bus.

import { test } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { readFileSync, readdirSync } from 'node:fs';

process.env.ZOOM_CLIENT_ID ??= 'test-client-id';
process.env.ZOOM_CLIENT_SECRET ??= 'test-client-secret';
process.env.ZOOM_WS_ENDPOINT ??= 'wss://example.invalid/ws?subscriptionId=test';

const { config } = await import('../src/config.js');
const { buildBounded, planSummaryChunks, MAX_QUERY_CHARS } = await import('../src/prompt.js');
const { ask, newConversation } = await import('../src/reception.js');
const { appendRow } = await import('../src/board.js');
const { sendWhatsApp } = await import('../src/notify.js');

const realFetch = globalThis.fetch;
function withFetch(impl, fn) {
  globalThis.fetch = impl;
  return (async () => { try { return await fn(); } finally { globalThis.fetch = realFetch; } })();
}
const jsonResponse = (obj, status = 200) => ({
  ok: status >= 200 && status < 300,
  status,
  headers: new Map(),
  async text() { return JSON.stringify(obj); },
  async json() { return obj; },
});

// ── Blocker 1 — the 1,000-character contract ────────────────────────────────

test('blocker 1: a bounded prompt never exceeds what the backend accepts', () => {
  const header = ['Answer briefly.', ''];
  const body = Array.from({ length: 400 }, (_, i) => `speaker ${i}: ${'word '.repeat(12)}`);
  const { text, used, dropped } = buildBounded(header, body);

  assert.ok(text.length <= MAX_QUERY_CHARS, `prompt was ${text.length} chars`);
  assert.ok(used > 0, 'it should keep as much recent context as fits');
  assert.equal(used + dropped, body.length, 'every line is either used or counted as dropped');
  // The tail is what a live question is about; the head is what may be lost.
  assert.ok(text.includes('speaker 399'), 'the newest line must survive');
  assert.ok(!text.includes('speaker 0:'), 'the oldest lines are the ones dropped');
});

test('blocker 1: every summary segment fits, and coverage is counted honestly', () => {
  const body = Array.from({ length: 300 }, (_, i) => `speaker: line ${i} ${'x'.repeat(40)}`);
  const { chunks, covered, dropped } = planSummaryChunks(['Summarise this segment.', ''], body, { maxChunks: 4 });

  assert.ok(chunks.length > 0 && chunks.length <= 4, 'chunk count is bounded — each one costs money');
  for (const c of chunks) {
    assert.ok(c.length <= MAX_QUERY_CHARS, `a segment was ${c.length} chars and would be truncated server-side`);
  }
  assert.equal(covered + dropped, body.length, 'coverage plus loss must equal the call');
  assert.ok(dropped > 0, 'a 300-line call cannot fit four segments — that must be reported, not hidden');
});

test('blocker 1: an over-budget query is refused rather than silently truncated', async () => {
  config.receptionExec = 'https://example.invalid/exec';
  const res = await ask('x'.repeat(MAX_QUERY_CHARS + 1), newConversation());
  assert.equal(res.ok, false);
  assert.match(res.error, /truncates at 1000/, 'the error must name the contract it would have broken');
});

// ── Blocker 2 — signed conversation continuity ──────────────────────────────

test('blocker 2: the server-minted ct is kept and replaces the legacy vid', async () => {
  config.receptionExec = 'https://example.invalid/exec';
  const conv = newConversation();
  const seen = [];

  await withFetch(async (url) => {
    seen.push(new URL(url));
    return jsonResponse({ ok: true, reply: 'first', ct: 'server-token-1' });
  }, () => ask('hello', conv));

  assert.equal(conv.ct, 'server-token-1', 'the signed token must be persisted for the meeting');
  assert.equal(seen[0].searchParams.get('vid'), conv.legacyVid, 'the bounded legacy fallback applies to the first call only');

  await withFetch(async (url) => {
    seen.push(new URL(url));
    return jsonResponse({ ok: true, reply: 'second', ct: 'server-token-1' });
  }, () => ask('again', conv));

  assert.equal(seen[1].searchParams.get('ct'), 'server-token-1', 'later calls must carry ct');
  assert.equal(seen[1].searchParams.get('vid'), null,
    'once a ct exists, vid must never be sent again — it is a retired contract');
});

// ── Blocker 3 — acceptance is not delivery ──────────────────────────────────

test('blocker 3: a hung Graph call times out instead of silencing the assistant', async () => {
  Object.assign(config, { metaToken: 'x', waPhoneNumberId: '1', waTo: '2', notifyTimeoutMs: 50 });
  const res = await withFetch(
    (_url, opts) => new Promise((_resolve, reject) => {
      opts.signal.addEventListener('abort', () => {
        const err = new Error('aborted');
        err.name = 'AbortError';
        reject(err);
      });
    }),
    () => sendWhatsApp('hello'),
  );
  assert.equal(res.ok, false);
  assert.match(res.error, /did not answer within/, 'a hung send must surface as a timeout');
});

test('blocker 3: no source file claims delivery to a phone', () => {
  const dir = new URL('../src/', import.meta.url);
  for (const file of readdirSync(dir)) {
    if (!file.endsWith('.js')) continue;
    const src = readFileSync(new URL(file, dir), 'utf8');
    // The board finding and the console lines both said "delivered". Graph
    // returning a wamid means accepted; delivery arrives on a status webhook
    // this project does not yet receive.
    const claims = src.match(/^(?!\s*(\/\/|\*)).*delivered to (?:phone|WhatsApp)/gmi);
    assert.equal(claims, null, `${file} claims delivery it cannot evidence: ${claims?.join(' | ')}`);
  }
});

// ── Blocker 4 — D-4 read-back ───────────────────────────────────────────────

function busStub(rowsAfterAppend) {
  let appends = 0;
  const impl = async (_url, opts) => {
    const body = JSON.parse(opts?.body ?? '{}');
    if (body.action === 'append') {
      appends += 1;
      return jsonResponse({ ok: true });
    }
    return jsonResponse({ ok: true, rows: rowsAfterAppend });
  };
  return { impl, appends: () => appends };
}

test('blocker 4: a write is confirmed by reading the row back, not by the bus saying ok', async () => {
  Object.assign(config, { busUrl: 'https://example.invalid/bus', busSecret: 's' });
  const row = ['row-id-1', '2026-09-09T00:00:00Z', 'zoom-agent'];
  const stub = busStub([['other'], ['row-id-1'], ['another']]);

  const res = await withFetch(stub.impl, () => appendRow('Blackboard - Alpha DB', row));
  assert.equal(res.verified, true, 'exactly one matching Row_ID is what proof looks like');
  assert.equal(res.matches, 1);
  assert.equal(stub.appends(), 1, 'exactly one append attempt, always');
});

test('blocker 4: a missing row is unresolved, never a retry', async () => {
  Object.assign(config, { busUrl: 'https://example.invalid/bus', busSecret: 's' });
  const stub = busStub([['someone-else']]);
  const res = await withFetch(stub.impl, () => appendRow('Blackboard - Alpha DB', ['row-id-2']));

  assert.equal(res.verified, false);
  assert.equal(res.ambiguous, true, 'absence on a read that can be stale is not proof of failure');
  assert.match(res.error, /stale/i);
  assert.equal(stub.appends(), 1, 'an unverified append must NOT be retried — that is how duplicates land');
});

test('blocker 4: a duplicate Row_ID is reported, loudly', async () => {
  Object.assign(config, { busUrl: 'https://example.invalid/bus', busSecret: 's' });
  const stub = busStub([['dup'], ['dup']]);
  const res = await withFetch(stub.impl, () => appendRow('Blackboard - Alpha DB', ['dup']));
  assert.equal(res.verified, false);
  assert.equal(res.matches, 2);
  assert.match(res.error, /appears 2 times/);
});

test('blocker 4: a transport error still reads back instead of assuming failure', async () => {
  Object.assign(config, { busUrl: 'https://example.invalid/bus', busSecret: 's' });
  let appends = 0;
  const impl = async (_url, opts) => {
    const body = JSON.parse(opts?.body ?? '{}');
    if (body.action === 'append') {
      appends += 1;
      throw new Error('socket hang up');
    }
    return jsonResponse({ ok: true, rows: [['landed-anyway']] });
  };
  const res = await withFetch(impl, () => appendRow('Blackboard - Alpha DB', ['landed-anyway']));
  assert.equal(appends, 1);
  assert.equal(res.verified, true, 'the write DID land; an exception is not evidence of absence');
});

test('blocker 4: the board row reports a finished result as DONE, not OPEN', () => {
  const src = readFileSync(new URL('../src/assistant.js', import.meta.url), 'utf8');
  assert.match(src, /^\s*'DONE',/m, 'a completed RESULT must not sit in every reader’s open queue');
});

// ── Blocker 5 — loopback, state, and credential storage ─────────────────────

test('blocker 5: the authorize URL carries a one-time state nonce', async () => {
  const { authorizeUrl } = await import('../src/oauth.js');
  const a = new URL(authorizeUrl()).searchParams.get('state');
  const b = new URL(authorizeUrl()).searchParams.get('state');
  assert.ok(a && a.length >= 16, 'state must exist and be unguessable');
  assert.notEqual(a, b, 'each authorization gets its own nonce');
});

test('blocker 5: the callback listens on loopback and rejects an unbound code', async () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'zoom-oauth-'));
  config.tokenFile = path.join(dir, 'tokens.json');
  config.port = 0;

  const { startOAuthServer, authorizeUrl } = await import('../src/oauth.js');
  const server = startOAuthServer(() => {});
  await new Promise((r) => server.once('listening', r));
  const { address, port } = server.address();

  try {
    assert.match(address, /^(127\.0\.0\.1|::1)$/, `bound ${address} — the callback must not be on every interface`);

    const base = `http://127.0.0.1:${port}`;
    const forged = await realFetch(`${base}/oauth/callback?code=stolen&state=never-issued`);
    assert.equal(forged.status, 400, 'a code with no matching request must be refused');

    const state = new URL(authorizeUrl()).searchParams.get('state');
    const good = await withFetch(
      async () => jsonResponse({ access_token: 'a', refresh_token: 'r', expires_in: 3600 }),
      () => realFetch(`${base}/oauth/callback?code=real&state=${state}`),
    );
    assert.equal(good.status, 200, 'a properly bound callback completes');

    const replay = await realFetch(`${base}/oauth/callback?code=real&state=${state}`);
    assert.equal(replay.status, 410, 'the endpoint closes once authorization is done');

    const mode = fs.statSync(config.tokenFile).mode & 0o777;
    if (process.platform !== 'win32') {
      assert.equal(mode, 0o600, `refresh token stored with mode ${mode.toString(8)}`);
    }
    assert.equal(readdirSync(dir).filter((f) => f.endsWith('.tmp')).length, 0,
      'the atomic replace must leave no temp file behind');
  } finally {
    server.close();
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

// ── Acceptance items from the follow-up review ──────────────────────────────

test('acceptance: the token recycle asks for a token that outlives the next window', async () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'zoom-ttl-'));
  config.tokenFile = path.join(dir, 'tokens.json');
  // Ten minutes left: exactly the state the 50-minute recycle finds itself in.
  fs.writeFileSync(config.tokenFile, JSON.stringify({
    access_token: 'stale', refresh_token: 'r', expires_at: Date.now() + 10 * 60_000,
  }));

  const { getAccessToken, loadTokens } = await import('../src/oauth.js');
  loadTokens();
  try {
    const unchanged = await getAccessToken();
    assert.equal(unchanged, 'stale', 'the default horizon is satisfied — no refresh needed');

    const refreshed = await withFetch(
      async () => jsonResponse({ access_token: 'fresh', refresh_token: 'r2', expires_in: 3600 }),
      () => getAccessToken({ minTtlMs: 55 * 60_000 }),
    );
    assert.equal(refreshed, 'fresh',
      'asking for 55 minutes of life must actually refresh — the recycle reconnected with the same token before this');
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

test('acceptance: the declared Node floor matches what the locked SDK requires', () => {
  const pkg = JSON.parse(readFileSync(new URL('../package.json', import.meta.url), 'utf8'));
  const floor = Number(String(pkg.engines.node).replace(/[^\d.]/g, '').split('.')[0]);
  assert.ok(floor >= 22,
    `engines.node is ${pkg.engines.node}, but the locked @zoom/rtms needs Node >=22 — `
    + 'advertising a floor the dependency cannot run on sends operators to a broken install');
});

test('acceptance: no Express, so no qs advisory in a process holding a refresh token', () => {
  const pkg = JSON.parse(readFileSync(new URL('../package.json', import.meta.url), 'utf8'));
  assert.equal(pkg.dependencies?.express, undefined,
    'two routes did not justify a dependency tree carrying two moderate qs advisories');
});
