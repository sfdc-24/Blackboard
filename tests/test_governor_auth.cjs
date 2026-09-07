const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '../apps-script/governor-page-api/Auth.gs'), 'utf8');

function complete(overrides = {}) {
  const claims = { aud: 'test-client', iss: 'https://accounts.google.com',
    exp: Math.floor(Date.now() / 1000) + 600, email_verified: true,
    email: 'visitor@example.invalid', sub: 'test-subject', ...overrides };
  const jwt = 'header.' + Buffer.from(JSON.stringify(claims)).toString('base64url') + '.signature';
  let minted = 0;
  const cache = new Map([['authstate_test-state', '1']]);
  const context = vm.createContext({
    PropertiesService: { getScriptProperties: () => ({getProperty: name => name === 'GOOGLE_CLIENT_ID' ? 'test-client' : 'mock-secret'}) },
    CacheService: { getScriptCache: () => ({get: key => cache.get(key), remove: key => cache.delete(key)}) },
    Utilities: {base64DecodeWebSafe: s => Buffer.from(s, 'base64url'), newBlob: b => ({getDataAsString: () => b.toString()})},
    UrlFetchApp: {fetch(url) {
      assert.equal(url, 'https://oauth2.googleapis.com/token');
      return {getResponseCode: () => 200, getContentText: () => JSON.stringify({id_token: jwt})};
    }},
  });
  vm.runInContext(source, context);
  context.mintSession_ = () => { minted++; return 'mock-session'; };
  const result = context.authCompleteCallback_({parameter: {code: 'mock-code', state: 'test-state'}});
  return {result, minted};
}

test('both exact Google issuers with a verified email can mint a session', () => {
  for (const iss of ['accounts.google.com', 'https://accounts.google.com']) {
    const {result, minted} = complete({iss});
    assert.equal(result.ok, true);
    assert.equal(minted, 1);
  }
});

test('issuer substrings, missing values and non-string claims cannot mint a session', () => {
  for (const iss of [undefined, null, '', 'https://accounts.google.com.attacker.invalid',
    'https://attacker.invalid/accounts.google.com', 'http://accounts.google.com', ['accounts.google.com']]) {
    const {result, minted} = complete({iss});
    assert.equal(result.error, 'unexpected issuer', JSON.stringify(iss));
    assert.equal(minted, 0);
  }
});

test('missing, false and truthy non-boolean email verification cannot mint a session', () => {
  for (const email_verified of [undefined, null, false, 'false', 'true', 0, 1, {}, []]) {
    const {result, minted} = complete({email_verified});
    assert.equal(result.error, 'email not verified by Google', JSON.stringify(email_verified));
    assert.equal(minted, 0);
  }
});

test('audience, expiration and email checks still reject invalid tokens', () => {
  for (const overrides of [{aud: 'another-client'}, {exp: 1}, {email: ''}]) {
    const {result, minted} = complete(overrides);
    assert.equal(result.ok, false);
    assert.equal(minted, 0);
  }
});
