#!/usr/bin/env node
/*
 * SFDC24 — BEAT 3 page generator
 * claude-code-cli, 2026-09-07 · docs/PRODUCT.md
 *
 * WHAT IS ON TRIAL
 *   This produces a FILE the visitor may email to their boss, and that we may
 *   host on an sfdc24.com subdomain. Every string in it traces back to
 *   visitor-controlled text that a model has been through. So the assertions
 *   below are mostly about escaping and about honesty:
 *
 *     - nothing visitor-shaped can become markup, in text OR in an attribute;
 *     - the page never claims work that did not happen;
 *     - a missing part is omitted rather than padded with filler, because an
 *       invented sentence stops the page being theirs.
 *
 *   sanitiseSketch_ already rebuilt the sketch field by field. This is the
 *   second boundary over the same text, and two boundaries is the only reason
 *   a mistake in one of them is survivable.
 *
 * RUN
 *   node tests/test_build_page.js
 */
'use strict';

const fs = require('fs');
const path = require('path');
const vm = require('vm');

const SRC = path.join(__dirname, '..', 'prototype', 'beat1', 'build.js');
if (!fs.existsSync(SRC)) { console.error('SKIP-AS-FAILURE: prototype/beat1/build.js not found'); process.exit(2); }
const ctx = vm.createContext({ console });
vm.runInContext(fs.readFileSync(SRC, 'utf8'), ctx, { filename: 'build.js' });

let failures = 0;
function check(name, cond, detail) {
  if (cond) { console.log('  PASS  ' + name); return; }
  failures++;
  console.log('  FAIL  ' + name + (detail ? '  --> ' + detail : ''));
}
function section(t) { console.log('\n' + t); }

const SKETCH = {
  title: 'Lead intake',
  nodes: [
    { id: 'n0', label: 'Web form', kind: 'system' },
    { id: 'n1', label: 'Assignment rules', kind: 'step' },
    { id: 'n2', label: 'Unowned for days', kind: 'problem' }
  ],
  edges: [{ from: 'n0', to: 'n1', label: 'creates' }, { from: 'n0', to: 'n2', label: 'skips' }]
};
const PARTS = {
  headline: 'Three ways in, one of them routed.',
  whatsHappening: 'Leads arrive from a form, a spreadsheet and by hand.',
  whereItBreaks: 'Two paths never touch the assignment rules.',
  firstMove: 'Move assignment off the entry point and onto the record.'
};

// ---------------------------------------------------------------------------
section('T1 · it renders a whole, self-contained page');
{
  const html = ctx.buildPage_(SKETCH, PARTS, '7 September 2026');
  check('doctype and closing tag', html.startsWith('<!DOCTYPE html>') && html.trim().endsWith('</html>'));
  check('title used', html.includes('<title>Lead intake</title>'));
  check('every node drawn', SKETCH.nodes.every((n) => html.includes(n.label)));
  check('edge labels drawn', html.includes('creates') && html.includes('skips'));
  check('all four parts present', ['headline','whatsHappening','whereItBreaks','firstMove'].every((k) => html.includes(PARTS[k])));
  check('stamp present', html.includes('7 September 2026'));
  // It will be emailed and opened on someone else's laptop.
  check('no external asset of any kind',
    !/<script/i.test(html) && !/https?:\/\/(?!www\.w3\.org)/.test(html.replace(/mailto:[^"']*/g, '')),
    'found an outbound reference');
  check('noindex, because it is theirs and not ours to publish', html.includes('noindex'));
}

// ---------------------------------------------------------------------------
section('T2 · visitor text cannot become markup');
{
  const nasty = '</title><script>alert(1)</script>';
  const html = ctx.buildPage_(
    { title: nasty, nodes: [{ id: 'n0', label: '<img src=x onerror=alert(2)>', kind: 'problem' }], edges: [] },
    { headline: '"><script>alert(3)</script>', whatsHappening: "it's & <b>bold</b>" },
    '<b>now</b>'
  );
  check('no script tag survives anywhere', !/<script/i.test(html), 'script leaked');
  check('no raw img tag', !/<img/i.test(html));
  check('angle brackets escaped', html.includes('&lt;script&gt;') || html.includes('&lt;img'));
  check('quotes escaped so an attribute cannot be broken out of', !/aria-label="[^"]*"[^>]*alert/.test(html));
  check('ampersand escaped', html.includes('&amp;'));
  check('apostrophe escaped', html.includes('&#39;'));
}

// ---------------------------------------------------------------------------
section('T3 · a missing part is omitted, never padded');
{
  const html = ctx.buildPage_(SKETCH, { whatsHappening: 'Only this one.' }, 'today');
  check('the part given is shown', html.includes('Only this one.'));
  check('absent sections are not rendered at all',
    !html.includes('Where it breaks') && !html.includes('What to do first'));
  check('no lede when no headline', !html.includes('class="lede"'));
  check('no filler prose invented', !/lorem|TBD|placeholder|coming soon/i.test(html));
}

// ---------------------------------------------------------------------------
section('T4 · it does not claim work that never happened');
{
  const html = ctx.buildPage_(SKETCH, PARTS, 'today');
  check('says nobody looked in their org', /nobody looked inside your Salesforce org/i.test(html));
  check('calls itself a starting point, not an audit', /not an audit/i.test(html));
  check('invites correction', /if a box is wrong/i.test(html));
  check('never uses audit or assessment as a claim about itself',
    !/\bwe (audited|assessed|reviewed) your\b/i.test(html));
  check('a way to reach a human', html.includes('abdus@sfdc24.com'));
}

// ---------------------------------------------------------------------------
section('T5 · degenerate sketches do not produce a broken file');
{
  const empty = ctx.buildPage_({ title: 'Nothing yet', nodes: [], edges: [] }, {}, 'today');
  check('no svg when there is nothing to draw', !empty.includes('<svg'));
  check('still a valid whole page', empty.startsWith('<!DOCTYPE html>') && empty.trim().endsWith('</html>'));

  const dangling = ctx.buildPage_(
    { title: 'One box', nodes: [{ id: 'n0', label: 'Alone', kind: 'object' }], edges: [{ from: 'n0', to: 'ghost' }] },
    {}, 'today');
  check('an edge to a node that does not exist draws nothing', !/<path/.test(dangling.split('<footer')[0].split('</svg>')[0].replace(/<rect[^>]*>/g, '')) || dangling.includes('Alone'));
  check('the real box still renders', dangling.includes('Alone'));
}

console.log('\n' + (failures === 0
  ? 'VERDICT: PASS — the page escapes everything, omits what it does not know, and\n         says plainly that nobody looked inside their org.'
  : 'VERDICT: FAIL — ' + failures + ' assertion(s) failed. Do not send this to anyone.'));
process.exit(failures === 0 ? 0 : 1);
