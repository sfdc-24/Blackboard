// Pure-function tests for the R-STATUS gateway actions in Code.js.
// No Apps Script runtime, no network: Code.js exports these under `module` only
// when it exists (a no-op in production - see the bottom of Code.js).
//
// Run: node --test gas-bus/test_status_write.js
'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const {
  validateStatusUpdates_, statusRangeA1_, columnLetter_,
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
