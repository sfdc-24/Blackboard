const { test } = require('node:test');
const assert = require('node:assert/strict');
const { createHash, createHmac } = require('node:crypto');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const ROOT = path.resolve(__dirname, '..');
const PROJECT = path.join(ROOT, 'apps-script', 'governor-page-api');
const AUTH = fs.readFileSync(path.join(PROJECT, 'Auth.gs'), 'utf8');
const CODE = fs.readFileSync(path.join(PROJECT, 'Code.gs'), 'utf8');
const INBOX = fs.readFileSync(path.join(PROJECT, 'PublicInbox.gs'), 'utf8');
const RECEPTION = fs.readFileSync(path.join(PROJECT, 'Reception.html'), 'utf8');
const VOICE = fs.readFileSync(path.join(ROOT, 'site', 'voice', 'index.html'), 'utf8');

function asBytes(value) {
  if (Buffer.isBuffer(value)) return value;
  if (Array.isArray(value)) return Buffer.from(value.map(n => (Number(n) + 256) % 256));
  return Buffer.from(String(value), 'utf8');
}

class ScriptCache {
  constructor() { this.values = new Map(); }
  get(key) { return this.values.has(key) ? this.values.get(key) : null; }
  put(key, value) { this.values.set(key, String(value)); }
  remove(key) { this.values.delete(key); }
}

function createHarness(initialProperties = {}) {
  const harness = {
    values: new Map(Object.entries({
      AUTH_SIGNING_SECRET: 'test-signing-key',
      ANTHROPIC_KEY: 'test-provider-key',
      ...initialProperties,
    })),
    cache: new ScriptCache(),
    lock: { held: false },
    providerCalls: 0,
    providerPayloads: [],
    inboxRows: [],
    sheetNames: [],
    propertyWritesUnderLock: 0,
    providerSawLock: false,
    onProviderFetch: null,
    failProvider: false,
    uuid: 0,
  };

  const properties = {
    getProperty(key) { return harness.values.has(key) ? harness.values.get(key) : null; },
    setProperty(key, value) { harness.values.set(key, String(value)); return this; },
    setProperties(values) {
      assert.equal(harness.lock.held, true, 'chat budget writes must hold the script lock');
      harness.propertyWritesUnderLock += 1;
      for (const [key, value] of Object.entries(values)) harness.values.set(key, String(value));
      return this;
    },
    deleteProperty(key) { harness.values.delete(key); return this; },
    getProperties() { return Object.fromEntries(harness.values); },
  };

  const lock = {
    waitLock() {
      if (harness.lock.held) throw new Error('mock lock already held');
      harness.lock.held = true;
    },
    releaseLock() { harness.lock.held = false; },
  };

  const inboxSheet = {
    appendRow(row) { harness.inboxRows.push(Array.from(row)); },
    setFrozenRows() {},
    getLastRow() { return harness.inboxRows.length; },
    getRange(row, column) {
      return { getValue: () => (harness.inboxRows[row - 1] || [])[column - 1] };
    },
  };
  const spreadsheet = {
    getSheetByName(name) {
      harness.sheetNames.push(String(name));
      return name === 'PUBLIC_INBOX' ? inboxSheet : null;
    },
    insertSheet(name) {
      harness.sheetNames.push(String(name));
      assert.equal(name, 'PUBLIC_INBOX');
      return inboxSheet;
    },
  };

  const utilities = {
    Charset: { UTF_8: 'UTF_8' },
    DigestAlgorithm: { SHA_256: 'SHA_256' },
    base64EncodeWebSafe: value => asBytes(value).toString('base64url'),
    base64DecodeWebSafe: value => Buffer.from(String(value), 'base64url'),
    base64Encode: value => asBytes(value).toString('base64'),
    computeDigest: (_algorithm, value) => Array.from(createHash('sha256').update(String(value)).digest())
      .map(byte => byte > 127 ? byte - 256 : byte),
    computeHmacSha256Signature: (message, key) => Array.from(createHmac('sha256', String(key)).update(String(message)).digest())
      .map(byte => byte > 127 ? byte - 256 : byte),
    formatDate: (date, _zone, pattern) => {
      const iso = new Date(date).toISOString();
      return pattern === 'yyyyMMdd' ? iso.slice(0, 10).replace(/-/g, '') : iso.slice(0, 10);
    },
    getUuid: () => {
      harness.uuid += 1;
      return '00000000-0000-4000-8000-' + harness.uuid.toString(16).padStart(12, '0');
    },
    newBlob: bytes => ({ getDataAsString: () => asBytes(bytes).toString('utf8') }),
  };

  const context = vm.createContext({
    Buffer,
    CacheService: { getScriptCache: () => harness.cache },
    ContentService: {
      MimeType: { JSON: 'application/json', JAVASCRIPT: 'text/javascript' },
      createTextOutput(text) { return { text, setMimeType() { return this; } }; },
    },
    LockService: { getScriptLock: () => lock },
    Logger: { log() {} },
    PropertiesService: { getScriptProperties: () => properties },
    Session: {
      getActiveUser() { throw new Error('reception must not inspect Governor identity'); },
      getEffectiveUser() { throw new Error('reception must not inspect Governor identity'); },
    },
    SpreadsheetApp: {
      openById(id) {
        assert.equal(id, '120_71KaF4JKGPGz0qUz4phqWRljSqEzSRRm_0zXC_oY');
        return spreadsheet;
      },
      flush() {},
    },
    Utilities: utilities,
    UrlFetchApp: {
      fetch(url, options) {
        assert.match(url, /^https:\/\/api\.anthropic\.com\/v1\/messages$/);
        harness.providerSawLock = harness.providerSawLock || harness.lock.held;
        harness.providerCalls += 1;
        harness.providerPayloads.push(JSON.parse(options.payload));
        if (harness.onProviderFetch) {
          const callback = harness.onProviderFetch;
          harness.onProviderFetch = null;
          callback();
        }
        if (harness.failProvider) throw new Error('mock provider failure');
        return {
          getResponseCode: () => 200,
          getContentText: () => JSON.stringify({
            content: [{ type: 'text', text: 'A bounded test reply.' }],
          }),
        };
      },
    },
  });

  vm.runInContext(AUTH, context, { filename: 'Auth.gs' });
  vm.runInContext(CODE, context, { filename: 'Code.gs' });
  vm.runInContext(INBOX, context, { filename: 'PublicInbox.gs' });
  harness.context = context;
  return harness;
}

function signedSession(context, sub, email = 'visitor@example.invalid') {
  return context.mintSession_({ sub, email, name: 'Visitor' });
}

function decode(output) { return JSON.parse(output.text); }

test('Google subject, not email or caller id, is the stable conversation boundary', () => {
  const h = createHarness();
  const alice = signedSession(h.context, 'google-subject-alice', 'alice@example.invalid');
  const renamedAlice = signedSession(h.context, 'google-subject-alice', 'new-address@example.invalid');
  const bob = signedSession(h.context, 'google-subject-bob', 'bob@example.invalid');

  const a1 = h.context.conversationIdentity_('', alice);
  const a2 = h.context.conversationIdentity_('', renamedAlice);
  const b1 = h.context.conversationIdentity_('', bob);
  assert.equal(a1.key, a2.key, 'changing email/client state must not rotate the Google identity');
  assert.notEqual(a1.key, b1.key);
  assert.equal(h.context.readConversation_(a1.token, h.context.readSession_(bob)), null);

  const tokenPayload = JSON.parse(Buffer.from(a1.token.split('.')[0], 'base64url').toString('utf8'));
  assert.deepEqual(Object.keys(tokenPayload).sort(), ['exp', 'id', 'k', 'p', 'v']);
  assert.equal(tokenPayload.p, 'blackboard.conversation.v1');
  assert.equal(JSON.stringify(tokenPayload).includes('google-subject-alice'), false);
  assert.equal(a1.key.includes('google-subject-alice'), false);
  assert.equal(h.context.readConversation_(alice, h.context.readSession_(alice)), null,
    'an auth-session token must not verify as a conversation token');
  assert.equal(h.context.readSession_(a1.token), null,
    'a conversation token must not verify as an auth-session token');
});

test('anonymous identity is a signed server nonce and forged tokens cannot select a key', () => {
  const h = createHarness();
  const first = h.context.conversationIdentity_('', '');
  const replay = h.context.conversationIdentity_(first.token, '');
  const second = h.context.conversationIdentity_('', '');
  assert.equal(first.key, replay.key);
  assert.notEqual(first.key, second.key);

  const forged = first.token.slice(0, -1) + (first.token.endsWith('a') ? 'b' : 'a');
  const replaced = h.context.conversationIdentity_(forged, '');
  assert.notEqual(replaced.key, first.key);
  assert.notEqual(replaced.token, forged);
});

test('a first anonymous voice turn bootstraps a signed token without trusting legacy vid', () => {
  const h = createHarness();
  const first = decode(h.context.voiceReply_({ vid: 'attacker-selected', q: 'hello' }));
  assert.equal(first.ok, true);
  assert.match(first.ct, /^[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$/);
  const resolved = h.context.conversationIdentity_(first.ct, '');
  assert.ok(h.cache.get('vh_' + resolved.key));
  assert.equal([...h.cache.values.keys()].some(key => key.includes('attacker-selected')), false);
  assert.equal(h.providerCalls, 1);
});

test('a vid-only legacy client cannot exceed the atomic whole-site ceiling', () => {
  const h = createHarness({ CHAT_DAILY_CAP: '2' });
  const first = decode(h.context.voiceReply_({ vid: 'legacy', q: 'one' }));
  const second = decode(h.context.voiceReply_({ vid: 'legacy', q: 'two' }));
  const third = decode(h.context.voiceReply_({ vid: 'legacy', q: 'three' }));
  assert.equal(first.ok, true);
  assert.equal(second.ok, true);
  assert.equal(third.degraded, 'daily-cap');
  assert.notEqual(first.ct, second.ct, 'old client discards ct, so each call is a new anonymous bearer');
  assert.equal(h.providerCalls, 2);
  assert.equal(JSON.parse(h.values.get('CHAT_BUDGET_V1')).daily, 2);
});

test('rotating or colliding legacy vid values neither resets the cap nor creates history keys', () => {
  const h = createHarness();
  const identity = h.context.conversationIdentity_('', '');

  for (let i = 0; i < 12; i += 1) {
    const result = decode(h.context.voiceReply_({
      ct: identity.token,
      vid: 'caller-rotated-' + i,
      q: 'turn ' + i,
    }));
    assert.equal(result.ok, true);
    assert.equal(result.ct, identity.token);
  }
  const capped = decode(h.context.voiceReply_({
    ct: identity.token,
    vid: 'one-more-rotation',
    q: 'turn thirteen',
  }));
  assert.equal(capped.degraded, 'session-cap');
  assert.equal(h.providerCalls, 12);
  assert.deepEqual([...h.cache.values.keys()].filter(key => key.startsWith('vh_')), ['vh_' + identity.key]);
});

test('two Google identities with the same legacy vid never share cached history', () => {
  const h = createHarness();
  const aliceSession = signedSession(h.context, 'alice-sub', 'alice@example.invalid');
  const bobSession = signedSession(h.context, 'bob-sub', 'bob@example.invalid');
  const alice = h.context.conversationIdentity_('', aliceSession);
  const bob = h.context.conversationIdentity_('', bobSession);

  decode(h.context.voiceReply_({ ct: alice.token, s: aliceSession, vid: 'collision', q: 'alice-only-marker' }));
  decode(h.context.voiceReply_({ ct: bob.token, s: bobSession, vid: 'collision', q: 'bob-only-marker' }));

  assert.equal(h.providerCalls, 2);
  assert.equal(JSON.stringify(h.providerPayloads[1]).includes('alice-only-marker'), false);
  assert.equal(JSON.stringify(h.providerPayloads[1]).includes('bob-only-marker'), true);
  assert.ok(h.cache.get('vh_' + alice.key));
  assert.ok(h.cache.get('vh_' + bob.key));
});

test('daily and session budgets are reserved under one lock before one provider call', () => {
  const h = createHarness({ CHAT_DAILY_CAP: '1' });
  const first = h.context.conversationIdentity_('', '');
  const second = h.context.conversationIdentity_('', '');
  let raced;
  h.onProviderFetch = () => {
    raced = h.context.reception(second.token, 'concurrent request', [], '');
  };

  const result = h.context.reception(first.token, 'first request', [], '');
  assert.equal(result.ok, true);
  assert.equal(raced.degraded, 'daily-cap');
  assert.equal(h.providerCalls, 1);
  assert.equal(h.providerSawLock, false, 'the provider must run after the critical section');
  assert.equal(h.propertyWritesUnderLock, 1);
  const state = JSON.parse(h.values.get('CHAT_BUDGET_V1'));
  assert.equal(state.daily, 1);
  assert.equal(h.values.get(h.context.dailyKey_()), '1');
});

test('a failed provider attempt remains reserved and a retry cannot overspend', () => {
  const h = createHarness({ CHAT_DAILY_CAP: '1' });
  const identity = h.context.conversationIdentity_('', '');
  h.failProvider = true;
  const failed = h.context.reception(identity.token, 'first request', [], '');
  assert.equal(failed.degraded, 'api-error');
  assert.equal(h.providerCalls, 1);

  h.failProvider = false;
  const retry = h.context.reception(identity.token, 'retry request', [], '');
  assert.equal(retry.degraded, 'daily-cap');
  assert.equal(h.providerCalls, 1);
});

test('active-session state is bounded and new spend fails closed without eviction', () => {
  const h = createHarness();
  const admitted = [];
  for (let i = 0; i < 96; i += 1) {
    const identity = h.context.conversationIdentity_('', '');
    admitted.push(identity);
    assert.equal(h.context.reserveChatBudget_(identity.key).ok, true);
  }
  const overflow = h.context.conversationIdentity_('', '');
  const denied = h.context.reserveChatBudget_(overflow.key);
  assert.equal(denied.reason, 'session-capacity');
  const state = JSON.parse(h.values.get('CHAT_BUDGET_V1'));
  assert.equal(Object.keys(state.sessions).length, 96);
  assert.equal(Object.hasOwn(state.sessions, admitted[0].key), true);
  assert.ok(h.values.get('CHAT_BUDGET_V1').length < 8500);
});

test('identified reception keeps provenance untrusted and leaks no Governor data upstream', () => {
  const governorMarker = 'private-governor@example.invalid';
  const h = createHarness({ GOVERNOR_EMAILS: governorMarker });
  const session = signedSession(h.context, 'private-google-subject', 'alice@example.invalid');
  const identity = h.context.conversationIdentity_('', session);
  const result = h.context.reception(identity.token, 'Help with a Salesforce flow', [], session);
  assert.equal(result.ok, true);

  assert.equal(h.sheetNames.every(name => name === 'PUBLIC_INBOX'), true);
  assert.ok(h.inboxRows.length >= 2);
  for (const row of h.inboxRows) {
    assert.equal(row[5], 'EXTERNAL_UNTRUSTED');
    assert.equal(row[6], 'NONE');
    assert.equal(row[7], 'PUBLIC_RECEPTION');
    assert.equal(row[8], 'UNREVIEWED');
    assert.equal(row[2], identity.key);
    assert.equal(row[2].includes('private-google-subject'), false);
  }
  assert.equal(h.inboxRows[0][3], 'visitor:alice@example.invalid');
  const outbound = JSON.stringify(h.providerPayloads[0]);
  assert.equal(outbound.includes(governorMarker), false);
  assert.equal(outbound.includes('alice@example.invalid'), false);
  assert.equal(outbound.includes('private-google-subject'), false);
});

test('numeric caps and both clients retain the signed-token contract', () => {
  const h = createHarness();
  assert.equal(h.context.CHAT_SESSION_CAP, 12);
  assert.equal(h.context.CHAT_DAILY_DEFAULT, 150);
  assert.match(RECEPTION, /var CONVERSATION = "<\?!= conversationToken \?>"/);
  assert.match(RECEPTION, /\.reception\(CONVERSATION,/);
  assert.doesNotMatch(RECEPTION, /sfdc_sid/);
  assert.match(VOICE, /action:"say", ct:CONVERSATION/);
  assert.doesNotMatch(VOICE, /action:"say", vid:/);
  assert.doesNotMatch(CODE, /String\(p\.vid/);
  assert.match(INBOX, /'EXTERNAL_UNTRUSTED',\s*\n\s*'NONE',\s*\n\s*'PUBLIC_RECEPTION'/);
});
