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

/** Returned instead of a pid when the lock is held by a process that has died. */
export const DEAD_HOLDER = -1;

/**
 * The pid in the lock file if it names a LIVING process; DEAD_HOLDER otherwise.
 * Only ever called when the lock already exists, so "nobody holds it" is not an
 * outcome — it is a message-quality question, not a control-flow one.
 */
export function livingHolder(lock) {
  let pid;
  try {
    pid = Number(fs.readFileSync(lock, 'utf8').trim());
  } catch {
    return DEAD_HOLDER;   // vanished or unreadable under us
  }
  if (!Number.isInteger(pid) || pid <= 0) return DEAD_HOLDER;
  try {
    process.kill(pid, 0);   // signal 0 tests existence and sends nothing
    return pid;
  } catch {
    return DEAD_HOLDER;
  }
}

/**
 * Take the lock, or report who holds it. Exactly one process can ever win.
 *
 * THREE VERSIONS OF THIS FUNCTION HAVE BEEN WRONG, AND THE THIRD IS THE ONE
 * WORTH READING, because it is why there is no automatic stale recovery here.
 *
 *   1. check-then-write. Read the lock, see nobody, write. Two harnesses
 *      starting together both passed the read before either reached the write.
 *      A guard against a race, built out of a race. (Found by review.)
 *
 *   2. `wx`, plus reclaiming a lock whose pid was dead. Atomic acquisition, and
 *      still 1-2 mutual-exclusion violations per 960 contended rounds, because
 *      reclaiming means DELETING A FILE YOU DO NOT OWN: a reclaimer that judged
 *      the lock stale, then unlinked it a moment later, destroyed a fresh lock
 *      another process had legitimately created in between. Both then held it.
 *
 *   3. `wx` plus a settle-and-read-back after reclaiming. Narrowed it. Did not
 *      close it — the victim of the stray unlink is whoever took the lock on
 *      the FREE path, and that process has nothing to confirm against.
 *
 * There is no fourth version, because the flaw is not in the bookkeeping. Plain
 * files give atomic create-if-absent and nothing else; safe reclamation needs
 * compare-and-swap, which they do not have. Every scheme that deletes another
 * process's file races something.
 *
 * SO NOTHING IS EVER DELETED HERE EXCEPT BY ITS OWN HOLDER. `wx` is the whole
 * algorithm, and the guarantee is unconditional rather than probable.
 *
 * The cost is one line of human work after a hard kill: the caller is told the
 * exact path to remove. That is the correct trade for a gate whose entire job
 * is preventing two processes from writing the same source tree — a wedged gate
 * is loud and costs a `rm`, while a raced one is silent and corrupts a commit.
 * CI never pays it at all: every job gets a fresh temp directory.
 *
 * @returns {number|null} null when acquired; the holder's pid when it is
 *                        running; DEAD_HOLDER when a crashed run left it behind
 */
export function acquireLock(lock) {
  try {
    const fd = fs.openSync(lock, 'wx');   // create-if-absent, one syscall
    try { fs.writeSync(fd, String(process.pid)); } finally { fs.closeSync(fd); }
    return null;
  } catch (err) {
    if (err?.code !== 'EEXIST') throw err;
    return livingHolder(lock);
  }
}

/** Release only our own lock. Never another holder's. */
export function releaseLock(lock) {
  try {
    if (fs.readFileSync(lock, 'utf8').trim() === String(process.pid)) fs.unlinkSync(lock);
  } catch { /* already gone */ }
}
