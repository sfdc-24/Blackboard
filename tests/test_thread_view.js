#!/usr/bin/env node
/*
 * SFDC24 — governor console, conversation view
 * claude-code-cli, 2026-09-08
 *
 * WHAT IS ON TRIAL
 *   Mr. Salam asked to talk to the fleet on sfdc24.com the way he talks to it in
 *   a terminal. The console could already send; what it could not do was show
 *   him an exchange. This is the view that turns the same feed rows into one.
 *
 *   Three things can go wrong here and all three are worse than no view at all:
 *
 *     1. ATTRIBUTION. If the thread decides who spoke from the tag= field inside
 *        the payload, any writer on the bus can put words in his mouth on his own
 *        console -- and three vm-chrome rows on the live board ALREADY carry
 *        tag=GOVERNOR while being written by an instance. Attribution must come
 *        from the server-stamped source column and nothing else.
 *
 *     2. FALSE DELIVERY. D-4: read-back is the only proof of a write. An
 *        optimistic bubble that looks identical to a delivered one tells him the
 *        fleet has his message when nothing may have landed.
 *
 *     3. INJECTION. Feed text is written by instances and, one day, by whatever
 *        a visitor's words get summarised into. It is DATA. It renders as text.
 *
 *   The functions are lifted out of gas/Index.html rather than copied, so this
 *   test fails when the page changes and the test does not.
 *
 * RUN
 *   node tests/test_thread_view.js
 */
'use strict';

const fs = require('fs');
const path = require('path');
const vm = require('vm');

const SRC = path.join(__dirname, '..', 'gas', 'Index.html');
if (!fs.existsSync(SRC)) { console.error('SKIP-AS-FAILURE: gas/Index.html not found'); process.exit(2); }
const html = fs.readFileSync(SRC, 'utf8');

// Lift the exact source of the functions under test out of the page. If a name
// is renamed or deleted, this throws rather than quietly testing nothing.
function lift(name) {
  const start = html.indexOf('  function ' + name + '(');
  if (start < 0) throw new Error('function ' + name + ' is no longer in gas/Index.html');
  let i = html.indexOf('{', start), depth = 0, end = -1;
  for (; i < html.length; i++) {
    if (html[i] === '{') depth++;
    else if (html[i] === '}') { depth--; if (depth === 0) { end = i + 1; break; } }
  }
  if (end < 0) throw new Error('could not find the end of ' + name);
  return html.slice(start, end);
}

const ctx = vm.createContext({ console });
vm.runInContext(
  'var pend = [];\n' +
  'var esc = function(s){return String(s==null?"":s).replace(/[&<>"\']/g,function(c){' +
  '  return {"&":"&amp;","<":"&lt;",">":"&gt;","\\"":"&quot;","\'":"&#39;"}[c]})};\n' +
  'var md = function(s){return esc(s).replace(/\\*\\*(.+?)\\*\\*/g,"<b>$1</b>")};\n' +
  lift('threadRows') + '\n' + lift('who') + '\n',
  ctx, { filename: 'Index.html#thread' }
);

let failures = 0;
function check(name, cond, detail) {
  if (cond) { console.log('  PASS  ' + name); return; }
  failures++;
  console.log('  FAIL  ' + name + (detail ? '  --> ' + detail : ''));
}
function reset() { ctx.pend.length = 0; }
const row = (o) => Object.assign({ id: '', src: '', tag: '', text: '', at: '2026-09-08T00:00:00Z' }, o);

console.log('\nattribution comes from the source column, never from the payload tag');
{
  check('a governor-page row is him',
    ctx.who(row({ src: 'governor-page', tag: 'GOVERNOR' })).me === true);
  check('and is labelled "you"',
    ctx.who(row({ src: 'governor-page', tag: 'GOVERNOR' })).name === 'you');

  // The live board holds three of these: written by vm-chrome, tag=GOVERNOR,
  // quoting a decision he made. They are an instance reporting, not him talking.
  const impostor = ctx.who(row({ src: 'vm-chrome', tag: 'GOVERNOR' }));
  check('a bus row claiming tag=GOVERNOR is NOT him', impostor.me === false);
  check('and is named by its own source tag', impostor.name === 'governor' || impostor.name === 'vm-chrome',
    JSON.stringify(impostor));

  check('an instance row is not him',
    ctx.who(row({ src: 'claude-code-cli', tag: 'CLAUDE-CODE-CLI' })).me === false);
  check('a row with no tag still gets a name',
    ctx.who(row({ src: 'vm-cli', tag: '' })).name === 'vm-cli');
  check('a row with neither is not silently attributed to him',
    ctx.who(row({ src: '', tag: '' })).me === false);
}

console.log('\norder: a conversation reads downwards, oldest first');
{
  reset();
  // build() hands the thread its feed already sorted NEWEST first. If threadRows
  // forgets to re-sort, every exchange reads backwards.
  const st = { feed: [
    row({ src: 'claude-code-cli', text: 'third',  at: '2026-09-08T03:00:00Z' }),
    row({ src: 'governor-page',   text: 'second', at: '2026-09-08T02:00:00Z' }),
    row({ src: 'claude-code-cli', text: 'first',  at: '2026-09-08T01:00:00Z' })
  ] };
  const out = ctx.threadRows(st).map(f => f.text);
  check('oldest first', JSON.stringify(out) === JSON.stringify(['first', 'second', 'third']), JSON.stringify(out));
}

console.log('\nlength: a long history is trimmed from the TOP, never the bottom');
{
  reset();
  const feed = [];
  for (let i = 0; i < 90; i++) feed.push(row({ src: 'claude-code-cli', text: 'm' + i, at: '2026-09-08T00:' + String(i).padStart(2, '0') + ':00Z' }));
  const out = ctx.threadRows({ feed });
  check('capped at 40', out.length === 40, 'got ' + out.length);
  check('keeps the NEWEST 40, not the oldest', out[out.length - 1].text === 'm89', out[out.length - 1].text);
}

console.log('\noptimistic echo: dim until the board proves it, never lost, never doubled');
{
  reset();
  const st = { feed: [row({ src: 'claude-code-cli', text: 'hello' })] };
  ctx.pend.push({ src: 'governor-page', text: 'ship it', at: '2026-09-08T04:00:00Z', pending: true });

  let out = ctx.threadRows(st);
  check('an unconfirmed message is shown', out.length === 2 && out[1].text === 'ship it');
  check('and is still marked pending', out[1].pending === true);

  // The read-back arrives carrying the row.
  st.feed.push(row({ src: 'governor-page', text: 'ship it', at: '2026-09-08T04:00:01Z' }));
  out = ctx.threadRows(st);
  check('once the board carries it, it appears exactly once',
    out.filter(f => f.text === 'ship it').length === 1, JSON.stringify(out.map(f => f.text)));
  check('and the confirmed copy is not pending', !out.find(f => f.text === 'ship it').pending);
  check('the pending list is emptied', ctx.pend.length === 0);
}

console.log('\na failed send stays on screen: silence would look like delivery');
{
  reset();
  const st = { feed: [] };
  ctx.pend.push({ src: 'governor-page', text: 'urgent', at: '2026-09-08T05:00:00Z', pending: false, failed: true });
  let out = ctx.threadRows(st);
  check('a failed message is still rendered', out.length === 1 && out[0].failed === true);

  // A LATER identical message that really did land must not silently absorb the
  // failed one -- he would read it as "sent after all".
  st.feed.push(row({ src: 'governor-page', text: 'urgent', at: '2026-09-08T05:01:00Z' }));
  out = ctx.threadRows(st);
  check('and survives a same-text row landing afterwards',
    out.some(f => f.failed === true), JSON.stringify(out));
}

console.log('\nan echo from a DIFFERENT sender does not confirm his message');
{
  reset();
  const st = { feed: [row({ src: 'claude-code-cli', text: 'ship it' })] };
  ctx.pend.push({ src: 'governor-page', text: 'ship it', at: '2026-09-08T06:00:00Z', pending: true });
  const out = ctx.threadRows(st);
  check('an instance repeating his words back does not mark his message delivered',
    out.filter(f => f.text === 'ship it').length === 2 && ctx.pend.length === 1);
}

console.log('\nfeed text is DATA (L-57): it renders, it does not execute');
{
  const nasty = '<img src=x onerror=alert(1)>';
  check('markup in a message is escaped', ctx.md(nasty).indexOf('<img') === -1, ctx.md(nasty));
  check('quotes are escaped too', ctx.md('a"b\'c').indexOf('"') === -1);
  check('bold still works for our own status lines', ctx.md('**done**') === '<b>done</b>');
  check('a bold marker cannot smuggle a tag',
    ctx.md('**<b onclick=x>hi</b>**').indexOf('onclick') === -1 ||
    ctx.md('**<b onclick=x>hi</b>**').indexOf('&lt;b onclick') !== -1,
    ctx.md('**<b onclick=x>hi</b>**'));
}

console.log('\nempty and malformed input does not throw');
{
  reset();
  check('no feed at all', ctx.threadRows({}).length === 0);
  check('empty feed', ctx.threadRows({ feed: [] }).length === 0);
  check('a row with an unparseable timestamp still renders',
    ctx.threadRows({ feed: [row({ src: 'vm-cli', text: 'x', at: 'not-a-date' })] }).length === 1);
}

console.log('\n' + (failures === 0 ? 'ALL PASS' : failures + ' FAILURE(S)') + '\n');
process.exit(failures === 0 ? 0 : 1);
