#!/usr/bin/env node
/*
 * SFDC24 — Drive Trash Guard proofs
 * claude-code-cli, 2026-10-01
 *
 * WHY THIS EXISTS
 *   On 2026-10-01 the board bus, an Apps Script web app that had sat in the Drive trash since
 *   Aug 31 and kept serving, was purged by Google's 30-day trash sweep at 20:15:54Z. Every agent
 *   lost the board until an Admin Restore data. apps-script/drive-trash-guard runs hourly and
 *   untrashes any protected file it finds in the trash, or raises an alert when one is gone.
 *
 * RUN
 *   node tests/test_drive_trash_guard.js
 *
 * Loads the tracked Code.gs into a vm with fake Drive, Script, Mail and UrlFetch services.
 * Nothing leaves the process.
 */
'use strict';

const assert = require('assert');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const SRC = fs.readFileSync(path.join(__dirname, '..', 'apps-script', 'drive-trash-guard', 'Code.gs'), 'utf8');
const BUS = '1meav8p2zkRt-8obarV_fB5Q2EyExCvAaoZa3ro9_fmo4OE_95FpWkfu9';
const SHEET = '120_71KaF4JKGPGz0qUz4phqWRljSqEzSRRm_0zXC_oY';
const FOLDER = '1gZlJFDD2Wm419YOuolIXC3HXwzXOWYfP';
const CANARY = '1vjrB0Ep0sagDngZFAmBg1YXCaBE9rJ2YKPdh64hqTi4';
const SELF = 'SELF-SCRIPT-ID';

function world(opts) {
  opts = opts || {};
  const trashed = new Set(opts.trashed || []);
  const missing = new Set(opts.missing || []);
  const props = Object.assign({}, opts.props || {});
  const w = { mails: [], fetches: [], untrashed: [], lookups: [], triggers: (opts.triggers || []).slice(),
              deleted: [], created: [], props: props };
  function item(id, kind) {
    w.lookups.push(kind + ':' + id);
    if (missing.has(id)) throw new Error(opts.missingText || ('No item with the given ID could be found: ' + id));
    return {
      isTrashed: () => trashed.has(id),
      setTrashed: (b) => { if (!b) { trashed.delete(id); w.untrashed.push(id); } }
    };
  }
  w.ctx = {
    console: { error: () => {}, log: () => {} },
    DriveApp: { getFileById: (id) => item(id, 'file'), getFolderById: (id) => item(id, 'folder') },
    ScriptApp: {
      getScriptId: () => SELF,
      getProjectTriggers: () => w.triggers,
      deleteTrigger: (t) => { w.deleted.push(t.getHandlerFunction()); w.triggers = w.triggers.filter((x) => x !== t); },
      newTrigger: (fn) => ({ timeBased: () => ({ everyHours: (n) => ({ create: () => { w.created.push([fn, n]); } }) }) })
    },
    PropertiesService: { getScriptProperties: () => ({ getProperty: (k) => (k in props ? props[k] : null),
                                                       setProperty: (k, v) => { props[k] = v; } }) },
    MailApp: { sendEmail: (to, subject, body) => { if (opts.mailThrows) throw new Error('quota'); w.mails.push({ to, subject, body }); } },
    Session: { getEffectiveUser: () => ({ getEmail: () => 'owner@example.test' }) },
    UrlFetchApp: { fetch: (url, o) => { if (opts.fetchThrows) throw new Error('dns'); w.fetches.push({ url, o });
                                        return { getResponseCode: () => 200 }; } }
  };
  vm.createContext(w.ctx);
  vm.runInContext(SRC, w.ctx);
  return w;
}

const BOARD = { BUS_URL: 'https://bus.example.test/exec', BUS_SECRET: 's3cr3t-bus' };
const tests = [];
const test = (name, fn) => tests.push([name, fn]);

test('the bus, the sheet, the folder and the canary are protected (nobody drops them silently)', () => {
  const w = world();
  const files = w.ctx.PROTECTED_FILES.map((p) => p[0]);
  for (const id of [BUS, SHEET, CANARY]) assert.ok(files.includes(id), id);
  assert.ok(w.ctx.PROTECTED_FOLDERS.map((p) => p[0]).includes(FOLDER));
});

test('nothing in the trash: no alert, every item is looked up, and the run is recorded', () => {
  const w = world({ props: BOARD });
  const r = w.ctx.guard();
  assert.deepStrictEqual([r.restored.length, r.problems.length], [0, 0]);
  assert.deepStrictEqual([w.mails.length, w.fetches.length, w.untrashed.length], [0, 0, 0]);
  assert.ok(w.lookups.includes('file:' + BUS) && w.lookups.includes('folder:' + FOLDER) && w.lookups.includes('file:' + SELF));
  assert.ok(w.props.LAST_RUN);
});

test('a trashed bus is untrashed at once, with an email and one board row to the fleet', () => {
  const w = world({ trashed: [BUS], props: BOARD });
  const r = w.ctx.guard();
  assert.deepStrictEqual(w.untrashed, [BUS]);
  assert.strictEqual(r.restored.length, 1);
  assert.strictEqual(w.mails.length, 1);
  assert.strictEqual(w.mails[0].to, 'owner@example.test');
  assert.match(w.mails[0].subject, /^Drive Trash Guard: 1 untrashed$/);
  assert.ok(w.mails[0].body.includes(BUS));
  assert.strictEqual(w.fetches.length, 1);
  const sent = JSON.parse(w.fetches[0].o.payload);
  assert.strictEqual(w.fetches[0].url, BOARD.BUS_URL);
  assert.deepStrictEqual([sent.action, sent.secret, sent.title], ['append', BOARD.BUS_SECRET, 'Blackboard - Alpha DB']);
  const row = sent.sheetRow;
  assert.strictEqual(row.length, 10);
  assert.match(row[0], /^DRIVE-TRASH-GUARD-\d{8}T\d{4}Z$/);
  assert.deepStrictEqual([row[2], row[3], row[4], row[6], row[7]], ['drive-trash-guard', 'fleet;claude-code-cli', 'RESULT', 'OPEN', 'FLEET']);
  const fields = row[5].split('|');
  assert.strictEqual(fields.length, 8, 'BCB payload has exactly 7 delimiters');
  assert.deepStrictEqual(fields.slice(0, 7), ['BCB', 'v=1', 'id=' + row[0], 'phase=RESULT', 'class=ALERT',
                                             'from=drive-trash-guard', 'to=fleet,claude-code-cli']);
  assert.ok(!w.mails[0].body.includes(BOARD.BUS_SECRET) && !row.join(' ').includes(BOARD.BUS_SECRET));
});

test('a trashed folder, and the guard itself, are untrashed too', () => {
  const w = world({ trashed: [FOLDER, SELF] });
  const r = w.ctx.guard();
  assert.deepStrictEqual(w.untrashed.sort(), [FOLDER, SELF].sort());
  assert.strictEqual(r.restored.length, 2);
});

test('a purged file is an URGENT alert with the Restore data step, and the others are still checked', () => {
  const w = world({ missing: [BUS], trashed: [CANARY], props: BOARD });
  const r = w.ctx.guard();
  assert.deepStrictEqual(w.untrashed, [CANARY]);              // the error on the bus did not stop the canary
  assert.strictEqual(r.problems.length, 1);
  assert.match(w.mails[0].subject, /^URGENT: Drive Trash Guard: 1 untrashed, 1 not found$/);
  assert.ok(w.mails[0].body.includes('restore it NOW with Admin console > Users > owner > Restore data (Drive)'));
  assert.ok(w.mails[0].body.includes('within 25 days'));
  assert.ok(w.lookups.includes('file:' + SELF));
});

test('with no BUS_URL or BUS_SECRET the alert is email only', () => {
  for (const props of [{}, { BUS_URL: BOARD.BUS_URL }, { BUS_SECRET: BOARD.BUS_SECRET }]) {
    const w = world({ trashed: [BUS], props });
    w.ctx.guard();
    assert.deepStrictEqual([w.mails.length, w.fetches.length], [1, 0]);
  }
});

test('a failed email still posts the board row, and a failed post does not throw', () => {
  const w = world({ trashed: [BUS], props: BOARD, mailThrows: true });
  w.ctx.guard();
  assert.deepStrictEqual([w.mails.length, w.fetches.length, w.untrashed.length], [0, 1, 1]);
  const w2 = world({ trashed: [BUS], props: BOARD, fetchThrows: true });
  assert.doesNotThrow(() => w2.ctx.guard());
  assert.strictEqual(w2.mails.length, 1);
});

test('an error text with | or a newline cannot break the BCB payload', () => {
  const w = world({ missing: [SHEET], missingText: 'bad|id\nsecond line', props: BOARD });
  w.ctx.guard();
  const row = JSON.parse(w.fetches[0].o.payload).sheetRow;
  assert.strictEqual(row[5].split('|').length, 8);
  assert.ok(!/[\r\n]/.test(row[5]) && !/[\r\n]/.test(w.mails[0].body));
});

test('install replaces only its own guard triggers with one hourly trigger, then runs once', () => {
  const mk = (fn) => ({ getHandlerFunction: () => fn });
  const w = world({ triggers: [mk('guard'), mk('monitorTick'), mk('guard')], trashed: [CANARY] });
  w.ctx.install();
  assert.deepStrictEqual(w.deleted, ['guard', 'guard']);
  assert.deepStrictEqual(w.triggers.map((t) => t.getHandlerFunction()), ['monitorTick']);
  assert.deepStrictEqual(w.created, [['guard', 1]]);
  assert.deepStrictEqual(w.untrashed, [CANARY]);
});

test('no web entry point: nothing on the internet can call the guard', () => {
  const w = world();
  assert.strictEqual(typeof w.ctx.doGet, 'undefined');
  assert.strictEqual(typeof w.ctx.doPost, 'undefined');
});

let failed = 0;
for (const [name, fn] of tests) {
  try { fn(); console.log('ok   ' + name); } catch (e) { failed++; console.log('FAIL ' + name + '\n     ' + e.message); }
}
console.log('%d tests, %d failed', tests.length, failed);
process.exit(failed ? 1 : 0);
