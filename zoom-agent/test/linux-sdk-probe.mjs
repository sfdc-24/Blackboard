// Does the Linux media path actually WORK, or does it merely resolve?
//
// The previous CI step ran `require.resolve('@zoom/rtms')` and reported the
// result. Resolution proves a directory exists on disk. It does not load the
// native binding, and it does not prove the SDK can be constructed — which is
// the whole reason the Linux host exists in this design. A review called that
// out: the Linux use boundary was unguarded.
//
// This probe crosses the boundary for real: dynamic import (the same call
// src/rtms.js makes at stream time) and then construction of the client. It
// needs NO credentials, joins nothing, and opens no socket.
//
// It SKIPS only where the SDK is not published (Windows), because absence there
// is correct and separately tested in portability.test.mjs. On linux/darwin an
// absent SDK FAILS: the package is an optionalDependency, so a broken native
// install leaves `npm ci` green, and a check that cannot go red guards nothing.

import { createRequire } from 'node:module';

const require = createRequire(import.meta.url);

// The platforms @zoom/rtms actually publishes for. Anywhere else, its absence
// is the correct and separately tested behaviour (portability.test.mjs).
const SDK_PLATFORMS = new Set(['linux', 'darwin']);

// --simulate-missing exercises the absent-SDK branch without uninstalling the
// package. The branch decides whether CI can go red, so it needs a test of its
// own; a guard whose failure path is never executed is the thing this file was
// written to stop believing in.
const simulateMissing = process.argv.includes('--simulate-missing');

function have() {
  if (simulateMissing) return false;
  try { require.resolve('@zoom/rtms'); return true; } catch { return false; }
}

if (!have()) {
  if (!SDK_PLATFORMS.has(process.platform)) {
    console.log(`SKIP @zoom/rtms is not published for ${process.platform} — absence is expected and tested elsewhere`);
    process.exit(0);
  }
  // On a platform that DOES publish it, absence is a failure and must be one.
  // @zoom/rtms is an optionalDependency, so `npm ci` exits 0 even when the
  // native install fails — this branch previously exited 0 too, which meant
  // the Linux job could be green while the deployed agent had no media
  // capability whatsoever. A green check that cannot go red is decoration.
  console.error(
    `FAIL @zoom/rtms is not installed on ${process.platform}, which is a platform it publishes for. `
    + 'It is an optionalDependency, so a failed native install does not fail npm ci — '
    + 'this check is the only thing standing between that and a silently media-less agent.',
  );
  process.exit(1);
}

const mod = await import('@zoom/rtms');
const rtms = mod?.default;

if (!rtms) {
  console.error('FAIL @zoom/rtms imported but exposes no default export');
  process.exit(1);
}
if (typeof rtms.Client !== 'function') {
  console.error(`FAIL @zoom/rtms default export has no Client constructor (got ${typeof rtms.Client})`);
  process.exit(1);
}

// Construction loads the native binding. This is the step `require.resolve`
// never reached, and the one that would break first if the prebuilt binary did
// not match the runner's Node ABI or libc.
const client = new rtms.Client();
if (!client) {
  console.error('FAIL new rtms.Client() returned nothing');
  process.exit(1);
}

console.log(`PASS dynamic import and client construction succeeded on ${process.platform} (node ${process.version})`);
process.exit(0);
