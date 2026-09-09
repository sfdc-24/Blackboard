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
];

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
process.on('exit', restoreAll);
for (const sig of ['SIGINT', 'SIGTERM', 'SIGHUP', 'SIGBREAK']) {
  process.on(sig, () => { restoreAll(); process.exit(130); });
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
