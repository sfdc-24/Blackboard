/*
  gas-bus/Code.js, SENDER CHECK (owner, 2026-10-24: "the board doesn't confirm which agent actually
  sent a row. fix that").

  Every writer holds the one shared BUS_SECRET, so column C and the payload's from= were whatever the
  caller typed. The gateway now stamps column L with what it could confirm, and by default only FLAGS.
  Google's tokeninfo endpoint is stubbed; a test that reaches any other URL fails loudly.

  Run: node tests/test_gas_bus_sender_check.cjs
*/
'use strict';

const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const SOURCE = fs.readFileSync(path.join(__dirname, '..', 'gas-bus', 'Code.js'), 'utf8');
const TOKEN = 'aaaa.bbbb.cccc';
const FUTURE = String(Math.floor(Date.now() / 1000) + 600);

function world(props = {}, google = {}) {
  const rows = [];
  const fetches = [];
  let fetchThrows = false;
  const finder = (col) => (text) => {
    let entire = false, regex = false;
    const self = {
      matchEntireCell(v) { entire = v; return self; },
      useRegularExpression(v) { regex = v; return self; },
      matchCase() { return self; },
      findNext() {
        for (let i = 0; i < rows.length; i++) {
          const cell = String(rows[i][col - 1] == null ? '' : rows[i][col - 1]);
          const hit = regex ? new RegExp(text).test(cell) : (entire ? cell === text : cell.includes(text));
          if (hit) return { getRow: () => i + 1 };
        }
        return null;
      },
    };
    return self;
  };
  const sheet = {
    getLastRow: () => rows.length,
    getRange: (r, c) => ({ createTextFinder: finder(c) }),
    appendRow: (row) => rows.push(row.slice()),
  };
  const claims = Object.assign({ iss: 'https://accounts.google.com', aud: 'sfdc24-blackboard-bus',
    email: 'bus-reconciler@sfdc24.iam.gserviceaccount.com', email_verified: 'true', exp: FUTURE }, google.claims || {});
  const file = { getMimeType: () => 'sheet', getId: () => 'id1', getName: () => 'Blackboard - Alpha DB' };
  const ctx = {
    PropertiesService: { getScriptProperties: () => ({
      getProperty: (k) => (k in props ? props[k] : ({ FOLDER_ID: 'f', BUS_SECRET: 's' })[k] || null) }) },
    LockService: { getScriptLock: () => ({ tryLock: () => true, releaseLock: () => {} }) },
    CacheService: { getScriptCache: () => ({ get: () => null, put: () => {} }) },
    UrlFetchApp: { fetch: (url, opts) => {
      fetches.push({ url, opts });
      if (url !== 'https://oauth2.googleapis.com/tokeninfo') throw new Error('unexpected fetch ' + url);
      if (fetchThrows) throw new Error('down');
      return { getResponseCode: () => google.code || 200, getContentText: () => JSON.stringify(claims) };
    } },
    ScriptApp: { getOAuthToken: () => 'tok' },
    MimeType: { GOOGLE_DOCS: 'doc', GOOGLE_SHEETS: 'sheet' },
    SpreadsheetApp: { openById: () => ({ getSheets: () => [sheet], getSheetByName: () => sheet }) },
    DocumentApp: {},
    DriveApp: {
      getFolderById: () => ({ getFilesByName: () => {
        let done = false;
        return { hasNext: () => !done, next: () => { done = true; return file; } };
      } }),
      getFileById: () => file,
    },
    ContentService: {
      MimeType: { JSON: 'json' },
      createTextOutput: (s) => ({ setMimeType: () => ({ body: JSON.parse(s) }) }),
    },
    Utilities: { formatDate: () => '2026-10-24 11:00:00' },
    Session: { getScriptTimeZone: () => 'America/Toronto' },
    console,
  };
  vm.createContext(ctx);
  vm.runInContext(SOURCE, ctx);
  const post = (sheetRow, extra = {}) => ctx.doPost({ postData: { contents: JSON.stringify(Object.assign(
    { secret: 's', action: 'append', title: 'Blackboard - Alpha DB', sheetRow }, extra)) } }).body;
  return { rows, fetches, post, setFetchThrows: (v) => { fetchThrows = v; } };
}

let n = 0;
function row(tag, payload, width = 12) {
  n++;
  const r = ['R' + n, '2026-10-24T15:00:00Z', tag, 'aya', 'APPEND', payload, 'OPEN', 'Blackboard', 'g', 'dispatch'];
  while (r.length < width) r.push('');
  return r;
}
const L = 11;

const tests = {
  'shared secret alone: appended, stamped unverified, no network call'() {
    const w = world();
    const out = w.post(row('grok', 'BCB|v=1|id=A-1|from=grok|to=aya|'));
    assert.deepStrictEqual([out.ok, out.sender], [true, 'sender=unverified']);
    assert.strictEqual(w.rows[0][L], 'sender=unverified');
    assert.strictEqual(w.fetches.length, 0);
  },
  'column C and from= disagree: appended AND flagged mismatch (flag is the default)'() {
    const w = world();
    const out = w.post(row('aya', 'BCB|v=1|id=A-2|from=grok|to=aya|'));
    assert.strictEqual(out.ok, true);
    assert.strictEqual(w.rows[0][L], 'sender=mismatch;claimed=aya;from=grok');
  },
  'the comparison is case- and space-insensitive (CODEX-DESKTOP vs codex-desktop is not a mismatch)'() {
    const w = world();
    w.post(row('CODEX-DESKTOP ', 'BCB|v=1|id=A-3|from=codex-desktop|'));
    assert.strictEqual(w.rows[0][L], 'sender=unverified');
  },
  'a row with no from= field is compared on nothing and is unverified, not mismatch'() {
    const w = world();
    w.post(row('cowork-chrome', 'BCB|CONVERSPAN-GO-NOGO|FROM claude TO grok'));
    assert.strictEqual(w.rows[0][L], 'sender=unverified');
  },
  'from= is the field, never a substring (reply_from= is not from=)'() {
    const w = world();
    w.post(row('grok', 'BCB|v=1|id=A-4|reply_from=aya|from=grok|'));
    assert.strictEqual(w.rows[0][L], 'sender=unverified');
  },
  'a caller cannot pre-stamp itself verified: column L is overwritten'() {
    const w = world();
    const r = row('grok', 'BCB|v=1|id=A-5|from=grok|');
    r[L] = 'sender=verified;who=bus-reconciler@sfdc24.iam.gserviceaccount.com';
    w.post(r);
    assert.strictEqual(w.rows[0][L], 'sender=unverified');
  },
  'a 10-cell row is padded to 12, never shortened; a wider row keeps its extra cells'() {
    const w = world();
    w.post(row('grok', 'BCB|v=1|id=A-6|from=grok|', 10));
    assert.strictEqual(w.rows[0].length, 12);
    const wide = row('grok', 'BCB|v=1|id=A-7|from=grok|', 14);
    wide[13] = 'keep';
    w.post(wide);
    assert.deepStrictEqual([w.rows[1].length, w.rows[1][13]], [14, 'keep']);
  },
  'the first ten cells are written exactly as sent'() {
    const w = world();
    const r = row('grok', 'BCB|v=1|id=A-8|from=grok|');
    w.post(r);
    assert.strictEqual(JSON.stringify(w.rows[0].slice(0, 10)), JSON.stringify(r.slice(0, 10)));
  },
  'a good token for an account allowed the tag: verified, and the token is never echoed or stored'() {
    const w = world();
    const out = w.post(row('bus-reconciler', 'BCB|v=1|id=V-1|from=bus-reconciler|'), { id_token: TOKEN });
    assert.strictEqual(out.sender, 'sender=verified;who=bus-reconciler@sfdc24.iam.gserviceaccount.com');
    assert.strictEqual(w.rows[0][L], out.sender);
    assert.ok(!JSON.stringify(out).includes(TOKEN) && !JSON.stringify(w.rows).includes(TOKEN));
    assert.strictEqual(w.fetches[0].opts.method, 'post', 'the token goes in a POST body, never a URL');
  },
  'a good token used under someone else\'s tag: mismatch naming the real account'() {
    const w = world();
    w.post(row('grok', 'BCB|v=1|id=V-2|from=grok|'), { id_token: TOKEN });
    assert.strictEqual(w.rows[0][L], 'sender=mismatch;claimed=grok;token=bus-reconciler@sfdc24.iam.gserviceaccount.com');
  },
  'a verified Google account that is not on SENDER_IDENTITIES is a mismatch, not verified'() {
    const w = world({}, { claims: { email: 'someone@gmail.com' } });
    w.post(row('gemini', 'BCB|v=1|id=V-3|from=gemini|'), { id_token: TOKEN });
    assert.strictEqual(w.rows[0][L], 'sender=mismatch;claimed=gemini;token=someone@gmail.com');
  },
  'SENDER_IDENTITIES is an own-property lookup (a tag named constructor or __proto__ gets nothing)'() {
    const w = world({}, { claims: { email: '__proto__' } });
    w.post(row('constructor', 'BCB|v=1|id=V-4|'), { id_token: TOKEN });
    assert.ok(w.rows[0][L].startsWith('sender=mismatch'));
  },
  'each failed token check is bad-token with its reason, and the row still lands'() {
    const cases = [
      [{ claims: { aud: 'another-service' } }, 'audience'],
      [{ claims: { iss: 'https://evil.example' } }, 'issuer'],
      [{ claims: { email_verified: 'false' } }, 'email-unverified'],
      [{ claims: { exp: '1000' } }, 'expired'],
      [{ claims: { email: '' } }, 'no-email'],
      [{ code: 400 }, 'rejected-by-google'],
    ];
    for (const [google, why] of cases) {
      const w = world({}, google);
      const out = w.post(row('bus-reconciler', 'BCB|v=1|id=B-1|from=bus-reconciler|'), { id_token: TOKEN });
      assert.deepStrictEqual([out.ok, w.rows[0][L]], [true, 'sender=bad-token;why=' + why], why);
    }
  },
  'a token that is not JWT-shaped is refused before any network call'() {
    const w = world();
    w.post(row('bus-reconciler', 'BCB|v=1|id=B-2|'), { id_token: 'not a token|x=1' });
    assert.strictEqual(w.rows[0][L], 'sender=bad-token;why=shape');
    assert.strictEqual(w.fetches.length, 0);
  },
  'Google unreachable never fails the append'() {
    const w = world();
    w.setFetchThrows(true);
    const out = w.post(row('bus-reconciler', 'BCB|v=1|id=B-3|'), { id_token: TOKEN });
    assert.deepStrictEqual([out.ok, w.rows[0][L]], [true, 'sender=bad-token;why=tokeninfo-unreachable']);
  },
  'SENDER_AUDIENCE overrides the default audience'() {
    const w = world({ SENDER_AUDIENCE: 'custom-aud' }, { claims: { aud: 'custom-aud' } });
    w.post(row('gemini', 'BCB|v=1|id=V-5|', 12), { id_token: TOKEN });
    assert.strictEqual(w.rows[0][L], 'sender=mismatch;claimed=gemini;token=bus-reconciler@sfdc24.iam.gserviceaccount.com');
  },
  'enforce: mismatch and bad-token are refused 403 and nothing is appended; unverified still passes'() {
    const w = world({ SENDER_CHECK: 'enforce' }, { claims: { aud: 'wrong' } });
    const a = w.post(row('aya', 'BCB|v=1|id=E-1|from=grok|'));
    assert.deepStrictEqual([a.ok, a.error, a._httpStatus, a.sender], [false, 'SENDER_REFUSED', 403, 'sender=mismatch;claimed=aya;from=grok']);
    const b = w.post(row('bus-reconciler', 'BCB|v=1|id=E-2|'), { id_token: TOKEN });
    assert.deepStrictEqual([b.ok, b.sender], [false, 'sender=bad-token;why=audience']);
    assert.strictEqual(w.rows.length, 0);
    assert.strictEqual(w.post(row('grok', 'BCB|v=1|id=E-3|from=grok|')).ok, true);
    assert.strictEqual(w.rows.length, 1);
  },
  'off: the old behaviour - nothing stamped, row written verbatim, no sender field'() {
    const w = world({ SENDER_CHECK: 'off' });
    const r = row('aya', 'BCB|v=1|id=O-1|from=grok|', 10);
    const out = w.post(r, { id_token: TOKEN });
    assert.strictEqual(JSON.stringify(w.rows[0]), JSON.stringify(r));  // across the vm realm
    assert.strictEqual(out.sender, undefined);
    assert.strictEqual(w.fetches.length, 0);
  },
  'a stamp can never carry a | (the board field separator) or a ; from a hostile tag'() {
    const w = world();
    w.post(row('a|b;c', 'BCB|v=1|id=H-1|from=x;y=z|'));
    assert.ok(!/\|/.test(w.rows[0][L]));
    assert.strictEqual(w.rows[0][L], 'sender=mismatch;claimed=abc;from=xyz');
  },
  'a text append with no sheetRow is untouched by the sender check'() {
    const w = world();
    const out = w.post(undefined, { text: 'hello' });
    assert.strictEqual(out.sender, undefined);
    assert.strictEqual(w.rows[0].length, 2);
  },
  'a DUPLICATE is still refused with the sender check on'() {
    const w = world();
    const r = row('grok', 'BCB|v=1|id=D-1|from=grok|');
    w.post(r);
    assert.strictEqual(w.post(r).error, 'DUPLICATE');
    assert.strictEqual(w.rows.length, 1);
  },
};

let failed = 0;
for (const [name, fn] of Object.entries(tests)) {
  try { fn(); console.log('ok   ' + name); } catch (err) { failed++; console.log('FAIL ' + name + '\n     ' + err.message); }
}
console.log(failed ? `FAILED ${failed} of ${Object.keys(tests).length}` : `OK ${Object.keys(tests).length}`);
process.exit(failed ? 1 : 0);
