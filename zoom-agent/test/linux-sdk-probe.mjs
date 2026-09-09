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
// It is deliberately skip-not-fail on a host without the SDK, because Windows
// not having it is the correct and tested behaviour — see portability.test.mjs.

import { createRequire } from 'node:module';

const require = createRequire(import.meta.url);

function have() {
  try { require.resolve('@zoom/rtms'); return true; } catch { return false; }
}

if (!have()) {
  console.log('SKIP @zoom/rtms is not installed on this platform — that is expected off linux/darwin');
  process.exit(0);
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
