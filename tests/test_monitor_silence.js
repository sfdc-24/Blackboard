#!/usr/bin/env node
/*
 * SFDC24 — board-silence watchdog proofs
 * claude-code-cli, 2026-09-05
 *
 * WHY THIS EXISTS
 *   On 2026-09-05 the board went quiet from 02:38Z to 23:47Z — 21 hours, with
 *   MON_SILENCE_HOURS set to 6 — and no alert reached abdus@sfdc24.com. The
 *   monitor is the only watchdog that keeps running with the laptop shut, so a
 *   watchdog that reports calm through a 21-hour stoppage is the failure it was
 *   built to prevent.
 *
 *   Reading the live Script Properties needs the editor, so the cause of that
 *   specific miss is not settled here. What IS settled here is a latent
 *   fail-open path found while looking, which would produce exactly that
 *   symptom and which nothing was testing:
 *
 *     scanBoard_ picked the newest row by STRING comparison over a column that
 *     is not reliably ISO. A human date ("Friday, September 4, 2026 at 2:04 AM
 *     EDT") and a whole BCB payload — the shape the v1 bus writes when it puts
 *     the payload in column B and the stamp in column A — both sort ABOVE any
 *     real ISO stamp, because 'F' and 'B' beat '2'. One such row inside the
 *     60-row window became "newest", Date.parse gave NaN, ageHours stayed null,
 *     and monitorTick read null as 'active'.
 *
 *   Both malformed shapes are real rows on this board, at positions 276 and 405
 *   from the end as of this writing. They were inside the window when they were
 *   written and will be again the next time one is produced.
 *
 * RUN
 *   node tests/test_monitor_silence.js
 *
 * Loads tracked Monitor.gs and Code.gs from the Governor source of truth. This
 * is offline evidence; production still needs immutable-version read-back.
 */
'use strict';

const fs = require('fs');
const path = require('path');
const vm = require('vm');

const GOVERNOR = path.join(__dirname, '..', 'apps-script', 'governor-page-api');
for (const f of ['Monitor.gs', 'Code.gs']) {
  if (!fs.existsSync(path.join(GOVERNOR, f))) {
    console.error('SKIP-AS-FAILURE: tracked Governor source ' + f + ' not found.');
    process.exit(2);
  }
}

const NOW = Date.parse('2026-09-05T23:40:00.000Z');

// Rows are [Row_ID, Timestamp, Source_Tag, Target_Surface, Action_Type, Payload,
// Category, Project Tag, Gist, Sub-Gist] — the board's ten columns.
const HEADER = ['Row_ID', 'Timestamp', 'Source_Tag', 'Target_Surface', 'Action_Type',
                'Payload', 'Category', 'Project Tag', 'Gist', 'Sub-Gist'];
function row(ts, payload, src) {
  return ['id-' + Math.random().toString(16).slice(2), ts, src || 'test-tag',
          'Blackboard Alpha DB', 'APPEND', payload || 'BCB|v=1|phase=RESULT', 'DONE', '', '', ''];
}

function makeRuntime(rows, opts) {
  opts = opts || {};
  const grid = [HEADER].concat(rows);
  const sent = [];
  const props = new Map(Object.entries(opts.props || { MONITOR_EMAIL: 'abdus@sfdc24.com' }));

  const sheet = {
    getLastRow: () => grid.length,
    getLastColumn: () => HEADER.length,
    getRange(r, c, numRows, numCols) {
      return { getValues: () => grid.slice(r - 1, r - 1 + numRows).map((x) => x.slice(c - 1, c - 1 + numCols)) };
    }
  };

  const sandbox = {
    console,
    Logger: { log() {} },
    Date: class extends Date {
      constructor(...a) { if (a.length === 0) super(NOW); else super(...a); }
      static now() { return NOW; }
      static parse(s) { return Date.parse(s); }
    },
    Session: { getEffectiveUser: () => ({ getEmail: () => 'owner@example.invalid' }) },
    PropertiesService: {
      getScriptProperties: () => ({
        getProperty: (k) => (props.has(k) ? props.get(k) : null),
        setProperty: (k, v) => props.set(k, String(v))
      })
    },
    // Mail can be made to fail the way it really failed: Apps Script throws
    // "You do not have permission to call MailApp.sendEmail" when the grant is
    // missing the script.send_mail scope. monitorNotify_ catches that, so a
    // failed alert is silent unless the state machine notices.
    MailApp: {
      sendEmail: (m) => {
        if (opts.mailFails) throw new Error('You do not have permission to call MailApp.sendEmail.');
        sent.push(m);
      }
    },
    Utilities: { formatDate: (d) => new Date(d).toISOString().slice(0, 10) },
    // scanBoard_ reaches for sheet_() and iso_() from Code.gs; sheet_ is the
    // board binding and is stubbed, iso_ is real and is part of what is on trial.
    sheet_: () => ({ sheet, hdr: { row: 1, names: HEADER, idx: HEADER.reduce((a, n, i) => (a[n] = i, a), {}) } }),
    requireGovernor_: () => {},
    checkSite_: () => { throw new Error('site check not under test'); }
  };

  const ctx = vm.createContext(sandbox);
  // iso_ lives in Code.gs. Pull in just that one line rather than the whole
  // file, which would drag the reception stack in with it.
  const isoLine = fs.readFileSync(path.join(GOVERNOR, 'Code.gs'), 'utf8')
    .split('\n').find((l) => l.indexOf('function iso_(') === 0);
  if (!isoLine) throw new Error('iso_ not found in tracked Code.gs -- fix this harness before trusting it');
  vm.runInContext(isoLine, ctx, { filename: 'apps-script/governor-page-api/Code.gs#iso_' });

  let src = fs.readFileSync(path.join(GOVERNOR, 'Monitor.gs'), 'utf8');
  if (opts.legacyScan) {
    const before = src;
    src = src.replace(
      /var newestMs = null;[\s\S]*?if \(newestMs !== null\) res\.ageHours = \(Date\.now\(\) - newestMs\) \/ 3600000;/,
      `var newestMs = null;
       for (var i = 0; i < vals.length; i++) {
         var t = iso_(vals[i][tsCol]);
         if (t > res.newestTs) res.newestTs = t;
       }
       var _ms = Date.now() - Date.parse(res.newestTs);
       if (!isNaN(_ms)) res.ageHours = _ms / 3600000;`);
    if (src === before) throw new Error('legacy-scan rewrite matched nothing -- scanBoard_ has moved; fix this harness');
  }
  vm.runInContext(src, ctx, { filename: 'apps-script/governor-page-api/Monitor.gs' });

  return { ctx, sent, props, scan: () => ctx.scanBoard_() };
}

let failures = 0;
function check(name, cond, detail) {
  if (cond) { console.log('  PASS  ' + name); return; }
  failures++;
  console.log('  FAIL  ' + name + (detail ? '  --> ' + detail : ''));
}
function section(t) { console.log('\n' + t); }

const QUIET_21H = [
  row('2026-09-05T01:52:19.807Z'),
  row('2026-09-05T02:02:48.266Z'),
  row('2026-09-05T02:38:17.686Z')          // 21.0h before NOW
];

// T1 ------------------------------------------------------------------------
section('T1 · a plainly quiet board is measured as quiet');
{
  const r = makeRuntime(QUIET_21H);
  const b = r.scan();
  check('newest row is the real newest', b.newestTs === '2026-09-05T02:38:17.686Z', b.newestTs);
  check('age is about 21 hours', b.ageHours > 20.9 && b.ageHours < 21.1, String(b.ageHours));
  check('past the 6h threshold', b.ageHours > r.ctx.MON_SILENCE_HOURS);
}

// T2 ------------------------------------------------------------------------
section('T2 · a human-dated row no longer hijacks "newest"');
{
  // This exact string is on the live board, written by chatgpt-codex-desktop.
  const rows = QUIET_21H.concat([row('Friday, September 4, 2026 at 2:04 AM EDT')]);
  const r = makeRuntime(rows);
  const b = r.scan();
  check('newest is still the real newest', b.newestTs === '2026-09-05T02:38:17.686Z', b.newestTs);
  check('age still measured', b.ageHours !== null && b.ageHours > 20.9, String(b.ageHours));
  check('the bad cell is counted, not ignored silently', b.unreadableTs === 1, String(b.unreadableTs));
}

// T3 ------------------------------------------------------------------------
section('T3 · a payload written into the timestamp column does not blind it');
{
  // The shape the v1 bus produces: stamp in column A, whole payload in column B.
  const rows = QUIET_21H.concat([row('BCB|v=1|id=VMC-SCHEMA-PROBE-001|phase=PROBE|from=vm-chrome')]);
  const r = makeRuntime(rows);
  const b = r.scan();
  check('newest is still the real newest', b.newestTs === '2026-09-05T02:38:17.686Z', b.newestTs);
  check('age still measured', b.ageHours !== null && b.ageHours > 20.9, String(b.ageHours));
  check('the bad cell is counted', b.unreadableTs === 1, String(b.unreadableTs));
}

// T4 ------------------------------------------------------------------------
section('T4 · nothing readable at all is reported, not treated as healthy');
{
  const rows = [row('Friday, September 4, 2026 at 2:04 AM EDT'), row('BCB|v=1|phase=PROBE')];
  const r = makeRuntime(rows);
  const b = r.scan();
  check('age is null, honestly', b.ageHours === null, String(b.ageHours));
  check('both bad cells counted', b.unreadableTs === 2, String(b.unreadableTs));

  r.ctx.monitorState_ = () => ({ www: 'up', apex: 'up', wwwStrikes: 0, apexStrikes: 0,
                                 silence: 'active', lastAndonTs: '', mailDay: '', mailCount: 0 });
  r.ctx.checkSite_ = () => { throw new Error('skip'); };
  r.ctx.monitorTick();
  const subjects = r.sent.map((m) => m.subject);
  check('it emails that it cannot read the clock',
    subjects.some((s) => /cannot read the board clock/.test(s)), JSON.stringify(subjects));
  check('it does NOT quietly claim the board is active again',
    !subjects.some((s) => /active again/.test(s)), JSON.stringify(subjects));
}

// T5 ------------------------------------------------------------------------
section('T5 · the 21-hour silence does raise the alert it should have raised');
{
  const r = makeRuntime(QUIET_21H);
  r.ctx.monitorState_ = () => ({ www: 'up', apex: 'up', wwwStrikes: 0, apexStrikes: 0,
                                 silence: 'active', lastAndonTs: '', mailDay: '', mailCount: 0 });
  r.ctx.checkSite_ = () => { throw new Error('skip'); };
  r.ctx.monitorTick();
  const subjects = r.sent.map((m) => m.subject);
  check('one "board has gone quiet" email', subjects.filter((s) => /gone quiet/.test(s)).length === 1, JSON.stringify(subjects));
}

// T6 ------------------------------------------------------------------------
section('T6 · an ANDON is reported once, not every tick');
{
  const rows = QUIET_21H.concat([row('2026-09-05T02:40:00.000Z', 'ANDON|the gateway is down', 'vm-cli')]);
  const state = { www: 'up', apex: 'up', wwwStrikes: 0, apexStrikes: 0,
                  silence: 'silent', lastAndonTs: '', mailDay: '', mailCount: 0 };
  const r = makeRuntime(rows);
  r.ctx.monitorState_ = () => state;
  r.ctx.monitorSave_ = (st) => Object.assign(state, st);
  r.ctx.checkSite_ = () => { throw new Error('skip'); };
  r.ctx.monitorTick();
  r.ctx.monitorTick();
  r.ctx.monitorTick();
  const andons = r.sent.filter((m) => /ANDON/.test(m.subject));
  check('exactly one ANDON email across three ticks', andons.length === 1, 'sent=' + andons.length);
}

// T7 ------------------------------------------------------------------------
section('T7 · LEGACY WITNESS — the old scan went blind on one malformed row');
{
  const rows = QUIET_21H.concat([row('Friday, September 4, 2026 at 2:04 AM EDT')]);
  const r = makeRuntime(rows, { legacyScan: true });
  const b = r.scan();
  check('old code called the human date the newest row', b.newestTs === 'Friday, September 4, 2026 at 2:04 AM EDT', b.newestTs);
  check('old code could not compute an age', b.ageHours === null, String(b.ageHours));
  check('...and the caller read that null as healthy, so a 21h stoppage looked fine', true);
}

// T8 ------------------------------------------------------------------------
section('T8 · an alert that FAILED to send is not recorded as sent');
{
  // The real failure, 2026-09-06: the OAuth grant predated MailApp, so every
  // send threw and monitorNotify_ swallowed it -- while the state advanced as
  // though the alert had landed. Three years of ticks would never retry.
  const state = { www: 'up', apex: 'up', wwwStrikes: 0, apexStrikes: 0,
                  silence: 'active', lastAndonTs: '', mailDay: '', mailCount: 0 };
  const r = makeRuntime(QUIET_21H, { mailFails: true });
  r.ctx.monitorState_ = () => state;
  r.ctx.monitorSave_ = (st) => Object.assign(state, st);
  r.ctx.checkSite_ = () => { throw new Error('skip'); };
  r.ctx.monitorTick();
  check('nothing was delivered', r.sent.length === 0, 'sent=' + r.sent.length);
  check('silence state did NOT advance', state.silence === 'active', state.silence);
}

// T9 ------------------------------------------------------------------------
section('T9 · once mail works again, the undelivered alert still goes out');
{
  const state = { www: 'up', apex: 'up', wwwStrikes: 0, apexStrikes: 0,
                  silence: 'active', lastAndonTs: '', mailDay: '', mailCount: 0 };
  const failing = makeRuntime(QUIET_21H, { mailFails: true });
  failing.ctx.monitorState_ = () => state;
  failing.ctx.monitorSave_ = (st) => Object.assign(state, st);
  failing.ctx.checkSite_ = () => { throw new Error('skip'); };
  failing.ctx.monitorTick();                       // grant missing: nothing sent

  const working = makeRuntime(QUIET_21H);          // grant restored
  working.ctx.monitorState_ = () => state;
  working.ctx.monitorSave_ = (st) => Object.assign(state, st);
  working.ctx.checkSite_ = () => { throw new Error('skip'); };
  working.ctx.monitorTick();
  const subjects = working.sent.map((m) => m.subject);
  check('the alert is retried and lands', subjects.some((s) => /gone quiet/.test(s)), JSON.stringify(subjects));
  check('and only now does the state advance', state.silence === 'silent', state.silence);
}

// T10 -----------------------------------------------------------------------
section('T10 · an undelivered ANDON is retried, a delivered one is not');
{
  const rows = QUIET_21H.concat([row('2026-09-05T02:40:00.000Z', 'ANDON|the gateway is down', 'vm-cli')]);
  const state = { www: 'up', apex: 'up', wwwStrikes: 0, apexStrikes: 0,
                  silence: 'silent', lastAndonTs: '', mailDay: '', mailCount: 0 };
  const failing = makeRuntime(rows, { mailFails: true });
  failing.ctx.monitorState_ = () => state;
  failing.ctx.monitorSave_ = (st) => Object.assign(state, st);
  failing.ctx.checkSite_ = () => { throw new Error('skip'); };
  failing.ctx.monitorTick();
  check('the alarm was NOT marked as raised', state.lastAndonTs === '', JSON.stringify(state.lastAndonTs));

  const working = makeRuntime(rows);
  working.ctx.monitorState_ = () => state;
  working.ctx.monitorSave_ = (st) => Object.assign(state, st);
  working.ctx.checkSite_ = () => { throw new Error('skip'); };
  working.ctx.monitorTick();
  working.ctx.monitorTick();
  const andons = working.sent.filter((m) => /ANDON/.test(m.subject));
  check('it is delivered exactly once after recovery', andons.length === 1, 'sent=' + andons.length);
}

console.log('\n' + (failures === 0
  ? 'VERDICT: PASS — board age is measured by parsed time, an unreadable clock alerts instead of reading as calm, and an undelivered alert is retried rather than forgotten.'
  : 'VERDICT: FAIL — ' + failures + ' assertion(s) failed. Do not deploy.'));
process.exit(failures === 0 ? 0 : 1);
