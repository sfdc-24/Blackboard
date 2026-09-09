// SFDC24 Consultant backend — entry point.
//
//  ┌────────────┐  rtms_started/stopped   ┌──────────────┐   media frames   ┌───────────┐
//  │ Zoom cloud │ ──── events-ws.js ────▶ │   rtms.js    │ ───────────────▶ │ assistant │
//  └────────────┘   (event WebSocket)     │ (@zoom/rtms) │  transcript/etc  └───────────┘
//                                         └──────────────┘
//  oauth.js: one-time Allow → tokens for the event WebSocket.
import { config } from './config.js';
import { loadTokens, authorizeUrl, startOAuthServer } from './oauth.js';
import { ZoomEventSocket } from './events-ws.js';
import { handleZoomEvent } from './rtms.js';

const socket = new ZoomEventSocket(handleZoomEvent);

// A promise guard, not a boolean. The boolean version was set on entry and only
// cleared on failure, so after the first SUCCESSFUL connect it stayed true
// forever and every later call returned silently — including the one the OAuth
// callback makes after a re-authorization. Concurrent callers still share one
// attempt; sequential callers can retry.
let connecting = null;
function connectEventSocket() {
  if (connecting) return connecting;
  connecting = socket.connect().finally(() => { connecting = null; });
  return connecting;
}

/**
 * A failed connect that never produced a socket has no close handler to own its
 * retry, so this caller must — exactly as the reconnect and recycle paths
 * already do.
 *
 * The gap this closes: start with a saved token that has expired, have the
 * refresh fail transiently, and connect() rejects BEFORE `new WebSocket()`.
 * There is no close event, so nothing carries `retryScheduled`, and the catch
 * here only logged. A long-running agent then sat permanently disconnected
 * until a human noticed and restarted it — the failure being a transient one
 * that would have cleared on the next attempt.
 */
function connectWithRetry(onFail) {
  connectEventSocket().catch((err) => {
    onFail?.(err);
    if (!err.retryScheduled) socket.scheduleReconnect();
  });
}

startOAuthServer(() => {
  console.log('[oauth] authorized — connecting to Zoom event socket');
  connectWithRetry((err) => console.error('[events] connect failed:', err.message));
});

// THE EVENT PLANE'S CREDENTIAL DECIDES WHETHER STARTUP WAITS FOR A PERSON.
//
// This used to be `if (loadTokens())` unconditionally, which was right when the
// event socket rode on the user's OAuth grant and wrong the moment it stopped.
// With client_credentials there is no user token to find, so the agent printed
// "First run — authorize the app" and sat there: zero token requests, zero
// sockets, waiting forever for a personal artifact it no longer uses. The
// credential was corrected and the startup gate still keyed on the old one.
//
// Found by an independent reviewer running `node src/index.js` for real against
// localhost endpoints — no unit test here would have caught it, because the
// defect is in module top-level wiring rather than in any function.
const userTokens = loadTokens();

if (config.eventTokenGrant !== 'user') {
  console.log('[events] app credentials (client_credentials) — connecting without user authorization');
  if (!userTokens) {
    console.log(`(the OAuth flow is still available at http://localhost:${config.port}/ but the event plane does not need it)`);
  }
  connectWithRetry((err) => console.error('[events] connect failed:', err.message));
} else if (userTokens) {
  console.log('[oauth] found saved tokens (.tokens.json)');
  connectWithRetry((err) => {
    console.error('[events] connect failed:', err.message);
    console.log(`Re-authorize if needed: ${authorizeUrl()}`);
  });
} else {
  console.log('\n── First run ─────────────────────────────────────────────');
  console.log('Authorize the app with your Zoom account (one-time):');
  console.log(`\n  ${authorizeUrl()}\n`);
  console.log(`(or open http://localhost:${config.port}/ and click the link)`);
  console.log('──────────────────────────────────────────────────────────\n');
}

process.on('SIGINT', () => {
  console.log('\nShutting down…');
  socket.close();
  process.exit(0);
});
