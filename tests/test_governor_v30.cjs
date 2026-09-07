const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

function load(properties = {}) {
  const context = vm.createContext({ PropertiesService: { getScriptProperties: () => ({
    getProperty: key => properties[key] || null,
  }) } });
  for (const file of ['Code.gs', 'Monitor.gs']) {
    vm.runInContext(fs.readFileSync(path.join(__dirname, '..', 'apps-script/governor-page-api', file), 'utf8'), context);
  }
  return context;
}

test('v30 independent TTS kill switch survives source reconciliation', () => {
  assert.equal(load({ OPENAI_KEY: 'mock-only', TTS_ENABLED: 'off' }).ttsConfigured_(), false);
  assert.equal(load({ OPENAI_KEY: 'mock-only', TTS_ENABLED: 'OFF' }).ttsConfigured_(), false);
  assert.equal(load({ OPENAI_KEY: 'mock-only' }).ttsConfigured_(), true);
});

function scan(rows) {
  const context = load();
  context.sheet_ = () => ({
    sheet: { getLastRow: () => rows.length + 1,
      getRange: () => ({ getValues: () => rows }) },
    hdr: { row: 1, names: ['Timestamp', 'Payload', 'Source_Tag'],
      idx: { Timestamp: 0, Payload: 1, Source_Tag: 2 } },
  });
  context.iso_ = value => String(value);
  return context.scanBoard_();
}

test('v30 monitor uses readable timestamps despite lexically later malformed cells', () => {
  const valid = new Date(Date.now() - 8 * 3600000).toISOString();
  const result = scan([['payload in date column', '', 'fixture'], [valid, '', 'fixture']]);
  assert.equal(result.newestTs, valid);
  assert.equal(result.unreadableTs, 1);
  assert.ok(result.ageHours >= 8 && result.ageHours < 8.1);
});

test('v30 monitor retains unknown clock state when every timestamp is unreadable', () => {
  const result = scan([['not a timestamp', '', 'fixture'], ['BCB|v=1|payload', '', 'fixture']]);
  assert.equal(result.ageHours, null);
  assert.equal(result.newestTs, '');
  assert.equal(result.unreadableTs, 2);
});

test('v30 monitor retains an alarm with an unreadable timestamp', () => {
  const result = scan([['invalid clock', 'ANDON|fixture alarm', 'fixture']]);
  assert.equal(result.andon.text, 'ANDON|fixture alarm');
  assert.equal(result.andon.src, 'fixture');
});
