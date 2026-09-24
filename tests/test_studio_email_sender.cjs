'use strict';

const assert = require('node:assert/strict');
const { createHmac } = require('node:crypto');
const { readFileSync } = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const source = readFileSync(path.join(__dirname, '..', 'apps-script',
  'studio-email-sender', 'Code.gs'), 'utf8');
const secret = 'studio-test-secret-at-least-thirty-two-bytes';
const email = 'operator@example.com';
const code = '004219';

function harness() {
  const sent = [];
  const logs = [];
  const cache = new Map();
  const properties = new Map([
    ['STUDIO_EMAIL_SENDER_SECRET', secret],
    ['STUDIO_OPERATOR_EMAILS', email + ',other@example.com'],
  ]);
  let locked = false;
  const context = {
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
        waitLock: () => { assert.equal(locked, false); locked = true; },
        releaseLock: () => { assert.equal(locked, true); locked = false; },
      }),
    },
    CacheService: {
      getScriptCache: () => ({
        get: (key) => { assert.equal(locked, true); return cache.get(key) ?? null; },
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
    post: (body) => JSON.parse(context.doPost({ postData: {
      contents: JSON.stringify(body),
    } }).body),
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
