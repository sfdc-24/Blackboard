// Proves the agent loads and behaves on a host WITHOUT the Zoom media SDK.
//
// WHY THIS EXISTS
//   @zoom/rtms is a native module published for linux and darwin only. It used
//   to be a hard dependency imported at the top of src/rtms.js, which index.js
//   imports — so on Windows the module graph failed to resolve and `npm
//   install` refused outright with notsup. The agent could not be run, tested
//   or even imported on the laptop, which is why it sat untouched from
//   29 August while being described as "waiting on attention".
//
//   It is now an optionalDependency loaded lazily at the point a stream starts.
//   This file is the guard on that: if anyone reinstates a static import, or
//   moves the SDK back to `dependencies`, these tests fail on Windows CI.
//
// NO NETWORK, NO CREDENTIALS, ANY PLATFORM.
//   test/simulate-call.mjs is the other kind of test — it calls the real
//   reception() endpoint and needs SFDC24_EXEC. It is deliberately NOT run by
//   `npm test`, because a suite that needs a live backend and spends money is
//   not a suite CI can run on every push.

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';

const require = createRequire(import.meta.url);
const pkg = JSON.parse(readFileSync(new URL('../package.json', import.meta.url), 'utf8'));

// The agent's own config refuses to load without these. Set placeholders so the
// tests exercise module loading rather than configuration.
process.env.ZOOM_CLIENT_ID ??= 'test-client-id';
process.env.ZOOM_CLIENT_SECRET ??= 'test-client-secret';
process.env.ZOOM_WS_ENDPOINT ??= 'wss://example.invalid/ws?subscriptionId=test';

function sdkInstalled() {
  try {
    require.resolve('@zoom/rtms');
    return true;
  } catch {
    return false;
  }
}

test('the media SDK is optional, not a hard dependency', () => {
  assert.equal(
    pkg.dependencies?.['@zoom/rtms'],
    undefined,
    '@zoom/rtms must not be in dependencies — it publishes for linux/darwin only, '
    + 'so a hard dependency makes npm install fail outright on Windows',
  );
  assert.ok(
    pkg.optionalDependencies?.['@zoom/rtms'],
    '@zoom/rtms belongs in optionalDependencies so npm install succeeds everywhere',
  );
});

test('src/rtms.js has no static import of the SDK', () => {
  const src = readFileSync(new URL('../src/rtms.js', import.meta.url), 'utf8');
  assert.doesNotMatch(
    src,
    /^\s*import\s+[^;]*from\s+['"]@zoom\/rtms['"]/m,
    'a static import puts the SDK back in the module graph at load time, which is '
    + 'the exact wall that blocked this agent for ten days',
  );
  assert.match(src, /await import\(['"]@zoom\/rtms['"]\)/, 'it should be loaded lazily instead');
});

test('every module imports on this platform', async () => {
  // index.js is excluded on purpose: importing it starts the OAuth server and
  // the event socket. The modules below are the whole graph it depends on.
  for (const mod of ['config.js', 'reception.js', 'notify.js', 'board.js', 'assistant.js', 'rtms.js']) {
    const loaded = await import(`../src/${mod}`);
    assert.ok(loaded, `${mod} failed to import`);
  }
});

test('a stream join without the SDK fails with a sentence, not a stack', async (t) => {
  if (sdkInstalled()) {
    t.skip('SDK present on this platform — the absent-SDK path cannot be exercised here');
    return;
  }
  const { handleZoomEvent } = await import('../src/rtms.js');
  const errors = [];
  const real = console.error;
  console.error = (...args) => errors.push(args.join(' '));
  try {
    await handleZoomEvent({
      event: 'meeting.rtms_started',
      payload: { object: { rtms_stream_id: 's1', meeting_uuid: 'm1' } },
    });
  } finally {
    console.error = real;
  }
  assert.equal(errors.length, 1, 'exactly one explanation should be logged');
  assert.match(errors[0], /linux and darwin/, 'the message must say why, not just that it failed');
  assert.match(errors[0], /the rest of the agent still can/, 'and must not imply the whole agent is broken');
});

test('an unrelated Zoom event is ignored without touching the SDK', async () => {
  const { handleZoomEvent } = await import('../src/rtms.js');
  await assert.doesNotReject(
    () => handleZoomEvent({ event: 'meeting.participant_joined', payload: {} }),
    'non-RTMS events must not reach the loader at all',
  );
});
