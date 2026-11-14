// Pure-function tests for the R-STATUS gateway actions in Code.js.
// No Apps Script runtime, no network: Code.js exports these under `module` only
// when it exists (a no-op in production - see the bottom of Code.js).
//
// Run: node --test gas-bus/test_status_write.js
'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const {
  validateStatusUpdates_, statusRangeA1_, columnLetter_, handleStatusWrite_,
  STATUS_WRITE_MAX_ROWS, STATUS_COLS,
} = require('./Code.js');

function sixValues() { return ['OPEN', '', '', '', '', '']; }

test('a well-formed batch validates', () => {
  const result = validateStatusUpdates_([{ row_id: 'R1', values: sixValues() }]);
  assert.deepEqual(result, { ok: true });
});

test('an empty array is refused', () => {
  assert.equal(validateStatusUpdates_([]).ok, false);
  assert.equal(validateStatusUpdates_(null).ok, false);
  assert.equal(validateStatusUpdates_(undefined).ok, false);
});

test('more than the max rows per call is refused', () => {
  const updates = [];
  for (let i = 0; i < STATUS_WRITE_MAX_ROWS + 1; i++) {
    updates.push({ row_id: 'R' + i, values: sixValues() });
  }
  const result = validateStatusUpdates_(updates);
  assert.equal(result.ok, false);
  assert.match(result.error, new RegExp(String(STATUS_WRITE_MAX_ROWS)));
});

test('exactly the max rows per call is fine', () => {
  const updates = [];
  for (let i = 0; i < STATUS_WRITE_MAX_ROWS; i++) {
    updates.push({ row_id: 'R' + i, values: sixValues() });
  }
  assert.equal(validateStatusUpdates_(updates).ok, true);
});

test('a missing or blank row_id is refused, naming the index', () => {
  assert.equal(validateStatusUpdates_([{ values: sixValues() }]).ok, false);
  assert.equal(validateStatusUpdates_([{ row_id: '', values: sixValues() }]).ok, false);
  assert.equal(validateStatusUpdates_([{ row_id: '   ', values: sixValues() }]).ok, false);
  const result = validateStatusUpdates_([
    { row_id: 'R1', values: sixValues() },
    { row_id: '', values: sixValues() },
  ]);
  assert.match(result.error, /updates\[1\]/);
});

test('values must be exactly the six M-R cells, not more, not fewer', () => {
  assert.equal(STATUS_COLS, 6);
  assert.equal(validateStatusUpdates_([{ row_id: 'R1', values: ['OPEN'] }]).ok, false);
  assert.equal(validateStatusUpdates_([{ row_id: 'R1', values: ['OPEN', '', '', '', '', '', 'extra'] }]).ok, false);
  assert.equal(validateStatusUpdates_([{ row_id: 'R1', values: 'OPEN' }]).ok, false);
});

test('statusRangeA1_ names only M to R, never a column outside it', () => {
  assert.equal(statusRangeA1_(5), 'M5:R5');
  assert.equal(statusRangeA1_(1), 'M1:R1');
  assert.equal(statusRangeA1_(21228), 'M21228:R21228');
});

test('statusRangeA1_ refuses a non-positive-integer row number', () => {
  assert.throws(() => statusRangeA1_(0));
  assert.throws(() => statusRangeA1_(-1));
  assert.throws(() => statusRangeA1_(1.5));
  assert.throws(() => statusRangeA1_('5'));
});

test('columnLetter_ matches the known M-R columns', () => {
  assert.equal(columnLetter_(13), 'M');
  assert.equal(columnLetter_(18), 'R');
  assert.equal(columnLetter_(1), 'A');
  assert.equal(columnLetter_(27), 'AA');
});

// ---- handleStatusWrite_ against a fake sheet (finding #13): the real write
// path - Row_ID lookup (column A, including a header row at row 1), misses,
// and the lock - had no test at all; only the pure helpers above did.

function fakeRange(sheet, values) {
  return {
    getValues() { return values; },
    setValues(rows) { sheet._written.push(rows[0]); },
  };
}

function makeFakeSheet(rows) {
  // rows[0] is the header row, same as a real sheet read.
  const sheet = {
    _written: [],
    getLastRow() { return rows.length; },
    getRange(a, b, c, d) {
      if (typeof a === 'string') {
        // statusRangeA1_'s "M<row>:R<row>" form - identify the row from it.
        const m = /^M(\d+):R\1$/.exec(a);
        if (!m) throw new Error('unexpected A1 range in test fake: ' + a);
        const rowNum = Number(m[1]);
        return fakeRange(sheet, [rows[rowNum - 1] || []]);
      }
      if (a === 1 && b === 1 && d === 1) {
        // Column A lookup: getRange(1, 1, last, 1).
        return fakeRange(sheet, rows.slice(0, c).map((r) => [r[0]]));
      }
      throw new Error('unexpected getRange call in test fake: ' + JSON.stringify([a, b, c, d]));
    },
  };
  return sheet;
}

function withFakeAppsScript(sheet, fn) {
  const file = { getId() { return 'file-1'; } };
  const prevDrive = global.DriveApp;
  const prevSheets = global.SpreadsheetApp;
  const prevLock = global.LockService;
  global.DriveApp = {
    getFolderById() {
      return { getFilesByName() { let used = false; return { hasNext() { return !used; }, next() { used = true; return file; } }; } };
    },
  };
  global.SpreadsheetApp = { openById() { return { getSheets() { return [sheet]; } }; } };
  let locked = false;
  global.LockService = {
    getScriptLock() {
      return {
        tryLock() { locked = true; return true; },
        releaseLock() { locked = false; },
      };
    },
  };
  try {
    return fn();
  } finally {
    global.DriveApp = prevDrive;
    global.SpreadsheetApp = prevSheets;
    global.LockService = prevLock;
    assert.equal(locked, false, 'the lock must always be released');
  }
}

function jsonBody(response) {
  // jsonOut_ needs ContentService, which this test does not fake (handleStatusWrite_
  // is called directly, bypassing doPost's jsonOut_ wrapping would need) - instead
  // this harness fakes ContentService too, inline, so the real return path runs.
  return JSON.parse(response.getContent());
}

function withFakeContentService(fn) {
  const prev = global.ContentService;
  global.ContentService = {
    MimeType: { JSON: 'application/json' },
    createTextOutput(text) {
      return { _text: text, setMimeType() { return this; }, getContent() { return this._text; } };
    },
  };
  try {
    return fn();
  } finally {
    global.ContentService = prev;
  }
}

test('handleStatusWrite_ finds a row by column A Row_ID, including past the header', () => {
  const rows = [
    ['Row_ID', 'Timestamp'],               // header, row 1
    ['R1', 't1', '', '', '', '', '', '', '', '', '', '', 'OLD', '', '', '', '', ''],
    ['R2', 't2'],
  ];
  const sheet = makeFakeSheet(rows);
  withFakeContentService(() => withFakeAppsScript(sheet, () => {
    const body = { title: 'Blackboard - Alpha DB',
                   updates: [{ row_id: 'R2', values: ['OPEN', 'a', 'b', 'c', 'd', 'e'] }] };
    const result = jsonBody(handleStatusWrite_(body, { folderId: 'folder-1' }));
    assert.equal(result.ok, true);
    assert.equal(result.written, 1);
    assert.deepEqual(result.misses, []);
    assert.deepEqual(sheet._written[0], ['OPEN', 'a', 'b', 'c', 'd', 'e']);
  }));
});

test('handleStatusWrite_ reports a miss for a row_id not in column A, and writes nothing for it', () => {
  const rows = [['Row_ID'], ['R1']];
  const sheet = makeFakeSheet(rows);
  withFakeContentService(() => withFakeAppsScript(sheet, () => {
    const body = { title: 'Blackboard - Alpha DB',
                   updates: [{ row_id: 'NOT-ON-THE-SHEET', values: ['OPEN', '', '', '', '', ''] }] };
    const result = jsonBody(handleStatusWrite_(body, { folderId: 'folder-1' }));
    assert.equal(result.written, 0);
    assert.deepEqual(result.misses, ['NOT-ON-THE-SHEET']);
    assert.equal(sheet._written.length, 0);
  }));
});

test('handleStatusWrite_ writes several rows and releases the lock even when one misses', () => {
  const rows = [['Row_ID'], ['R1'], ['R2'], ['R3']];
  const sheet = makeFakeSheet(rows);
  withFakeContentService(() => withFakeAppsScript(sheet, () => {
    const body = {
      title: 'Blackboard - Alpha DB',
      updates: [
        { row_id: 'R1', values: ['OPEN', '', '', '', '', ''] },
        { row_id: 'GONE', values: ['OPEN', '', '', '', '', ''] },
        { row_id: 'R3', values: ['CLOSED', '', '', '', '', ''] },
      ],
    };
    const result = jsonBody(handleStatusWrite_(body, { folderId: 'folder-1' }));
    assert.equal(result.written, 2);
    assert.deepEqual(result.misses, ['GONE']);
    assert.equal(sheet._written.length, 2);
  }));
  // withFakeAppsScript's own finally asserts the lock was released.
});
