'use strict';

const assert = require('node:assert/strict');
const { createHmac } = require('node:crypto');
const { readFileSync } = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');
const { gasPath } = require('./gas_source.cjs');

const source = readFileSync(path.join(__dirname, '..', 'apps-script',
  'studio-email-sender', 'Code.gs'), 'utf8');
const secret = 'studio-test-secret-at-least-thirty-two-bytes';
const governorSecret = 'a'.repeat(64);
const governorProject = path.join(__dirname, '..', 'apps-script', 'governor-page-api');
const governorCodeSource = readFileSync(gasPath(governorProject, 'Code'), 'utf8');
const governorEmailSource = readFileSync(gasPath(governorProject, 'StudioEmail'), 'utf8');
const email = 'operator@example.com';
const code = '004219';

function harness({ nowMs = Date.now() } = {}) {
  const sent = [];
  const logs = [];
  const cache = new Map();
  const properties = new Map([
    ['STUDIO_EMAIL_SENDER_SECRET', secret],
    ['STUDIO_OPERATOR_EMAILS', email + ',other@example.com'],
  ]);
  let locked = false;
  let nextLockDelayMs = 0;
  let nextCacheDelayMs = 0;
  const context = {
    Date: { now: () => nowMs },
    PropertiesService: {
      getScriptProperties: () => ({
        getProperty: (key) => properties.get(key) ?? null,
        setProperty: (key, value) => { assert.equal(locked, true); properties.set(key, value); },
      }),
    },
    Utilities: {
      Charset: { UTF_8: 'UTF-8' },
      computeHmacSha256Signature: (message, key) =>
        Array.from(createHmac('sha256', key).update(message, 'utf8').digest()),
    },
    LockService: {
      getScriptLock: () => ({
        waitLock: () => {
          assert.equal(locked, false);
          nowMs += nextLockDelayMs;
          nextLockDelayMs = 0;
          locked = true;
        },
        releaseLock: () => { assert.equal(locked, true); locked = false; },
      }),
    },
    CacheService: {
      getScriptCache: () => ({
        get: (key) => {
          assert.equal(locked, true);
          nowMs += nextCacheDelayMs;
          nextCacheDelayMs = 0;
          return cache.get(key) ?? null;
        },
        put: (key, value, ttl) => {
          assert.equal(locked, true);
          assert.equal(ttl, 300);
          cache.set(key, value);
        },
      }),
    },
    MailApp: {
      sendEmail: (...args) => sent.push(args),
    },
    ContentService: {
      MimeType: { JSON: 'json' },
      createTextOutput: (body) => ({
        body,
        setMimeType(type) { assert.equal(type, 'json'); return this; },
      }),
    },
    Logger: { log: (...args) => logs.push(args) },
    console: { log: (...args) => logs.push(args), error: (...args) => logs.push(args) },
  };
  vm.createContext(context);
  vm.runInContext(source, context, { filename: 'Code.gs' });
  return {
    sent, logs, cache,
    advanceSeconds: (seconds) => { nowMs += seconds * 1000; },
    setNextLockDelaySeconds: (seconds) => { nextLockDelayMs = seconds * 1000; },
    setNextCacheDelaySeconds: (seconds) => { nextCacheDelayMs = seconds * 1000; },
    post: (body) => JSON.parse(context.doPost({ postData: {
      contents: JSON.stringify(body),
    } }).body),
  };
}

function signedRequest(overrides = {}, signingSecret = secret) {
  const body = {
    timestamp: String(Math.floor(Date.now() / 1000)),
    nonce: '0123456789abcdef0123456789abcdef',
    email,
    code,
    ...overrides,
  };
  body.signature = createHmac('sha256', signingSecret)
    .update([body.timestamp, body.nonce, body.email, body.code].join('\n'))
    .digest('hex');
  return body;
}

function governorHarness({
  nowMs = Date.now(), remainingQuota = 100, enabled = 'on',
  allowlist = email + ',other@example.com', signingSecret = governorSecret,
  stateRaw, mailThrows = false, lockWorks = true,
} = {}) {
  const sent = [];
  const appendCalls = [];
  const propertyReads = [];
  const hmacCalls = [];
  const properties = new Map([
    ['GOVERNOR_PASS', 'governor-test-pass'],
    ['STUDIO_GOVERNOR_EMAIL_ENABLED', enabled],
    ['STUDIO_GOVERNOR_EMAIL_SECRET', signingSecret],
    ['STUDIO_GOVERNOR_OPERATOR_EMAILS', allowlist],
  ]);
  if (typeof stateRaw !== 'undefined') {
    properties.set('STUDIO_GOVERNOR_EMAIL_STATE_V1', stateRaw);
  }
  let locked = false;
  let nextLockDelayMs = 0;
  let quota = remainingQuota;
  let beforeMailSend = null;
  const context = {
    Date: { now: () => nowMs },
    PropertiesService: {
      getScriptProperties: () => ({
        getProperty: (key) => {
          propertyReads.push(key);
          return properties.get(key) ?? null;
        },
        setProperty: (key, value) => {
          assert.equal(locked, true, 'state is reserved under ScriptLock');
          properties.set(key, value);
        },
      }),
    },
    Utilities: {
      Charset: { UTF_8: 'UTF-8' },
      computeHmacSha256Signature: (message, key) => {
        hmacCalls.push(String(message));
        return Array.from(createHmac('sha256', String(key))
          .update(String(message), 'utf8').digest());
      },
    },
    LockService: {
      getScriptLock: () => ({
        tryLock: (timeout) => {
          assert.equal(timeout, 5000);
          assert.equal(locked, false);
          nowMs += nextLockDelayMs;
          nextLockDelayMs = 0;
          if (!lockWorks) return false;
          locked = true;
          return true;
        },
        releaseLock: () => {
          assert.equal(locked, true);
          locked = false;
        },
      }),
    },
    MailApp: {
      getRemainingDailyQuota: () => {
        assert.equal(locked, true, 'quota snapshot is reconciled under ScriptLock');
        return quota;
      },
      sendEmail: (...args) => {
        assert.equal(locked, false, 'mail delivery must not hold the shared ScriptLock');
        const hook = beforeMailSend;
        beforeMailSend = null;
        if (hook) hook();
        sent.push(args);
        quota = Math.max(0, quota - 1);
        if (mailThrows) throw new Error('ambiguous mail failure containing sensitive fixture data');
      },
    },
    ContentService: {
      MimeType: { JSON: 'json' },
      createTextOutput: (body) => ({
        body,
        setMimeType(type) { assert.equal(type, 'json'); return this; },
      }),
    },
    console: { log: () => {}, error: () => {} },
    Logger: { log: () => {} },
  };
  vm.createContext(context);
  vm.runInContext(governorEmailSource, context, { filename: 'StudioEmail.js' });
  vm.runInContext(governorCodeSource, context, { filename: 'Code.js' });
  context.appendRow_ = (...args) => {
    appendCalls.push(args);
    return { ok: true, row_id: 'fixture-row' };
  };

  const output = (value) => JSON.parse(value.body);
  const event = (body, { action, actions, raw } = {}) => {
    const e = {
      parameter: typeof action === 'undefined' ? {} : { action },
      postData: { contents: typeof raw === 'string' ? raw : JSON.stringify(body) },
    };
    if (typeof actions !== 'undefined') e.parameters = { action: actions };
    return e;
  };
  return {
    sent, appendCalls, propertyReads, hmacCalls, properties,
    nowSeconds: () => Math.floor(nowMs / 1000),
    remainingQuota: () => quota,
    consumeQuota: (count) => { quota = Math.max(0, quota - count); },
    setQuota: (value) => { quota = value; },
    advanceSeconds: (seconds) => { nowMs += seconds * 1000; },
    setNextLockDelaySeconds: (seconds) => { nextLockDelayMs = seconds * 1000; },
    setBeforeMailSend: (hook) => { beforeMailSend = hook; },
    post: (body, options = {}) => output(context.doPost(event(body, options))),
    get: ({ action, actions } = {}) => {
      const e = { parameter: typeof action === 'undefined' ? {} : { action } };
      if (typeof actions !== 'undefined') e.parameters = { action: actions };
      return output(context.doGet(e));
    },
  };
}

function governorRequest(app, overrides = {}) {
  return signedRequest({
    timestamp: String(app.nowSeconds()),
    ...overrides,
  }, governorSecret);
}

function governorSubjectHash(address) {
  return createHmac('sha256', governorSecret)
    .update('studio-email-subject-v1\n' + address, 'utf8').digest('hex');
}

test('valid signed request sends once, replay is refused, and nothing sensitive is logged', () => {
  const app = harness();
  const request = signedRequest();
  assert.deepEqual(app.post(request), { ok: true });
  assert.equal(app.sent.length, 1);
  assert.equal(app.sent[0][0], email);
  assert.match(app.sent[0][2], /004219/);
  assert.deepEqual(app.post(request), { ok: false });
  app.cache.clear(); // Simulate CacheService's documented early eviction.
  assert.deepEqual(app.post(request), { ok: false });
  assert.equal(app.sent.length, 1);
  const captured = JSON.stringify(app.logs);
  assert.equal(captured.includes(email), false);
  assert.equal(captured.includes(code), false);
});

test('replay expiring during lock wait cannot reuse an evicted cache nonce', () => {
  const initialSeconds = Math.floor(Date.now() / 1000);
  const app = harness({ nowMs: initialSeconds * 1000 });
  const request = signedRequest({ timestamp: String(initialSeconds) });
  assert.deepEqual(app.post(request), { ok: true });
  assert.equal(app.sent.length, 1);

  app.advanceSeconds(119);
  app.cache.clear(); // CacheService may evict before the requested TTL.
  app.setNextLockDelaySeconds(5);
  assert.deepEqual(app.post(request), { ok: false });
  assert.equal(app.sent.length, 1);
});

test('slow cache read cannot make a replay prune its nonce reservation', () => {
  const initialSeconds = Math.floor(Date.now() / 1000);
  const app = harness({ nowMs: initialSeconds * 1000 });
  const request = signedRequest({ timestamp: String(initialSeconds) });
  assert.deepEqual(app.post(request), { ok: true });
  assert.equal(app.sent.length, 1);

  app.advanceSeconds(119);
  app.cache.clear();
  app.setNextCacheDelaySeconds(13);
  assert.deepEqual(app.post(request), { ok: false });
  assert.equal(app.sent.length, 1);
});

test('invalid HMAC, expired timestamp, and unknown email never send', () => {
  const app = harness();
  const badHmac = signedRequest();
  badHmac.signature = '0'.repeat(64);
  assert.deepEqual(app.post(badHmac), { ok: false });
  assert.deepEqual(app.post(signedRequest({
    timestamp: String(Math.floor(Date.now() / 1000) - 121),
  })), { ok: false });
  assert.deepEqual(app.post(signedRequest({ email: 'outsider@example.com' })), { ok: false });
  assert.equal(app.sent.length, 0);
});

test('shape and type checks reject extra fields, numeric codes, and unsafe nonces', () => {
  const app = harness();
  assert.deepEqual(app.post({ ...signedRequest(), extra: 'x' }), { ok: false });
  assert.deepEqual(app.post({ ...signedRequest(), code: 4219 }), { ok: false });
  assert.deepEqual(app.post(signedRequest({ nonce: '../unsafe' })), { ok: false });
  assert.equal(app.sent.length, 0);
});

test('Governor dispatcher sends signed mail without reading passphrase or touching the board', () => {
  const app = governorHarness();
  const request = governorRequest(app);
  assert.deepEqual(app.post(request, {
    action: 'studio-email', actions: ['studio-email'],
  }), { ok: true });
  assert.equal(app.sent.length, 1);
  assert.equal(app.sent[0][0], email);
  assert.match(app.sent[0][2], /004219/);
  assert.equal(app.appendCalls.length, 0);
  assert.equal(app.propertyReads.includes('GOVERNOR_PASS'), false);

  const state = JSON.parse(app.properties.get('STUDIO_GOVERNOR_EMAIL_STATE_V1'));
  assert.equal(state.version, 1);
  assert.equal(state.attempts.length, 1);
  assert.equal(state.quotaFence.pending.length, 1);
  assert.equal(JSON.stringify(state).includes(email), false);
  assert.equal(JSON.stringify(state).includes(code), false);
  assert.deepEqual(app.post(request, {
    action: 'studio-email', actions: ['studio-email'],
  }), { ok: false });
  assert.equal(app.sent.length, 1);
});

test('Governor route is exact, rejects duplicate action values, and never falls through', () => {
  const app = governorHarness();
  const request = governorRequest(app);

  assert.deepEqual(app.get({
    action: 'studio-email', actions: ['studio-email'],
  }), { ok: false });
  assert.equal(app.propertyReads.length, 0, 'GET probe does not read Script Properties');

  assert.deepEqual(app.post(request, {
    action: 'studio-email', actions: ['studio-email', 'say'],
  }), { ok: false });
  assert.deepEqual(app.post({ ...request, pass: 'governor-test-pass' }, {
    action: 'studio-email', actions: ['studio-email'],
  }), { ok: false });
  assert.equal(app.sent.length, 0);
  assert.equal(app.appendCalls.length, 0);

  const noAction = app.post(request);
  assert.equal(noAction.ok, false);
  assert.equal(noAction.error, 'bad passphrase');
  assert.equal(app.sent.length, 0);

  assert.deepEqual(app.post({
    pass: 'governor-test-pass', payload: 'BCB|v=1|id=fixture', source: 'fixture',
  }), { ok: true, row_id: 'fixture-row' });
  assert.equal(app.appendCalls.length, 1, 'legacy passphrase append remains intact');
});

test('Governor adapter is default-off and fails closed on shape, signature, quota and lock errors', () => {
  const disabled = governorHarness({ enabled: 'off' });
  assert.deepEqual(disabled.post(governorRequest(disabled), {
    action: 'studio-email', actions: ['studio-email'],
  }), { ok: false });

  const exhausted = governorHarness({ remainingQuota: 12 });
  assert.deepEqual(exhausted.post(governorRequest(exhausted), {
    action: 'studio-email', actions: ['studio-email'],
  }), { ok: false });

  const noLock = governorHarness({ lockWorks: false });
  assert.deepEqual(noLock.post(governorRequest(noLock), {
    action: 'studio-email', actions: ['studio-email'],
  }), { ok: false });

  const malformed = governorHarness();
  const bad = governorRequest(malformed);
  bad.signature = '0'.repeat(64);
  assert.deepEqual(malformed.post(bad, {
    action: 'studio-email', actions: ['studio-email'],
  }), { ok: false });
  assert.deepEqual(malformed.post({}, {
    action: 'studio-email', actions: ['studio-email'], raw: 'x'.repeat(2049),
  }), { ok: false });

  for (const app of [disabled, exhausted, noLock, malformed]) {
    assert.equal(app.sent.length, 0);
    assert.equal(app.appendCalls.length, 0);
  }
});

test('Governor authenticates invalid signatures before recipient membership work', () => {
  const allowed = governorHarness();
  const allowedRequest = governorRequest(allowed);
  allowedRequest.signature = '0'.repeat(64);
  assert.deepEqual(allowed.post(allowedRequest, {
    action: 'studio-email', actions: ['studio-email'],
  }), { ok: false });

  const unlisted = governorHarness();
  const unlistedRequest = governorRequest(unlisted, { email: 'missing@example.com' });
  unlistedRequest.signature = '0'.repeat(64);
  assert.deepEqual(unlisted.post(unlistedRequest, {
    action: 'studio-email', actions: ['studio-email'],
  }), { ok: false });

  assert.equal(allowed.hmacCalls.length, 1);
  assert.equal(unlisted.hmacCalls.length, 1);
  assert.deepEqual(allowed.propertyReads, unlisted.propertyReads);
  assert.equal(allowed.propertyReads.includes('STUDIO_GOVERNOR_OPERATOR_EMAILS'), false);
  assert.equal(allowed.sent.length + unlisted.sent.length, 0);
});

test('Governor quota fence serializes overlapping sends without holding the provider lock', () => {
  const app = governorHarness({ remainingQuota: 13 });
  let overlapping;
  app.setBeforeMailSend(() => {
    overlapping = app.post(governorRequest(app, { nonce: '1'.repeat(32) }), {
      action: 'studio-email', actions: ['studio-email'],
    });
  });

  assert.deepEqual(app.post(governorRequest(app), {
    action: 'studio-email', actions: ['studio-email'],
  }), { ok: true });
  assert.deepEqual(overlapping, { ok: false });
  assert.equal(app.sent.length, 1);
  assert.equal(app.remainingQuota(), 12);
  const state = JSON.parse(app.properties.get('STUDIO_GOVERNOR_EMAIL_STATE_V1'));
  assert.equal(state.attempts.length, 1);
  assert.equal(state.quotaFence.pending.length, 1);
});

test('Governor quota fence never attributes account-wide quota changes to Studio', () => {
  const decreased = governorHarness({ remainingQuota: 14 });
  let afterMonitor;
  decreased.setBeforeMailSend(() => {
    decreased.consumeQuota(1); // Governor monitoring uses one recipient.
    afterMonitor = decreased.post(governorRequest(decreased, {
      nonce: '1'.repeat(32),
    }), { action: 'studio-email', actions: ['studio-email'] });
  });
  assert.deepEqual(decreased.post(governorRequest(decreased), {
    action: 'studio-email', actions: ['studio-email'],
  }), { ok: true });
  assert.deepEqual(afterMonitor, { ok: false });
  assert.equal(decreased.sent.length, 1);
  assert.equal(decreased.remainingQuota(), 12);

  const increased = governorHarness({ remainingQuota: 13 });
  let second;
  let third;
  increased.setBeforeMailSend(() => {
    increased.setQuota(14); // Simulate a reset or provider-side limit increase.
    second = increased.post(governorRequest(increased, {
      nonce: '2'.repeat(32),
    }), { action: 'studio-email', actions: ['studio-email'] });
    third = increased.post(governorRequest(increased, {
      nonce: '3'.repeat(32),
    }), { action: 'studio-email', actions: ['studio-email'] });
  });
  assert.deepEqual(increased.post(governorRequest(increased), {
    action: 'studio-email', actions: ['studio-email'],
  }), { ok: true });
  assert.deepEqual(second, { ok: true });
  assert.deepEqual(third, { ok: false });
  assert.equal(increased.sent.length, 2);
  assert.equal(increased.remainingQuota(), 12);
  const state = JSON.parse(increased.properties.get(
    'STUDIO_GOVERNOR_EMAIL_STATE_V1'));
  assert.equal(state.attempts.length, 2);
  assert.equal(state.quotaFence.pending.length, 2);
});

test('Governor reservation survives ambiguous mail failure and request expiry during lock wait', () => {
  const ambiguous = governorHarness({ mailThrows: true });
  const request = governorRequest(ambiguous);
  assert.deepEqual(ambiguous.post(request, {
    action: 'studio-email', actions: ['studio-email'],
  }), { ok: false });
  assert.equal(ambiguous.sent.length, 1, 'the provider was called once');
  assert.deepEqual(ambiguous.post(request, {
    action: 'studio-email', actions: ['studio-email'],
  }), { ok: false });
  assert.equal(ambiguous.sent.length, 1, 'ambiguous request is never resent');
  assert.equal(JSON.parse(ambiguous.properties.get(
    'STUDIO_GOVERNOR_EMAIL_STATE_V1')).attempts.length, 1);

  const delayed = governorHarness();
  const expiring = governorRequest(delayed);
  delayed.setNextLockDelaySeconds(121);
  assert.deepEqual(delayed.post(expiring, {
    action: 'studio-email', actions: ['studio-email'],
  }), { ok: false });
  assert.equal(delayed.sent.length, 0);
  assert.equal(delayed.properties.has('STUDIO_GOVERNOR_EMAIL_STATE_V1'), false);
});

test('Governor adapter enforces per-recipient and global rolling limits with bounded state', () => {
  const app = governorHarness();
  for (let i = 0; i < 3; i++) {
    assert.deepEqual(app.post(governorRequest(app, {
      nonce: i.toString(16).padStart(32, '0'),
    }), { action: 'studio-email', actions: ['studio-email'] }), { ok: true });
  }
  assert.deepEqual(app.post(governorRequest(app, {
    nonce: '3'.padStart(32, '0'),
  }), { action: 'studio-email', actions: ['studio-email'] }), { ok: false });
  app.advanceSeconds(901);
  assert.deepEqual(app.post(governorRequest(app, {
    nonce: '4'.padStart(32, '0'),
  }), { action: 'studio-email', actions: ['studio-email'] }), { ok: true });

  const now = Math.floor(Date.now() / 1000);
  const full = JSON.stringify({
    version: 1,
    quotaFence: { pending: [] },
    attempts: Array.from({ length: 20 }, (_, i) => ({
      nonce: (i + 20).toString(16).padStart(32, '0'),
      keyedSubjectHash: governorSubjectHash(i % 2 ? email : 'other@example.com'),
      acceptedAt: now - 30,
    })),
  });
  const capped = governorHarness({ nowMs: now * 1000, stateRaw: full });
  assert.deepEqual(capped.post(governorRequest(capped, {
    nonce: 'f'.repeat(32),
  }), { action: 'studio-email', actions: ['studio-email'] }), { ok: false });
  assert.equal(capped.sent.length, 0);

  const expired = JSON.stringify({
    version: 1,
    quotaFence: {
      pending: Array.from({ length: 20 }, (_, i) => ({
        nonce: (i + 40).toString(16).padStart(32, '0'),
        reservedAt: now - 86401,
      })),
    },
    attempts: Array.from({ length: 20 }, (_, i) => ({
      nonce: (i + 40).toString(16).padStart(32, '0'),
      keyedSubjectHash: governorSubjectHash(email),
      acceptedAt: now - 86401,
    })),
  });
  const reopened = governorHarness({ nowMs: now * 1000, stateRaw: expired });
  assert.deepEqual(reopened.post(governorRequest(reopened, {
    nonce: 'e'.repeat(32),
  }), { action: 'studio-email', actions: ['studio-email'] }), { ok: true });
  assert.equal(JSON.parse(reopened.properties.get(
    'STUDIO_GOVERNOR_EMAIL_STATE_V1')).attempts.length, 1);
});

test('Governor adapter refuses corrupt or oversized durable limiter state', () => {
  for (const stateRaw of ['not-json', 'x'.repeat(8501), JSON.stringify({
    version: 1,
    quotaFence: { pending: [] },
    attempts: [{ nonce: '../bad', keyedSubjectHash: '0'.repeat(64), acceptedAt: 1 }],
  }), JSON.stringify({
    version: 1,
    quotaFence: { pending: ['not-a-reservation'] },
    attempts: [],
  }), JSON.stringify({
    version: 1,
    quotaFence: { pending: [] },
    attempts: [{
      nonce: ['0'.repeat(32)],
      keyedSubjectHash: ['0'.repeat(64)],
      acceptedAt: 1,
    }],
  })]) {
    const app = governorHarness({ stateRaw });
    assert.deepEqual(app.post(governorRequest(app), {
      action: 'studio-email', actions: ['studio-email'],
    }), { ok: false });
    assert.equal(app.sent.length, 0);
    assert.equal(app.appendCalls.length, 0);
  }
});
