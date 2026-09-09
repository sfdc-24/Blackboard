// A single-holder lock over a working tree, in its own module SO IT CAN BE
// TESTED.
//
// It lived inside mutate.mjs, which runs a six-minute suite on import — so
// there was no way to exercise it except by reasoning about it, and reasoning
// about it is exactly what produced the check-then-write race a reviewer then
// found. A lock nothing can call is a lock nothing can test.
import fs from 'node:fs';
import os from 'node:os';
import crypto from 'node:crypto';
import path from 'node:path';

/** One lock per checkout, outside the repo so it is never committed. */
export function lockPathFor(root) {
  return path.join(
    os.tmpdir(),
    `zoom-agent-mutate-${crypto.createHash('sha256').update(root).digest('hex').slice(0, 12)}.lock`,
  );
}

/** The pid in the lock file if it names a LIVING process, else null. */
export function livingHolder(lock) {
  try {
    const pid = Number(fs.readFileSync(lock, 'utf8').trim());
    if (!Number.isInteger(pid) || pid <= 0) return null;   // garbage: treat as stale
    process.kill(pid, 0);   // signal 0 tests for existence and sends nothing
    return pid;
  } catch {
    // ENOENT: no lock at all. ESRCH: the holder died without cleaning up.
    return null;
  }
}

/**
 * Take the lock, or report who holds it.
 *
 * ACQUISITION IS ATOMIC, AND THE FIRST VERSION OF IT WAS NOT.
 *   It read the lock, decided nobody held it, and then wrote — and two
 *   harnesses starting together both passed the read before either reached the
 *   write. Both then "held" it and both mutated the tree, which is the exact
 *   corruption the lock exists to prevent, one layer down. A guard against a
 *   race, built out of a race.
 *
 *   `wx` is open-if-absent-else-fail in a single syscall. Exactly one process
 *   can win it, and that is not a claim about timing.
 *
 * STALE RECOVERY IS BEST-EFFORT, AND SAYING SO IS THE POINT.
 *   A crashed run leaves a file naming a dead pid, and something must clear it
 *   or the gate wedges forever. That is unavoidably read-then-unlink. It is
 *   narrowed by requiring the holder to still be dead immediately before the
 *   unlink, and any process that loses the following `wx` race is told the
 *   winner's pid and stands down. The residual window needs two harnesses
 *   recovering the same crashed lock in the same instant — reachable only
 *   after a crash, where the original was reachable every single time.
 *
 * @returns {number|null} null when acquired; otherwise the pid holding it
 *                        (-1 when held by a process whose pid we could not read)
 */
export function acquireLock(lock) {
  for (let attempt = 0; attempt < 2; attempt += 1) {
    try {
      const fd = fs.openSync(lock, 'wx');   // create-or-fail, one syscall
      try { fs.writeSync(fd, String(process.pid)); } finally { fs.closeSync(fd); }
      return null;
    } catch (err) {
      if (err?.code !== 'EEXIST') throw err;
      const holder = livingHolder(lock);
      if (holder !== null) return holder;   // genuinely held: do not touch it
      if (attempt === 0) {
        // Stale. Clear it only while it is still dead, so a lock created
        // between our read and this unlink is not destroyed.
        try {
          if (livingHolder(lock) === null) fs.unlinkSync(lock);
        } catch { /* someone else cleared it first — fine, retry */ }
      }
    }
  }
  return livingHolder(lock) ?? -1;   // lost the retry
}

/** Release only our own lock. Never another holder's. */
export function releaseLock(lock) {
  try {
    if (fs.readFileSync(lock, 'utf8').trim() === String(process.pid)) fs.unlinkSync(lock);
  } catch { /* already gone */ }
}
