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

startOAuthServer(() => {
  console.log('[oauth] authorized — connecting to Zoom event socket');
  connectEventSocket().catch((err) => console.error('[events] connect failed:', err.message));
});

if (loadTokens()) {
  console.log('[oauth] found saved tokens (.tokens.json)');
  connectEventSocket().catch((err) => {
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
