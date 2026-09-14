// OAuth for a USER-MANAGED General App.
// One-time: user clicks Allow → Zoom redirects here with ?code= → we exchange it
// for access+refresh tokens. After that, refresh tokens keep us authorized forever.
//
// FOUR THINGS THIS FILE IS DELIBERATE ABOUT, all of them findings from review:
//
//   1. IT LISTENS ON LOOPBACK ONLY. Express's default listen binds every
//      interface; `server.address()` returned `{address:'::'}` on the reviewed
//      head. On a laptop on hotel wifi, or on the VM, that published the
//      authorization callback to the network. Zoom's own loopback guidance is
//      to bind a loopback literal, and that is now the default and the only
//      value that does not require the operator to say so out loud.
//
//   2. IT VALIDATES A ONE-TIME `state` NONCE. Without one the callback accepted
//      ANY code from anyone who could reach the port — there was nothing tying
//      the response to a request this process actually made.
//
//   3. THE CALLBACK CLOSES AFTER IT SUCCEEDS. An authorization endpoint that
//      stays open for the life of a long-running agent is an endpoint with no
//      reason to still exist.
//
//   4. TOKENS ARE WRITTEN 0600 AND REPLACED ATOMICALLY. A refresh token is a
//      permanent credential. A half-written file after a crash is an agent that
//      cannot authorize and cannot say why.
//
// There is no Express here on purpose. Two routes did not justify a dependency
// tree that carried two moderate `qs` advisories into a process that holds a
// long-lived Zoom refresh token.
import fs from 'node:fs';
import path from 'node:path';
import http from 'node:http';
import crypto from 'node:crypto';
import { config } from './config.js';

let tokens = null; // { access_token, refresh_token, expires_at }

// Pending authorization nonces: state -> issued-at. Single use, short lived.
const pendingStates = new Map();
const STATE_TTL_MS = 10 * 60_000;

export function loadTokens() {
  try {
    tokens = JSON.parse(fs.readFileSync(config.tokenFile, 'utf8'));
  } catch {
    tokens = null;
  }
  return tokens;
}

function saveTokens(next) {
  tokens = next;
  const target = path.resolve(config.tokenFile);
  const tmp = `${target}.${process.pid}.tmp`;
  // 0600 at creation, not chmod-after-write: between the two there is a window
  // in which a refresh token is world-readable.
  fs.writeFileSync(tmp, JSON.stringify(next, null, 2), { mode: 0o600 });
  try {
    fs.chmodSync(tmp, 0o600); // no-op on POSIX; makes the intent explicit
  } catch { /* Windows has no POSIX mode; the rename below still applies */ }
  fs.renameSync(tmp, target); // atomic within a filesystem
}

function freshState() {
  const now = Date.now();
  for (const [value, issued] of pendingStates) {
    if (now - issued > STATE_TTL_MS) pendingStates.delete(value);
  }
  const state = crypto.randomBytes(24).toString('base64url');
  pendingStates.set(state, now);
  return state;
}

/** True exactly once per issued nonce. */
function consumeState(state) {
  const value = String(state ?? '');
  const issued = pendingStates.get(value);
  if (issued === undefined) return false;
  pendingStates.delete(value);
  return Date.now() - issued <= STATE_TTL_MS;
}

export function authorizeUrl() {
  const url = new URL('https://zoom.us/oauth/authorize');
  url.searchParams.set('response_type', 'code');
  url.searchParams.set('client_id', config.clientId);
  url.searchParams.set('redirect_uri', config.redirectUri);
  url.searchParams.set('state', freshState());
  return url.toString();
}

/**
 * Every call to Zoom's token endpoint, bounded.
 *
 * THE HOLE THIS CLOSES IS IN A TIMER THAT ALREADY EXISTS.
 *   connect() awaits getAccessToken() BEFORE it constructs the WebSocket, and
 *   the connect bound is armed on that socket. So a token endpoint that accepts
 *   the request and then stalls blocks connect() in a window nothing is
 *   watching: no socket, therefore no close event, therefore no rejection and
 *   no scheduled retry. The agent sits disconnected until whatever the fetch
 *   implementation's own default happens to be — which for undici is minutes,
 *   and is not a promise anyone here made.
 *
 *   Two rounds of this PR went into making the connect timer cover TCP and the
 *   upgrade as well as the ack, on exactly this reasoning. The await that runs
 *   before all of it was still unguarded: the bound was widened up to the edge
 *   of the call that can hang first.
 *
 * Worst case is now tokenTimeoutMs + connectTimeoutMs, both finite. A bound you
 * can add up is the whole point.
 */
async function tokenRequest(params) {
  const basic = Buffer.from(`${config.clientId}:${config.clientSecret}`).toString('base64');
  let res;
  try {
    res = await fetch(config.tokenUrl, {
      method: 'POST',
      headers: {
        Authorization: `Basic ${basic}`,
        'Content-Type': 'application/x-www-form-urlencoded',
      },
      body: new URLSearchParams(params),
      signal: AbortSignal.timeout(config.tokenTimeoutMs),
    });
  } catch (err) {
    // Say which bound was hit. A caller that cannot tell a stall from a refusal
    // cannot decide whether retrying is sensible.
    if (err?.name === 'TimeoutError' || err?.name === 'AbortError') {
      throw new Error(
        `Zoom token request did not answer within ${config.tokenTimeoutMs}ms — `
        + 'aborted so the connect path can retry rather than hang',
      );
    }
    throw err;
  }
  if (!res.ok) {
    throw new Error(`Zoom token request failed (${res.status}): ${await res.text()}`);
  }
  const data = await res.json();
  saveTokens({
    access_token: data.access_token,
    refresh_token: data.refresh_token,
    expires_at: Date.now() + data.expires_in * 1000,
  });
  return tokens;
}

/**
 * An APP-level access token for the event plane, cached in memory.
 *
 * WHY THE EVENT SOCKET MUST NOT USE THE USER'S TOKEN
 *   Everything above this line is the user-consent flow: authorization_code,
 *   then refresh_token, tied to one person's grant and to a refresh token on
 *   disk. That is right for acting on a user's behalf and wrong for the event
 *   subscription, which is the APPLICATION asking Zoom to tell it when meetings
 *   start. Binding it to a person means the whole event plane dies when that
 *   person revokes consent, changes password, or simply lets the refresh token
 *   lapse — a background service that stops on an interactive lifecycle it has
 *   no reason to share.
 *
 *   Zoom's WebSocket guide additionally specifies `client_credentials` for this
 *   endpoint for both General and Server-to-Server OAuth apps, per the
 *   independent review of 2026-09-09 (developers.zoom.us/docs/api/websockets/).
 *   THAT DOCUMENT COULD NOT BE READ FROM THE CONTAINER THIS WAS WRITTEN IN —
 *   egress to developers.zoom.us is blocked — so the contract is taken from the
 *   review, not verified here. The design argument above stands on its own and
 *   does not depend on it.
 *
 *   `ZOOM_EVENT_TOKEN_GRANT=user` restores the previous behaviour if the
 *   contract turns out to be otherwise, so this is reversible without a deploy.
 */
let appToken = null;   // { access_token, expires_at }

export async function getAppAccessToken({ minTtlMs = 5 * 60_000 } = {}) {
  if (appToken && appToken.expires_at - Date.now() > minTtlMs) return appToken.access_token;

  const basic = Buffer.from(`${config.clientId}:${config.clientSecret}`).toString('base64');
  let res;
  try {
    res = await fetch(config.tokenUrl, {
      method: 'POST',
      headers: {
        Authorization: `Basic ${basic}`,
        'Content-Type': 'application/x-www-form-urlencoded',
      },
      body: new URLSearchParams({ grant_type: 'client_credentials' }),
      signal: AbortSignal.timeout(config.tokenTimeoutMs),
    });
  } catch (err) {
    appToken = null;
    if (err?.name === 'TimeoutError' || err?.name === 'AbortError') {
      throw new Error(
        `Zoom app-token request did not answer within ${config.tokenTimeoutMs}ms — `
        + 'aborted so the connect path can retry rather than hang',
      );
    }
    throw err;
  }
  if (!res.ok) {
    appToken = null;
    throw new Error(`Zoom app-token request failed (${res.status}): ${await res.text()}`);
  }
  const data = await res.json();
  if (!data?.access_token) {
    appToken = null;
    throw new Error('Zoom app-token response carried no access_token');
  }
  appToken = {
    access_token: data.access_token,
    expires_at: Date.now() + (Number(data.expires_in) || 3600) * 1000,
  };
  return appToken.access_token;
}

/** Test seam: forget the cached app token. */
export function __resetAppToken() { appToken = null; }

/**
 * The token the EVENT plane should use. One place decides, so the socket does
 * not have to know which flow it is on.
 */
export async function getEventToken(opts) {
  return config.eventTokenGrant === 'user'
    ? getAccessToken(opts)
    : getAppAccessToken(opts);
}

export async function exchangeCode(code) {
  return tokenRequest({
    grant_type: 'authorization_code',
    code,
    redirect_uri: config.redirectUri,
  });
}

async function refreshTokens() {
  if (!tokens?.refresh_token) {
    throw new Error(`No refresh token — authorize first: ${authorizeUrl()}`);
  }
  return tokenRequest({
    grant_type: 'refresh_token',
    refresh_token: tokens.refresh_token,
  });
}

/**
 * Returns a valid access token.
 *
 * `minTtlMs` is what the caller needs the token to remain valid FOR, not a
 * refresh threshold. The event socket recycles at 50 minutes and asked for a
 * "fresh" token while ten minutes remained; this function refreshed only inside
 * five, so the recycle reconnected with the same token it already had and the
 * hour-long expiry was never actually avoided. Callers now state their horizon.
 */
export async function getAccessToken({ minTtlMs = 5 * 60_000 } = {}) {
  if (!tokens) loadTokens();
  if (!tokens) return null;
  if (Date.now() >= tokens.expires_at - minTtlMs) {
    await refreshTokens();
  }
  return tokens.access_token;
}

/**
 * Loopback HTTP server for the redirect URL configured in the Marketplace
 * console. Returns the node:http server so callers can close it.
 */
export function startOAuthServer(onAuthorized) {
  let acceptingCallbacks = true;

  const server = http.createServer((req, res) => {
    const url = new URL(req.url, `http://${config.oauthHost}:${config.port}`);
    const send = (status, body) => {
      res.writeHead(status, { 'Content-Type': 'text/html; charset=utf-8' });
      res.end(body);
    };

    if (req.method !== 'GET') return send(405, 'Method not allowed');

    if (url.pathname === '/') {
      return send(200,
        '<h3>SFDC24 Consultant backend</h3>' +
        (tokens
          ? '<p>Authorized ✔ — listening for RTMS events.</p>'
          : `<p><a href="${authorizeUrl()}">Authorize with Zoom</a> to get started.</p>`));
    }

    if (url.pathname === '/oauth/callback') {
      if (!acceptingCallbacks) {
        // Already authorized. Nothing legitimate arrives here afterwards.
        return send(410, 'Authorization already completed — this endpoint is closed.');
      }
      const code = url.searchParams.get('code');
      const error = url.searchParams.get('error');
      const state = url.searchParams.get('state');

      if (error || !code) {
        // NOTHING FROM THE QUERY STRING IS REFLECTED HERE. `send` writes
        // text/html, so echoing `error` put attacker-controlled markup into a
        // page served from this loopback origin — reachable by anything that
        // can make the operator's browser open a URL, and reached BEFORE the
        // state nonce is checked, so the CSRF guard offered no protection at
        // all. Script running on this origin can request `/`, read a freshly
        // minted authorize URL and its state, and bind the agent to an
        // attacker's Zoom authorization. The token file this process writes is
        // the whole prize.
        //
        // Escaping would work. A fixed string cannot be got wrong later, and
        // the operator loses nothing: the real value is logged here, where it
        // is useful and inert.
        console.error(`[oauth] callback rejected — error=${JSON.stringify(error)} code=${code ? 'present' : 'missing'}`);
        return send(400, 'Authorization failed. Check the backend logs for the reason.');
      }
      // Before the exchange, never after: an unbound code must not be spent.
      if (!consumeState(state)) {
        console.error('[oauth] rejected a callback with an unknown or reused state nonce');
        return send(400, 'Authorization failed: state did not match a pending request.');
      }

      exchangeCode(String(code)).then(() => {
        acceptingCallbacks = false;
        pendingStates.clear();
        send(200, '<h3>SFDC24 Consultant authorized ✔</h3><p>You can close this tab — the backend is connecting to Zoom now.</p>');
        onAuthorized?.();
      }).catch((err) => {
        console.error('[oauth] token exchange failed:', err.message);
        send(500, 'Token exchange failed — check the backend logs.');
      });
      return undefined;
    }

    return send(404, 'Not found');
  });

  // Loopback literal by default. Overriding it is possible but must be typed
  // out by an operator who means it.
  server.listen(config.port, config.oauthHost, () => {
    const addr = server.address();
    console.log(`[oauth] listening on http://${config.oauthHost}:${addr.port} (loopback only)`);
  });
  return server;
}
