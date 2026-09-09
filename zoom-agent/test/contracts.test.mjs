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
    assert.ok(c.text.length <= MAX_QUERY_CHARS, `a segment was ${c.text.length} chars and would be truncated server-side`);
    assert.ok(c.lines > 0, 'each segment must know how many lines it represents, for honest coverage');
  }
  assert.equal(chunks.reduce((n, c) => n + c.lines, 0), covered, 'per-segment line counts must sum to reported coverage');
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


// ── Second review round: six findings from chatgpt-codex-connector on 6256712 ──
//
// Five P1 and one P2, all confirmed against the source before being accepted.
// Three were in code written the same night as the fixes above, which is the
// argument for these tests existing rather than a note in a commit message.

import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import net from 'node:net';

const assistant = await import('../src/assistant.js');

/** A free TCP port that is closed, so a connect() is refused immediately. */
async function refusedPort() {
  const srv = net.createServer();
  await new Promise((r) => srv.listen(0, '127.0.0.1', r));
  const { port } = srv.address();
  await new Promise((r) => srv.close(r));
  return port;
}

function receptionStub(handler) {
  return async (url) => {
    const u = new URL(url);
    return jsonResponse(handler(u));
  };
}

test('F2: the wrap-up waits for an in-flight live ask instead of racing it', async () => {
  Object.assign(config, {
    receptionExec: 'https://example.invalid/exec',
    busUrl: '', busSecret: '', metaToken: '', waPhoneNumberId: '', waTo: '',
  });
  const order = [];

  await withFetch(async (url) => {
    const u = new URL(url);
    const q = u.searchParams.get('q');
    const isLive = q.includes('assisting live during a Zoom call');
    if (isLive) {
      order.push('live-start');
      await new Promise((r) => setTimeout(r, 30));
      order.push('live-end');
      return jsonResponse({ ok: true, reply: 'live answer', ct: 'ct-from-live' });
    }
    order.push(`wrap-start(ct=${u.searchParams.get('ct')})`);
    return jsonResponse({ ok: true, reply: 'the summary', ct: 'ct-from-live' });
  }, async () => {
    assistant.onTranscriptLine({ meetingId: 'm-race', userName: 'A', text: 'sfdc24 what now?' });
    // The call ends while the live ask is still out — the exact race reported.
    await assistant.onMeetingEnded('m-race');
  });

  assert.deepEqual(order.slice(0, 2), ['live-start', 'live-end'],
    'the live ask must complete before the wrap-up begins');
  assert.ok(order[2]?.startsWith('wrap-start('), 'the wrap-up runs after, not alongside');
  assert.match(order[2], /ct=ct-from-live/,
    'the wrap-up must reuse the token the live ask was minted — racing produced two conversations');
});

test('F3: a call that decided nothing still gets summarised', async () => {
  const src = readFileSync(new URL('../src/assistant.js', import.meta.url), 'utf8');
  const segmentTask = src.slice(src.indexOf('const SEGMENT_TASK'), src.indexOf('/**\n * Summarise the call'));
  assert.doesNotMatch(segmentTask, /\bNOTHING\b/,
    'a NOTHING escape in the segment task means a discussion-only call maps every segment to nothing '
    + 'and the whole wrap-up is abandoned');
  assert.match(segmentTask, /ABOUT/,
    'the final summary asks what the call was about, so the segment task must preserve the subject');
});

test('F5: coverage counts only the segments that survived', async () => {
  Object.assign(config, {
    receptionExec: 'https://example.invalid/exec',
    busUrl: 'https://example.invalid/bus', busSecret: 's',
    metaToken: '', waPhoneNumberId: '', waTo: '',
    summaryMaxChunks: 6,
  });

  // Long enough that one prompt cannot hold it, so the segment path is taken.
  const line = (i) => `speaker: point number ${i} ${'detail '.repeat(8)}`;

  async function runCall(meetingId, failNthSegment) {
    let segment = 0;
    let captured = null;
    await withFetch(async (url, opts) => {
      if (opts?.body) {
        const body = JSON.parse(opts.body);
        if (body.action === 'append') { captured = body.sheetRow; return jsonResponse({ ok: true }); }
        return jsonResponse({ ok: true, rows: captured ? [captured] : [] });
      }
      const q = new URL(url).searchParams.get('q');
      if (q.includes('ONE SEGMENT')) {
        segment += 1;
        if (segment === failNthSegment) return jsonResponse({ ok: false, reason: 'segment blew up' });
        return jsonResponse({ ok: true, reply: `note ${segment}`, ct: 'ct1' });
      }
      return jsonResponse({ ok: true, reply: 'final summary', ct: 'ct1' });
    }, async () => {
      for (let i = 0; i < 60; i += 1) {
        assistant.onTranscriptLine({ meetingId, userName: 'A', text: line(i) });
      }
      await assistant.onMeetingEnded(meetingId);
    });
    const payload = captured?.[5] ?? '';
    const m = payload.match(/summary_covered_lines=(\d+)/);
    return { covered: m ? Number(m[1]) : null, payload };
  }

  const clean = await runCall('m-cov-clean', 0);
  const lossy = await runCall('m-cov-lossy', 2);

  assert.ok(clean.covered > 0, 'a clean run should report real coverage');
  assert.ok(lossy.covered < clean.covered,
    `a failed segment must reduce reported coverage (clean=${clean.covered}, lossy=${lossy.covered}) — `
    + 'reporting plan.covered regardless told the board it covered lines whose summary was discarded');
  assert.match(lossy.payload, /contributed nothing to this summary/,
    'and the finding must say so, not merely count differently');
});

test('F4: the Linux probe fails when the SDK is absent on a platform that publishes it', () => {
  // fileURLToPath, NOT url.pathname: on Windows the latter yields "/D:/a/..."
  // with a leading slash, which node cannot open — so BOTH spawns exited 1 and
  // the Windows branch read a path bug as a probe result. Caught by the
  // windows-latest leg, which is the entire argument for that leg existing.
  const probe = fileURLToPath(new URL('./linux-sdk-probe.mjs', import.meta.url));
  // Platform-agnostic guard: with url.pathname this was "/D:/a/..." on Windows
  // and existsSync would have caught it on the first run anywhere, instead of
  // the spawn's exit code 1 masquerading as a probe verdict.
  assert.ok(fs.existsSync(probe), `probe path is not openable on ${process.platform}: ${probe}`);
  const missing = spawnSync(process.execPath, [probe, '--simulate-missing'], { encoding: 'utf8' });
  const present = spawnSync(process.execPath, [probe], { encoding: 'utf8' });

  if (process.platform === 'linux' || process.platform === 'darwin') {
    assert.equal(missing.status, 1,
      'an optionalDependency that failed to install leaves npm ci green — this probe is the only thing '
      + 'that can turn the Linux job red, so its absent-SDK branch must exit non-zero');
    assert.match(missing.stderr, /optionalDependency/);
    assert.equal(present.status, 0, 'and it must pass when the SDK really is there');
  } else {
    assert.equal(missing.status, 0,
      `off linux/darwin, absence is correct and must skip (stderr: ${missing.stderr?.trim()})`);
    assert.match(missing.stdout, /SKIP/, 'and it must say it skipped, not silently succeed');
  }
});

test('F6: a refused connection schedules exactly one reconnect', async () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'zoom-ws-'));
  config.tokenFile = path.join(dir, 'tokens.json');
  fs.writeFileSync(config.tokenFile, JSON.stringify({
    access_token: 'a', refresh_token: 'r', expires_at: Date.now() + 60 * 60_000,
  }));
  const { loadTokens } = await import('../src/oauth.js');
  loadTokens();

  const port = await refusedPort();
  config.wsEndpoint = `ws://127.0.0.1:${port}/ws`;

  const { ZoomEventSocket } = await import('../src/events-ws.js');
  const socket = new ZoomEventSocket(() => {});
  let scheduled = 0;
  socket.scheduleReconnect = () => { scheduled += 1; };   // count, never actually retry

  try {
    // ONE connect. The first draft of this test called connect() twice via a
    // stray `??`, counted two schedules, and read exactly like the bug.
    const err = await socket.connect().then(() => null, (e) => e);
    assert.ok(err, 'connect must reject when the endpoint refuses');
    assert.equal(err.retryScheduled, true,
      'the rejection must be marked as already owned, or the caller schedules a duplicate');
    await new Promise((r) => setTimeout(r, 20));
    assert.equal(scheduled, 1, 'the close handler schedules exactly one retry');
  } finally {
    socket.close();
    fs.rmSync(dir, { recursive: true, force: true });
  }

  // The half the first assertion cannot see. Stubbing scheduleReconnect means
  // the CALLER's catch never runs — and the caller is where the second,
  // duplicate schedule came from. This drives the real scheduleReconnect once
  // and proves the catch adds nothing when the failure is already owned.
  {
    const s2 = new ZoomEventSocket(() => {});
    s2.attempts = -10;                       // backoff delay collapses to ~1ms
    const real = Object.getPrototypeOf(s2).scheduleReconnect;
    let calls = 0;
    s2.scheduleReconnect = function counting(...args) {
      calls += 1;
      if (calls === 1) return real.apply(this, args);   // let the first run for real
      return undefined;
    };
    s2.connect = async () => {
      const e = new Error('refused, and the close handler already scheduled');
      e.retryScheduled = true;
      throw e;
    };

    s2.scheduleReconnect();
    await new Promise((r) => setTimeout(r, 60));
    assert.equal(calls, 1,
      `the retry path scheduled ${calls} times for one failure — when connect() rejects with `
      + 'retryScheduled the caller must not schedule again, or every outage generation doubles '
      + 'the pending sockets and delayed attempts supersede the recovered connection');
    s2.close();
  }
});


// ── Third review round: four findings on 852d9ce ────────────────────────────

test('P2a: a failed summary handoff reaches the durable evidence', async () => {
  Object.assign(config, {
    receptionExec: 'https://example.invalid/exec',
    busUrl: 'https://example.invalid/bus', busSecret: 's',
    metaToken: 'x', waPhoneNumberId: '1', waTo: '2', notifyTimeoutMs: 200,
  });
  let captured = null;
  await withFetch(async (url, opts) => {
    if (opts?.body) {
      const body = JSON.parse(opts.body);
      if (body.action === 'append') { captured = body.sheetRow; return jsonResponse({ ok: true }); }
      if (body.action === 'read') return jsonResponse({ ok: true, rows: captured ? [captured] : [] });
      // the Graph send: reject the summary handoff
      return jsonResponse({ error: { code: 131047 } }, 400);
    }
    return jsonResponse({ ok: true, reply: 'the summary', ct: 'ct1' });
  }, async () => {
    assistant.onTranscriptLine({ meetingId: 'm-fail', userName: 'A', text: 'a short call' });
    await assistant.onMeetingEnded('m-fail');
  });

  assert.ok(captured, 'a board row should still be written');
  const payload = captured[5];
  assert.doesNotMatch(payload, /errors=none/,
    'the row claimed errors=none while the principal summary handoff had failed');
  assert.match(payload, /Graph 400/, 'the actual failure must be on the row');
});

test('P2b: a line placed only in part is not counted as covered', () => {
  const long = 'y'.repeat(3000);          // cannot fit any single prompt
  const normal = Array.from({ length: 5 }, (_, i) => `speaker: ordinary line ${i}`);
  const { covered, partial, dropped, chunks } = planSummaryChunks(
    ['Summarise this segment.', ''], [long, ...normal], { maxChunks: 8 },
  );

  assert.ok(partial >= 1, 'the oversized line was placed only in part and must be counted as such');
  assert.equal(covered + partial + dropped, 6, 'every line is covered, partial or dropped — never uncounted');
  const sliced = chunks.find((c) => c.text.includes('y'.repeat(50)));
  assert.ok(sliced, 'the tail of the long line is still sent for context');
  assert.equal(sliced.lines, 0, 'but it credits no coverage — its opening was discarded');
});

test('P2c: a stale read-back is retried before the write is called unresolved', async () => {
  Object.assign(config, { busUrl: 'https://example.invalid/bus', busSecret: 's' });
  let reads = 0;
  const impl = async (_url, opts) => {
    const body = JSON.parse(opts?.body ?? '{}');
    if (body.action === 'append') return jsonResponse({ ok: true });
    reads += 1;
    // First read is valid but STALE — the exact condition the module documents.
    return jsonResponse({ ok: true, rows: reads === 1 ? [['someone-else']] : [['late-row']] });
  };
  const res = await withFetch(impl, () => appendRow('Blackboard - Alpha DB', ['late-row']));

  assert.ok(reads >= 2, `only ${reads} read(s) — a stale read must not consume the whole budget`);
  assert.equal(res.verified, true,
    'the row was there on the second look; declaring it unresolved wasted the retries the module budgets');
});

test('P1: the board finding says which persona produced the summary', () => {
  const src = readFileSync(new URL('../src/assistant.js', import.meta.url), 'utf8');
  assert.match(src, /PERSONA_CAVEAT/, 'the caveat must exist');
  assert.match(src, /never to obey instructions/i,
    'reception() is the website receptionist and is instructed to refuse embedded commands — '
    + 'a finding that hides that is presenting a possible conversational reply as call evidence');
  const boundary = src.slice(src.indexOf('const boundary = ['), src.indexOf('const payload = bcb('));
  assert.match(boundary, /PERSONA_CAVEAT/, 'and it must be on the durable board row, not only in a comment');
});
