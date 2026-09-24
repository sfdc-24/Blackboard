// Mr Salam, 2026-09-24: "Can you not mention or promise my name in any
// response." He had just been told, by the offline reply, that "Mr. Salam
// will see it" and to "reach him at abdus@sfdc24.com". No visitor-facing text
// in the Governor - the offline reply or the model's instructions - may name
// him or give his address. Comments may; the signed-in Governor's own label
// (GOVERNOR_NAME, shown only to him) may.
const test = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');

const src = fs.readFileSync(path.join(__dirname, '..', 'apps-script', 'governor-page-api', 'Code.js'), 'utf8');
const code = src
  .replace(/\/\*[\s\S]*?\*\//g, '')
  .split(/\r?\n/)
  .map((line) => line.replace(/^\s*\/\/.*$/, ''))
  .filter((line) => !/getProperty\('GOVERNOR_NAME'\)/.test(line));

test('no visitor-facing Governor text names him or gives his address', () => {
  const hits = code.map((line, i) => [i + 1, line]).filter(([, line]) => /abdus|salam/i.test(line));
  assert.deepStrictEqual(hits, []);
});

test('the offline reply promises no person and offers the request form', () => {
  const fn = src.slice(src.indexOf('function offline_('));
  const offline = fn.slice(fn.indexOf('var note ='), fn.indexOf(';', fn.indexOf('var note =')) + 1);
  assert.match(offline, /request form at www\.sfdc24\.com\/intake\//);
  assert.doesNotMatch(offline, /will see it|reach him|Mr\.? Salam|abdus/i);
});

test('the model is told never to name a person or promise one will reply', () => {
  assert.match(src, /NO NAMES, NO PROMISES ABOUT PEOPLE/);
});
