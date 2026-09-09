// Mutation control for test/contracts.test.mjs.
//
// WHY THIS EXISTS, AND WHY IT IS NOT THE SUITE
//
// The suite is green. That says "my tests passed", not "my tests would catch
// the defect coming back". Those are different claims, and this repository has
// already shipped a guard that was green and inert — twice — while the thing it
// was written to prevent sat live in production.
//
// So each case below REVERTS one fix the review forced and requires the suite
// to fail on the NAMED test. A fix nothing notices the loss of is not covered,
// however many assertions surround it.
//
// TWO PROPERTIES THIS HARNESS NEEDS, BOTH LEARNED THE HARD WAY:
//
//   1. Every mutation verifies itself. A replacement whose anchor no longer
//      matches changes nothing, the suite passes, and the run reports a healthy
//      guard as dead — or a dead one as healthy. An anchor miss is a hard
//      failure here, never a skipped case.
//
//   2. It cannot lose your source. An earlier version restored only in a
//      `finally`, was interrupted, and left a deliberately broken timeout in
//      notify.js. Originals are stashed up front and restored on exit and on
//      every signal. Each run is also time-bounded, because a mutation can make
//      a test HANG rather than fail, and an unbounded harness then hangs with
//      it — which is how that interruption happened.
//
// Run: npm run mutate

import fs from 'node:fs';
import os from 'node:os';
import crypto from 'node:crypto';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { spawnSync } from 'node:child_process';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');

const MUTATIONS = [
  {
    blocker: 1,
    name: 'an over-budget query is silently truncated again instead of refused',
    file: 'src/reception.js',
    from: '  if (question.length > MAX_QUERY_CHARS) {',
    to: '  if (false) {',
    expect: /over-budget query is refused/,
  },
  {
    blocker: 2,
    name: 'the server-minted ct is thrown away, as the rollback-era client did',
    file: 'src/reception.js',
    from: '      conv.ct = String(data.ct);',
    to: '      /* dropped */',
    expect: /server-minted ct is kept/,
  },
  {
    blocker: 3,
    name: 'the Graph send loses its absolute timeout',
    file: 'src/notify.js',
    from: '  const timer = setTimeout(() => ctl.abort(), timeoutMs);',
    to: '  const timer = setTimeout(() => {}, timeoutMs);',
    expect: /hung Graph call times out/,
  },
  {
    blocker: 3,
    name: 'a wamid is called a delivery again',
    file: 'src/notify.js',
    from: '    return { ok: true, id: data?.messages?.[0]?.id };',
    to: '    return { ok: true, delivered: true, id: data?.messages?.[0]?.id };',
    expect: /claims delivery to a phone|no field that reads as a delivery/,
  },
  {
    blocker: 4,
    name: 'the append trusts the bus instead of reading the row back',
    file: 'src/board.js',
    from: '  const seen = await countRowId(title, rowId);',
    to: '  const seen = { count: 1 };',
    expect: /confirmed by reading the row back|missing row is unresolved|duplicate Row_ID is reported/,
  },
  {
    blocker: 4,
    name: 'a row that cannot be found is reported as a clean failure',
    file: 'src/board.js',
    // First attempt at this one only replaced the FIRST line of a two-line
    // message, and the word "stale" survived on the second — so the mutation
    // "passed" and I nearly filed a coverage gap that was my own bad anchor.
    // Mutate the ambiguity flag itself, which is the claim under test.
    from: '    ok: false, verified: false, matches: 0, rowId, busSaid,\n    ambiguous: true,',
    to: '    ok: false, verified: false, matches: 0, rowId, busSaid,\n    ambiguous: false,',
    expect: /missing row is unresolved, never a retry/,
  },
  {
    blocker: 5,
    name: 'the authorize URL drops its one-time state nonce',
    file: 'src/oauth.js',
    from: "  url.searchParams.set('state', freshState());",
    to: '  /* no state */',
    expect: /one-time state nonce|rejects an unbound code/,
  },
  {
    blocker: 5,
    name: 'a state nonce becomes replayable',
    file: 'src/oauth.js',
    from: 'function consumeState(state) {',
    to: 'function consumeState(state) {\n  if (state) return true;',
    expect: /one-time state nonce|rejects an unbound code/,
  },
  {
    blocker: 5,
    name: 'the callback listens on every interface again',
    file: 'src/oauth.js',
    from: '  server.listen(config.port, config.oauthHost, () => {',
    to: "  server.listen(config.port, '0.0.0.0', () => {",
    expect: /listens on loopback/,
  },
  {
    blocker: 'acceptance',
    name: 'the recycle goes back to accepting a token that expires inside the window',
    file: 'src/events-ws.js',
    from: 'const RECYCLE_MIN_TTL_MS = TOKEN_RECYCLE_MS + 5 * 60_000;',
    to: 'const RECYCLE_MIN_TTL_MS = 5 * 60_000;',
    expect: /outlives the next window|horizon that outlasts its own window/,
  },

  // ── Rounds three to seven, folded in from a parallel session's ad-hoc
  //    harness so there is ONE gate rather than two. Two agents each keeping a
  //    private mutation script is how a fix ends up covered in one place and
  //    unguarded in the other.
  {
    blocker: '4-board',
    name: 'a stale read-back consumes the whole retry budget again',
    file: 'src/board.js',
    from: '      if (count > 0) return { count };',
    to: '      return { count };',
    expect: /stale read-back is retried/,
  },
  {
    blocker: '4-coverage',
    name: 'coverage counts segments whose summary never arrived',
    file: 'src/assistant.js',
    from: '    covered: res.ok ? covered : 0,',
    to: '    covered: plan.covered,',
    expect: /coverage counts only the segments that survived/,
  },
  {
    blocker: '4-evidence',
    name: 'the board finding stops naming the persona that produced it',
    file: 'src/assistant.js',
    from: '    PERSONA_CAVEAT,\n  ].filter(Boolean).join(\' \');',
    to: '  ].filter(Boolean).join(\' \');',
    expect: /says which persona produced the summary/,
  },
  {
    blocker: '4-prompt',
    name: 'a line placed only in part is credited as fully covered',
    file: 'src/prompt.js',
    from: '        groups.push({ lines: [line.slice(line.length - room)], covered: 0 });',
    to: '        groups.push({ lines: [line.slice(line.length - room)], covered: 1 });',
    expect: /placed only in part is not counted as covered/,
  },
  {
    blocker: '4-evidence',
    name: 'a failed summary handoff never reaches the board row',
    file: 'src/assistant.js',
    from: '      session.failures.push(sent.error);\n    }\n    console.log(sent.ok',
    to: '    }\n    console.log(sent.ok',
    expect: /failed summary handoff reaches the durable evidence/,
  },
  {
    blocker: '5-hold',
    name: 'the wrap-up defaults back on, before a trusted route exists',
    file: 'src/config.js',
    from: "wrapUpEnabled: optional('WRAP_UP_ENABLED', 'false') === 'true',",
    to: "wrapUpEnabled: optional('WRAP_UP_ENABLED', 'true') === 'true',",
    expect: /wrap-up is held by default|README does not promise/,
  },
  {
    blocker: '5-hold',
    name: 'the hold is bypassed and a held wrap-up runs anyway',
    file: 'src/assistant.js',
    from: '  if (!config.wrapUpEnabled) {',
    to: '  if (false) {',
    expect: /wrap-up is held by default/,
  },
  {
    blocker: '5-docs',
    name: 'the README promises summaries the default never produces',
    file: 'README.md',
    from: '**With the defaults, nothing from a call reaches the shared board.** The',
    to: 'It also puts summaries on a phone and a shared board. The',
    expect: /README does not promise a capability that is held/,
  },
  {
    blocker: '6-ack',
    name: 'a refused build_connection is treated as a successful one',
    file: 'src/events-ws.js',
    from: '        if (success) {\n          this.attempts = 0;',
    to: '        if (true) {\n          this.attempts = 0;',
    expect: /refused build_connection is a failure/,
  },
  {
    blocker: '7-hang',
    name: 'the connect bound becomes effectively infinite',
    file: 'src/events-ws.js',
    from: '      }, config.connectTimeoutMs);',
    to: '      }, 24 * 60 * 60_000);',
    expect: /never completes the upgrade still fails/,
  },
  {
    blocker: '7-own',
    name: 'the reservation check goes back to existence instead of ownership',
    file: 'src/rtms.js',
    from: '  if (active.get(streamId)?.token !== token) {',
    to: '  if (!active.has(streamId)) {',
    expect: /reservation is owned/,
  },
  {
    blocker: '7-own',
    name: 'an aborting attempt evicts a newer reservation',
    file: 'src/rtms.js',
    from: '    releaseIfOurs(streamId, token);   // never hold a reservation we cannot fulfil',
    to: '    active.delete(streamId);   // never hold a reservation we cannot fulfil',
    expect: /reservation is owned/,
  },
  {
    blocker: '7-own',
    name: 'a late leave from a superseded client evicts the live one',
    file: 'src/rtms.js',
    from: '    // A late leave from a superseded client must not evict the live one.\n    releaseIfOurs(streamId, token);',
    to: '    active.delete(streamId);',
    expect: /reservation is owned/,
  },

  {
    blocker: '10-stale',
    name: 'a stale backoff survives a successful ack and tears the socket down',
    file: 'src/events-ws.js',
    from: '          this.clearPendingReconnect();\n          console.log(\'[events] Zoom acknowledged the connection — listening\');',
    to: '          console.log(\'[events] Zoom acknowledged the connection — listening\');',
    expect: /stale backoff does not tear down the healthy socket/,
  },
  {
    blocker: '10-stale',
    name: 'the backoff handle is not tracked, so nothing can cancel it',
    file: 'src/events-ws.js',
    from: '    this.reconnectTimer = setTimeout(() => {\n      this.reconnectTimer = null;',
    to: '    setTimeout(() => {',
    expect: /stale backoff does not tear down the healthy socket/,
  },

  {
    blocker: '9-join',
    name: 'a join that throws leaves the stream reserved and the call unrecoverable',
    file: 'src/rtms.js',
    from: '    releaseIfOurs(streamId, token);   // a join that never started can never leave',
    to: '    /* the reservation stays */',
    expect: /join that throws releases the reservation/,
  },

  {
    blocker: '8-startup',
    name: 'a startup failure before the socket exists is only logged again',
    file: 'src/index.js',
    from: '    if (!err.retryScheduled) socket.scheduleReconnect();',
    to: '    /* logged only */',
    expect: /startup failure that never reached a socket still retries/,
  },
];

// ── Two guards that stashing originals cannot provide ──────────────────────
//
// THE STASH DEFENCE HAS A HOLE, AND IT WAS FOUND BY FALLING INTO IT.
//
// One run was started in the background and a second was started before it
// finished. The second stashed an ALREADY-MUTATED src/events-ws.js as its
// "original" and faithfully restored that at the end. The tree looked clean —
// one modified file, expected — and the reverted fix rode inside a legitimate
// diff. It reported 27/27 caught while the source on disk carried a defect the
// harness itself had introduced.
//
// The harness's own header says it cannot lose your source. It could, and this
// is how: not by failing to restore, but by restoring the wrong thing. A
// snapshot is only a safe baseline if nothing else is writing.

const LOCK = path.join(
  os.tmpdir(),
  `zoom-agent-mutate-${crypto.createHash('sha256').update(ROOT).digest('hex').slice(0, 12)}.lock`,
);

function lockHolder() {
  try {
    const pid = Number(fs.readFileSync(LOCK, 'utf8').trim());
    if (!Number.isInteger(pid) || pid <= 0) return null;
    process.kill(pid, 0);     // signal 0 tests for existence, sends nothing
    return pid;               // alive, and holding it
  } catch (err) {
    // ENOENT: no lock. ESRCH: the holder died without cleaning up — stale, take it.
    if (err?.code === 'ESRCH') { try { fs.unlinkSync(LOCK); } catch { /* raced */ } }
    return null;
  }
}

const holder = lockHolder();
if (holder !== null) {
  console.error(
    `\nREFUSING TO RUN — pid ${holder} is already mutating this checkout.\n`
    + '\nTwo harnesses cannot share a working tree. The second one snapshots the\n'
    + "first one's mutations as pristine source and restores THOSE, which silently\n"
    + 'reverts a real fix inside a diff that looks intentional.\n'
    + `\nWait for it, or remove ${LOCK} if you are certain it is dead.\n`,
  );
  process.exit(2);
}
fs.writeFileSync(LOCK, String(process.pid));
function releaseLock() {
  try {
    if (fs.readFileSync(LOCK, 'utf8').trim() === String(process.pid)) fs.unlinkSync(LOCK);
  } catch { /* already gone */ }
}

// PREFLIGHT: every anchor must be present BEFORE anything is touched.
//
// Mid-run this is already reported per case as ANCHOR LOST — but by then other
// files have been mutated, and, worse, a missing anchor at the START means the
// source is not pristine. Checking first is what turns "one case cannot apply"
// into "stop: this tree is not a valid baseline", which is the difference that
// mattered above. It is also the check that found the residue by hand.
{
  const missing = MUTATIONS.filter((m) => applyAnchor(fs.readFileSync(path.join(ROOT, m.file), 'utf8'), m.from, m.to) === null);
  if (missing.length) {
    releaseLock();
    console.error(`\nREFUSING TO RUN — ${missing.length} of ${MUTATIONS.length} anchors are not in the source:\n`);
    for (const m of missing) console.error(`  ${m.file}  [b${m.blocker}] ${m.name}`);
    console.error(
      '\nEither an anchor is stale, or this tree is already carrying a mutation —\n'
      + 'an interrupted run, or a second harness that restored the wrong baseline.\n'
      + 'Snapshotting it now would make that mutation the new "original".\n'
      + 'Check `git diff` against the sources above before running again.\n',
    );
    process.exit(2);
  }
}

// Stash every original BEFORE the first mutation, and put them back on any exit
// path. `finally` alone does not survive a kill.
const originals = new Map();
for (const m of MUTATIONS) {
  const file = path.join(ROOT, m.file);
  if (!originals.has(file)) originals.set(file, fs.readFileSync(file, 'utf8'));
}
function restoreAll() {
  for (const [file, text] of originals) {
    try { if (fs.readFileSync(file, 'utf8') !== text) fs.writeFileSync(file, text); } catch { /* nothing to do */ }
  }
}
process.on('exit', () => { restoreAll(); releaseLock(); });
for (const sig of ['SIGINT', 'SIGTERM', 'SIGHUP', 'SIGBREAK']) {
  process.on(sig, () => { restoreAll(); releaseLock(); process.exit(130); });
}

/**
 * Apply one anchor, tolerating line endings.
 *
 * A multi-line anchor written with \n does not match a file git checked out
 * with \r\n, and the harness then reports ANCHOR LOST on source that is
 * perfectly fine. On Windows that turns every multi-line mutation into a false
 * alarm — which is exactly the "the tool is broken, not the code" noise that
 * makes people stop running a gate.
 *
 * @returns {string|null} the mutated text, or null if the anchor is absent
 */
function applyAnchor(text, from, to) {
  if (text.includes(from)) return text.replace(from, to);
  if (!from.includes('\n')) return null;
  const pattern = new RegExp(
    from.split('\n').map((line) => line.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')).join('\\r?\\n'),
  );
  if (!pattern.test(text)) return null;
  return text.replace(pattern, to.split('\n').join('\r\n'));
}

let failures = 0;
console.log(`\nmutation control — ${MUTATIONS.length} cases\n`);

for (const m of MUTATIONS) {
  const file = path.join(ROOT, m.file);
  const before = fs.readFileSync(file, 'utf8');
  const label = `[b${m.blocker}] ${m.name}`;

  const after = applyAnchor(before, m.from, m.to);
  if (after === null) {
    console.log(`  ANCHOR LOST  ${label}\n               ${m.file} no longer contains the text to mutate.`);
    console.log('               Fix the anchor. A mutation that cannot apply proves nothing.');
    failures++;
    continue;
  }
  if (after === before) {
    console.log(`  NO-OP        ${label}  (replacement changed nothing)`);
    failures++;
    continue;
  }

  fs.writeFileSync(file, after);
  try {
    // Bounded: a mutation can stop a promise settling, and node:test then waits
    // forever. A run that does not finish is still a caught mutation, but the
    // harness must not wait with it.
    const run = spawnSync(process.execPath, ['--test', 'test/contracts.test.mjs'],
      { cwd: ROOT, encoding: 'utf8', timeout: 120_000, killSignal: 'SIGKILL' });
    const out = `${run.stdout ?? ''}${run.stderr ?? ''}`;
    const namedFailure = out.split('\n')
      .filter((l) => l.trimStart().startsWith('✖') || l.includes('not ok'))
      .some((l) => m.expect.test(l));

    // Order matters. Some mutations fail the right test and THEN leave a timer
    // running so the process never exits; reporting only "hung" would hide that
    // it was properly caught, and a bare hang is weak evidence on its own.
    if (namedFailure) {
      console.log(`  caught       ${label}${run.signal ? '  (and then leaked a timer)' : ''}`);
    } else if (run.signal) {
      console.log(`  WEAK         ${label}\n               suite hung and was killed, but no named test failed`);
      failures++;
    } else if (run.status !== 0) {
      console.log(`  WRONG TEST   ${label}\n               the suite failed, but not on ${m.expect}`);
      failures++;
    } else {
      console.log(`  NOT CAUGHT   ${label}\n               the suite stayed GREEN with the fix reverted`);
      failures++;
    }
  } finally {
    fs.writeFileSync(file, before);
  }
}

console.log(`\n${MUTATIONS.length - failures}/${MUTATIONS.length} mutations caught\n`);
process.exit(failures === 0 ? 0 : 1);
