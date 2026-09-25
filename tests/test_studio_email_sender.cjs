'use strict';

const assert = require('node:assert/strict');
const { createHash, createHmac } = require('node:crypto');
const { readFileSync } = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const source = readFileSync(path.join(__dirname, '..', 'apps-script',
  'studio-email-sender', 'Code.gs'), 'utf8');
const secret = 'studio-test-secret-at-least-thirty-two-bytes';
const email = 'operator@example.com';
const code = '004219';

const signed = (bytes) => Array.from(bytes).map((b) => (b > 127 ? b - 256 : b));

function harness({ nowMs = Date.now(), extraProperties = [] } = {}) {
  const sent = [];
  const logs = [];
  const cache = new Map();
  const properties = new Map([
    ['STUDIO_EMAIL_SENDER_SECRET', secret],
    ['STUDIO_OPERATOR_EMAILS', email + ',other@example.com'],
    ...extraProperties,
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
      // Apps Script byte arrays are signed Java bytes.
      base64Decode: (text) => signed(Buffer.from(text, 'base64')),
      DigestAlgorithm: { SHA_256: 'SHA_256' },
      computeDigest: (algorithm, bytes) => {
        assert.equal(algorithm, 'SHA_256');
        return signed(createHash('sha256').update(Buffer.from(bytes.map((b) => b & 255))).digest());
      },
      newBlob: (bytes, type, name) => ({ bytes, type, name }),
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
    postRaw: (contents) => JSON.parse(context.doPost({ postData: { contents } }).body),
  };
}

function signedRequest(overrides = {}) {
  const body = {
    timestamp: String(Math.floor(Date.now() / 1000)),
    nonce: '0123456789abcdef0123456789abcdef',
    email,
    code,
    ...overrides,
  };
  body.signature = createHmac('sha256', secret)
    .update([body.timestamp, body.nonce, body.email, body.code].join('\n'))
    .digest('hex');
  return body;
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

const pdf = Buffer.from('%PDF-1.4\nSFDC24 working session\n%%EOF\n', 'latin1');

function signedSummary(overrides = {}, bytes = pdf) {
  const body = {
    kind: 'summary',
    timestamp: String(Math.floor(Date.now() / 1000)),
    nonce: 'fedcba9876543210fedcba9876543210',
    email,
    pdf: bytes.toString('base64'),
    pdf_sha256: createHash('sha256').update(bytes).digest('hex'),
    ...overrides,
  };
  body.signature = createHmac('sha256', secret)
    .update(['summary', body.timestamp, body.nonce, body.email, body.pdf_sha256].join('\n'))
    .digest('hex');
  return body;
}

test('a signed summary sends the exact PDF once, as an attachment, and a replay is refused', () => {
  const app = harness();
  const request = signedSummary();
  assert.deepEqual(app.post(request), { ok: true });
  assert.equal(app.sent.length, 1);
  const [to, subject, text, options] = app.sent[0];
  assert.equal(to, email);
  assert.match(subject, /working session/);
  assert.equal(text.includes(request.pdf), false);
  assert.equal(options.attachments.length, 1);
  const blob = options.attachments[0];
  assert.equal(blob.type, 'application/pdf');
  assert.deepEqual(Buffer.from(blob.bytes.map((b) => b & 255)), pdf);
  assert.deepEqual(app.post(request), { ok: false });
  assert.equal(app.sent.length, 1);
  const captured = JSON.stringify(app.logs);
  assert.equal(captured.includes(email), false);
});

test('a summary whose PDF does not match its signed digest never sends', () => {
  const app = harness();
  const request = signedSummary();
  request.pdf = Buffer.from('%PDF-1.4\nsomething else\n', 'latin1').toString('base64');
  assert.deepEqual(app.post(request), { ok: false });
  const notPdf = Buffer.from('<html>not a pdf</html>', 'latin1');
  assert.deepEqual(app.post(signedSummary({ nonce: '1'.repeat(32) }, notPdf)), { ok: false });
  assert.equal(app.sent.length, 0);
});

test('a summary signature cannot be replayed as a sign-in code and the other way round', () => {
  const app = harness();
  const code = signedRequest();
  assert.deepEqual(app.post({ ...signedSummary(), signature: code.signature }), { ok: false });
  const summary = signedSummary();
  assert.deepEqual(app.post({ ...code, signature: summary.signature }), { ok: false });
  assert.equal(app.sent.length, 0);
});

test('a summary to an address off the allowlist needs the owner to open it', () => {
  const closed = harness();
  assert.deepEqual(closed.post(signedSummary({ email: 'visitor@bakery.example' })), { ok: false });
  assert.equal(closed.sent.length, 0);
  const open = harness({ extraProperties: [['STUDIO_SUMMARY_ANY_RECIPIENT', 'true']] });
  assert.deepEqual(open.post(signedSummary({ email: 'visitor@bakery.example' })), { ok: true });
  assert.equal(open.sent.length, 1);
  // Opening summaries never opens the sign-in code to outsiders.
  assert.deepEqual(open.post(signedRequest({ email: 'visitor@bakery.example' })), { ok: false });
  assert.equal(open.sent.length, 1);
});

test('summary shape checks: extra fields, bad base64, oversized sign-in bodies', () => {
  const app = harness();
  assert.deepEqual(app.post({ ...signedSummary(), extra: 'x' }), { ok: false });
  assert.deepEqual(app.post(signedSummary({ pdf: 'not base64!' })), { ok: false });
  assert.deepEqual(app.post({ ...signedRequest(), filler: 'x'.repeat(3000) }), { ok: false });
  // A valid sign-in request padded past 2048 characters is still refused: the
  // larger limit belongs to summaries only.
  const padded = JSON.stringify(signedRequest()) + ' '.repeat(3000);
  assert.deepEqual(app.postRaw(padded), { ok: false });
  assert.equal(app.sent.length, 0);
  assert.deepEqual(app.postRaw(JSON.stringify(signedRequest({ nonce: 'a'.repeat(32) }))), { ok: true });
});

test('client workspace addresses get the sign-in code and their summary; nobody else does', () => {
  const clients = [['STUDIO_CLIENT_EMAILS', 'client@example.com, second.client@example.com']];
  const app = harness({ extraProperties: clients });
  assert.deepEqual(app.post(signedRequest({ email: 'client@example.com' })), { ok: true });
  assert.deepEqual(app.post(signedSummary({ email: 'second.client@example.com',
    nonce: 'fedcba9876543210fedcba9876543210' })), { ok: true });
  assert.deepEqual(app.post(signedRequest({ email: 'outsider@example.com',
    nonce: '11111111111111111111111111111111' })), { ok: false });
  assert.equal(app.sent.length, 2);
  // Without the property, the same client address is refused.
  const closed = harness();
  assert.deepEqual(closed.post(signedRequest({ email: 'client@example.com' })), { ok: false });
  assert.equal(closed.sent.length, 0);
});

test('one malformed client address refuses every request, like the operator list', () => {
  const app = harness({ extraProperties: [['STUDIO_CLIENT_EMAILS', 'client@example.com,Not An Email']] });
  assert.deepEqual(app.post(signedRequest()), { ok: false });
  assert.equal(app.sent.length, 0);
});
