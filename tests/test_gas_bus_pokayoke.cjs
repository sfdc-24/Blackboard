/*
  gas-bus/Code.js, PY-01 and PY-02 (Grok's poka-yoke audit, 2026-10-08).

  PY-01: a sheetRow whose Row_ID, or whose BCB id=, is already on the sheet is refused with DUPLICATE
  and nothing is appended. PY-02: shipped OFF; when WORKER_TRIGGER=on, an AYA_REQ to bus-reconciler
  starts ONE run of the worker job, debounced, and the append never fails because the trigger did.

  The Apps Script globals are stubbed with only what the gateway calls, so a new service call fails
  loudly here instead of passing against a stub that accepts anything.
*/
'use strict';

const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const SOURCE = fs.readFileSync(path.join(__dirname, '..', 'gas-bus', 'Code.js'), 'utf8');

function world(props = {}) {
  const rows = [];
  const fetches = [];
  const cache = {};
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
  const ctx = {
    PropertiesService: { getScriptProperties: () => ({
      getProperty: (k) => (k in props ? props[k] : ({ FOLDER_ID: 'f', BUS_SECRET: 's' })[k] || null) }) },
    LockService: { getScriptLock: () => ({ tryLock: () => true, releaseLock: () => {} }) },
    CacheService: { getScriptCache: () => ({ get: (k) => cache[k] || null, put: (k, v) => { cache[k] = v; } }) },
    UrlFetchApp: { fetch: (url, opts) => {
      fetches.push({ url, opts });
      if (fetchThrows) throw new Error('down');
      return { getResponseCode: () => 200 };
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
    Utilities: { formatDate: () => '2026-10-08 15:00:00' },
    Session: { getScriptTimeZone: () => 'America/Toronto' },
    console,
  };
  const file = { getMimeType: () => 'sheet', getId: () => 'id1', getName: () => 'Blackboard - Alpha DB' };
  vm.createContext(ctx);
  vm.runInContext(SOURCE, ctx);
  const post = (sheetRow) => ctx.doPost({ postData: { contents: JSON.stringify(
    { secret: 's', action: 'append', title: 'Blackboard - Alpha DB', sheetRow }) } }).body;
  return { rows, fetches, post, setFetchThrows: (v) => { fetchThrows = v; } };
}

function row(rowId, payload, action = 'APPEND', target = 'ALL') {
  return [rowId, '2026-10-08T15:00:00Z', 'grok', target, action, payload, 'OPEN', 'Blackboard', 'g', ''];
}

const tests = {
  'a new row is appended'() {
    const w = world();
    assert.strictEqual(w.post(row('R1', 'BCB|v=1|id=A-1|phase=NOTE')).ok, true);
    assert.strictEqual(w.rows.length, 1);
  },
  'the same Row_ID again is DUPLICATE and nothing is appended'() {
    const w = world();
    w.post(row('R1', 'BCB|v=1|id=A-1|phase=NOTE'));
    const out = w.post(row('R1', 'BCB|v=1|id=A-2|phase=NOTE'));
    assert.deepStrictEqual([out.ok, out.error, out.duplicate, out.existing_row], [false, 'DUPLICATE', 'row_id', 1]);
    assert.strictEqual(w.rows.length, 1);
  },
  'a new Row_ID with a BCB id already on the sheet is DUPLICATE'() {
    const w = world();
    w.post(row('R1', 'BCB|v=1|id=GROK-OWNER-GO-FIXES-0907|phase=DISPATCH'));
    const out = w.post(row('R2', 'BCB|v=1|id=GROK-OWNER-GO-FIXES-0907|phase=DISPATCH'));
    assert.deepStrictEqual([out.error, out.duplicate, out.id], ['DUPLICATE', 'bcb_id', 'GROK-OWNER-GO-FIXES-0907']);
    assert.strictEqual(w.rows.length, 1);
  },
  'an id named only in answers= or as a prefix is not a duplicate'() {
    const w = world();
    w.post(row('R1', 'BCB|v=1|id=REQ-1|phase=REQUEST'));
    assert.strictEqual(w.post(row('R2', 'BCB|v=1|id=RES-1|answers=REQ-1')).ok, true);
    assert.strictEqual(w.post(row('R3', 'BCB|v=1|id=REQ|phase=NOTE')).ok, true, 'REQ is not REQ-1');
    assert.strictEqual(w.post(row('R4', 'BCB|v=1|req=REQ-1|do=redis-op')).ok, true, 'req= is not id=');
    assert.strictEqual(w.rows.length, 4);
  },
  'a Row_ID that is only part of another is not a duplicate'() {
    const w = world();
    w.post(row('GROK-RW-1103-SET', 'BCB|v=1|id=A-1'));
    assert.strictEqual(w.post(row('GROK-RW-1103', 'BCB|v=1|id=A-2')).ok, true);
    assert.strictEqual(w.rows.length, 2);
  },
  'APPEND_DEDUP=off turns it off without a redeploy'() {
    const w = world({ APPEND_DEDUP: 'off' });
    w.post(row('R1', 'BCB|v=1|id=A-1'));
    assert.strictEqual(w.post(row('R1', 'BCB|v=1|id=A-1')).ok, true);
    assert.strictEqual(w.rows.length, 2);
  },
  'APPEND_DEDUP=rowid keeps the Row_ID check alone'() {
    const w = world({ APPEND_DEDUP: 'rowid' });
    w.post(row('R1', 'BCB|v=1|id=A-1'));
    assert.strictEqual(w.post(row('R2', 'BCB|v=1|id=A-1')).ok, true, 'a re-ask under the same id passes');
    assert.strictEqual(w.post(row('R1', 'BCB|v=1|id=A-9')).error, 'DUPLICATE', 'the Row_ID still does not');
  },
  'the worker trigger is OFF unless WORKER_TRIGGER is exactly on'() {
    for (const value of [undefined, 'true', 'ON', '1']) {
      const w = world(value === undefined ? {} : { WORKER_TRIGGER: value });
      const out = w.post(row('Q1', 'BCB|v=1|id=Q1|req=Q1|do=redis-op', 'AYA_REQ', 'bus-reconciler'));
      assert.strictEqual(out.ok, true);
      assert.strictEqual(out.worker, undefined, String(value));
      assert.strictEqual(w.fetches.length, 0, String(value));
    }
  },
  'on: a request starts ONE run, and a burst is debounced'() {
    const w = world({ WORKER_TRIGGER: 'on' });
    const first = w.post(row('Q1', 'BCB|v=1|id=Q1|req=Q1|do=redis-op', 'AYA_REQ', 'bus-reconciler'));
    const second = w.post(row('Q2', 'BCB|v=1|id=Q2|req=Q2|do=redis-op', 'AYA_REQ', 'bus-reconciler'));
    assert.strictEqual(first.worker, 'started-http-200');
    assert.strictEqual(second.worker, 'debounced');
    assert.strictEqual(w.fetches.length, 1);
    assert.strictEqual(w.fetches[0].url,
      'https://run.googleapis.com/v2/projects/sfdc24/locations/us-central1/jobs/bus-requests:run');
    assert.strictEqual(w.fetches[0].opts.headers.Authorization, 'Bearer tok');
  },
  'on: a row that is not a worker request starts nothing'() {
    const w = world({ WORKER_TRIGGER: 'on' });
    w.post(row('N1', 'BCB|v=1|id=N1|phase=NOTE'));
    w.post(row('N2', 'BCB|v=1|id=N2', 'AYA_REQ', 'grok'));
    assert.strictEqual(w.fetches.length, 0);
  },
  'on: a failed start never fails the append'() {
    const w = world({ WORKER_TRIGGER: 'on' });
    w.setFetchThrows(true);
    const out = w.post(row('Q1', 'BCB|v=1|id=Q1|req=Q1|do=redis-op', 'AYA_REQ', 'bus-reconciler'));
    assert.deepStrictEqual([out.ok, out.worker], [true, 'start-failed']);
    assert.strictEqual(w.rows.length, 1);
  },
  'a job name the property cannot smuggle a path through'() {
    const w = world({ WORKER_TRIGGER: 'on', WORKER_JOB: 'projects/x/locations/y/jobs/z/../../other' });
    const out = w.post(row('Q1', 'BCB|v=1|id=Q1|req=Q1|do=redis-op', 'AYA_REQ', 'bus-reconciler'));
    assert.strictEqual(out.worker, 'bad-job-name');
    assert.strictEqual(w.fetches.length, 0);
  },
};

let failed = 0;
for (const [name, fn] of Object.entries(tests)) {
  try { fn(); console.log('ok   ' + name); } catch (err) { failed++; console.log('FAIL ' + name + '\n     ' + err.message); }
}
console.log(failed ? `FAILED ${failed} of ${Object.keys(tests).length}` : `OK ${Object.keys(tests).length}`);
process.exit(failed ? 1 : 0);
