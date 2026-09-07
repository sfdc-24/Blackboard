/**
 * SFDC24 - Sign in with Google, for visitors
 * claude-code-cli, 2026-09-04.
 *
 * WHY THE REDIRECT FLOW AND NOT THE "Sign in with Google" BUTTON
 *   Google Identity Services requires the page's origin to be pre-registered as
 *   an Authorized JavaScript Origin. This page renders inside Apps Script's own
 *   sandbox iframe, on a googleusercontent.com subdomain that VARIES per
 *   session, itself nested inside a Google Sites iframe. There is no fixed
 *   origin to register, and One Tap is blocked in cross-origin frames anyway.
 *   The redirect flow needs only a fixed redirect URI, which /exec gives us.
 *
 * WHY SESSIONS ARE STATELESS
 *   An Apps Script web app cannot set cookies, and it serves page HTML from a
 *   googleusercontent.com subdomain that is NOT stable, so localStorage there
 *   can be unreadable on a later visit. So the session IS a signed token:
 *   payload + HMAC-SHA256 over a server-only secret, carried in the page URL
 *   and re-verified server-side on each render. Nothing to store, nothing to
 *   expire out of a table, and a tampered token fails the MAC.
 *
 * THE RULE THAT MATTERS MOST
 *   IDENTITY IS NOT AUTHORITY. Knowing a visitor is alice@acme.com does not let
 *   alice instruct the fleet. Signed-in visitor text is still quarantined with
 *   instruction_authority=NONE exactly like anonymous text (see PublicInbox.gs
 *   and L-81's neighbours). The only thing sign-in changes is that we know who
 *   said it. Governor authority remains whoami_().isGovernor and nothing here
 *   touches it.
 */

// The EXACT string registered as an Authorized redirect URI in Cloud Console
// (project sfdc24, client "SFDC24 Website Sign-In"). Deliberately hardcoded
// rather than derived: ScriptApp.getService().getUrl() returns the
// /a/macros/sfdc24.com/... Workspace form for signed-in domain users, which is
// NOT what is registered, and the mismatch fails with redirect_uri_mismatch.
var AUTH_REDIRECT_URI =
  'https://script.google.com/macros/s/AKfycbx0D-5DAnMqOm9YbN3iKDwuiBApEi_xex60f6pwdvObEyQBF5jcOK715pl1mN-Nzn6gng/exec';

var AUTH_CLIENT_ID_KEY     = 'GOOGLE_CLIENT_ID';
var AUTH_CLIENT_SECRET_KEY = 'GOOGLE_CLIENT_SECRET';
var AUTH_SIGNING_KEY       = 'AUTH_SIGNING_SECRET';
var AUTH_SESSION_DAYS      = 14;
var AUTH_STATE_TTL_SECS    = 600;

function authConfigured_() {
  var p = PropertiesService.getScriptProperties();
  return !!(p.getProperty(AUTH_CLIENT_ID_KEY) && p.getProperty(AUTH_CLIENT_SECRET_KEY));
}

/** Server-only signing key for session tokens. Minted once, never leaves here. */
function authSigningKey_() {
  var p = PropertiesService.getScriptProperties();
  var k = p.getProperty(AUTH_SIGNING_KEY);
  if (!k) {
    k = Utilities.getUuid() + Utilities.getUuid();
    p.setProperty(AUTH_SIGNING_KEY, k);
  }
  return k;
}

function b64urlEncode_(str) {
  return Utilities.base64EncodeWebSafe(str, Utilities.Charset.UTF_8).replace(/=+$/, '');
}

function b64urlDecode_(str) {
  var s = String(str);
  while (s.length % 4 !== 0) s += '=';
  return Utilities.newBlob(Utilities.base64DecodeWebSafe(s)).getDataAsString();
}

function hmac_(msg) {
  var raw = Utilities.computeHmacSha256Signature(msg, authSigningKey_());
  return Utilities.base64EncodeWebSafe(raw).replace(/=+$/, '');
}

/** Constant-time-ish comparison. Avoids leaking position of first difference. */
function safeEqual_(a, b) {
  a = String(a); b = String(b);
  if (a.length !== b.length) return false;
  var diff = 0;
  for (var i = 0; i < a.length; i++) diff |= (a.charCodeAt(i) ^ b.charCodeAt(i));
  return diff === 0;
}

function mintSession_(claims) {
  var payload = {
    email: claims.email,
    name:  claims.name || '',
    sub:   claims.sub,
    exp:   Date.now() + AUTH_SESSION_DAYS * 86400000
  };
  var body = b64urlEncode_(JSON.stringify(payload));
  return body + '.' + hmac_(body);
}

/** Returns the session claims, or null. Never throws. */
function readSession_(token) {
  try {
    if (!token) return null;
    var parts = String(token).split('.');
    if (parts.length !== 2) return null;
    if (!safeEqual_(hmac_(parts[0]), parts[1])) return null;
    var claims = JSON.parse(b64urlDecode_(parts[0]));
    if (!claims || !claims.email) return null;
    if (!claims.exp || Date.now() > claims.exp) return null;
    return claims;
  } catch (e) { return null; }
}

/** Read the payload of a JWT. The token came from Google's token endpoint over
 *  TLS, so the transport authenticates it; the claim checks below are what stop
 *  a token minted for a DIFFERENT app being replayed at us. */
function jwtClaims_(idToken) {
  var parts = String(idToken || '').split('.');
  if (parts.length !== 3) return null;
  try { return JSON.parse(b64urlDecode_(parts[1])); } catch (e) { return null; }
}

// Where a completed sign-in may send the visitor. A CLOSED LIST, looked up by
// key, never a URL taken from the request -- otherwise ?back= would be an open
// redirect that hands a live session token to whoever crafted the link.
var AUTH_RETURNS = {
  voice: 'https://www.sfdc24.com/voice/'
};

/** Build the Google consent URL and remember a one-time state nonce. */
function authStartUrl_(back) {
  var clientId = PropertiesService.getScriptProperties().getProperty(AUTH_CLIENT_ID_KEY);
  var state = Utilities.getUuid();
  // The nonce doubles as the carrier for where to land afterwards. Google
  // returns state to us untouched, and it is single use, so nothing about the
  // destination is attacker-supplied by the time we read it back.
  var val = AUTH_RETURNS.hasOwnProperty(String(back)) ? String(back) : '1';
  CacheService.getScriptCache().put('authstate_' + state, val, AUTH_STATE_TTL_SECS);

  return 'https://accounts.google.com/o/oauth2/v2/auth'
    + '?client_id=' + encodeURIComponent(clientId)
    + '&redirect_uri=' + encodeURIComponent(AUTH_REDIRECT_URI)
    + '&response_type=code'
    + '&scope=' + encodeURIComponent('openid email profile')
    + '&state=' + encodeURIComponent(state)
    + '&prompt=select_account';
}

/**
 * Handle Google's redirect back. Returns { ok, token, email, name } or
 * { ok:false, error }. Called only from doGet.
 */
function authCompleteCallback_(e) {
  var p = (e && e.parameter) || {};
  if (p.error) return { ok: false, error: 'Google returned: ' + p.error };
  if (!p.code || !p.state) return { ok: false, error: 'missing code or state' };

  // CSRF: the state must be one WE issued, and it is single use.
  var cache = CacheService.getScriptCache();
  var key = 'authstate_' + p.state;
  var back = cache.get(key);
  if (!back) return { ok: false, error: 'state not recognised or expired - start again' };
  cache.remove(key);

  var props = PropertiesService.getScriptProperties();
  var res;
  try {
    res = UrlFetchApp.fetch('https://oauth2.googleapis.com/token', {
      method: 'post',
      muteHttpExceptions: true,
      payload: {
        code: p.code,
        client_id: props.getProperty(AUTH_CLIENT_ID_KEY),
        client_secret: props.getProperty(AUTH_CLIENT_SECRET_KEY),
        redirect_uri: AUTH_REDIRECT_URI,
        grant_type: 'authorization_code'
      }
    });
  } catch (err) {
    return { ok: false, error: 'token exchange failed: ' + err };
  }

  if (res.getResponseCode() !== 200) {
    // Deliberately does not echo the body: it can contain request details.
    return { ok: false, error: 'token endpoint returned HTTP ' + res.getResponseCode() };
  }

  var body;
  try { body = JSON.parse(res.getContentText()); } catch (err) { return { ok: false, error: 'bad token response' }; }

  var claims = jwtClaims_(body.id_token);
  if (!claims) return { ok: false, error: 'no id_token' };

  // The checks that actually matter.
  var wantAud = props.getProperty(AUTH_CLIENT_ID_KEY);
  if (claims.aud !== wantAud) return { ok: false, error: 'token audience mismatch' };
  if (claims.iss !== 'accounts.google.com' && claims.iss !== 'https://accounts.google.com') return { ok: false, error: 'unexpected issuer' };
  if (!claims.exp || (claims.exp * 1000) < Date.now()) return { ok: false, error: 'token expired' };
  if (claims.email_verified !== true) return { ok: false, error: 'email not verified by Google' };
  if (!claims.email) return { ok: false, error: 'no email in token' };

  return {
    ok: true,
    token: mintSession_(claims),
    email: claims.email,
    name: claims.name || '',
    back: back
  };
}

/**
 * Who is this visitor? Exposed to the page via google.script.run.
 *
 * SAFE TO EXPOSE: it takes a token the caller already holds and tells them what
 * it says. It grants nothing, reads nothing else, and an invalid or absent
 * token simply returns signedIn:false. It deliberately does NOT report Governor
 * status - that is whoami_()'s job and is decided by the Google session, never
 * by a token a caller supplies.
 */
function whoAmI(token) {
  var s = readSession_(token);
  return s
    ? { signedIn: true, email: s.email, name: s.name, expires: s.exp }
    : { signedIn: false, configured: authConfigured_() };
}

/** The URL a page should send the top window to in order to sign in. */
function signInUrl(back) {
  if (!authConfigured_()) return { ok: false, error: 'sign-in is not configured' };
  return { ok: true, url: authStartUrl_(back) };
}

/**
 * Small full-page responses for the sign-in dance.
 *
 * These always drive window.top, not window.location: the reception is embedded
 * in a Google Sites iframe, and sending only the iframe to Google's consent
 * screen would either be refused by frame-ancestors or trap the user in a
 * frame they cannot see.
 */
function authShell_(title, bodyHtml) {
  var css = 'body{margin:0;background:#0B0D10;color:#E6EDF3;'
          + 'font:14px/1.6 ui-monospace,Menlo,Consolas,monospace;'
          + 'display:flex;align-items:center;justify-content:center;height:100vh}'
          + '.card{max-width:520px;padding:24px}'
          + 'a{color:#00FFA6}'
          + 'h1{font-size:15px;margin:0 0 10px;color:#00FFA6}'
          + 'p{color:#8B949E;margin:0 0 8px}';
  return HtmlService.createHtmlOutput(
      '<style>' + css + '</style><div class="card">' + bodyHtml + '</div>')
    .setTitle(title)
    .addMetaTag('viewport', 'width=device-width,initial-scale=1')
    .setXFrameOptionsMode(HtmlService.XFrameOptionsMode.ALLOWALL);
}

function authRedirectPage_(url) {
  var safe = String(url).replace(/"/g, '&quot;');
  return authShell_('Signing in',
    '<h1>redirecting to Google</h1>'
    + '<p>If nothing happens, <a id="go" href="' + safe + '" target="_top">continue here</a>.</p>'
    + '<script>window.top.location.href = document.getElementById("go").href;</script>');
}

/**
 * Hand the freshly minted session back to the browser.
 *
 * The token travels in the URL rather than localStorage on purpose: Apps Script
 * serves page HTML from a googleusercontent.com subdomain that is not stable,
 * so anything stashed in localStorage may be unreadable on the next visit. The
 * server renders the signed-in state from this parameter instead, which needs
 * no client storage at all.
 */
function authStorePage_(out) {
  // Off-site destinations take the token in the FRAGMENT, not the query. A
  // fragment is never sent to the destination's server and never reaches its
  // logs or a Referer header; the voice page reads it, stores it, and strips it
  // from the address bar immediately.
  var away = AUTH_RETURNS[String(out.back)];
  var dest = away
    ? (away + '#s=' + encodeURIComponent(out.token))
    : (AUTH_REDIRECT_URI + '?view=home&s=' + encodeURIComponent(out.token));
  var safe = dest.replace(/"/g, '&quot;');
  var who = String(out.email).replace(/[<>&]/g, '');
  return authShell_('Signed in',
    '<h1>signed in as ' + who + '</h1>'
    + '<p>Taking you back to the conversation.</p>'
    + '<p><a id="go" href="' + safe + '" target="_top">continue</a></p>'
    + '<script>window.top.location.href = document.getElementById("go").href;</script>');
}

/** If this request is part of the sign-in dance, return its page. Else null. */
function authHandleGet_(e) {
  var p = (e && e.parameter) || {};

  if (p.auth === 'start') {
    var r = signInUrl(p.back);
    if (!r.ok) {
      return authShell_('Sign-in unavailable',
        '<h1>sign-in is not configured yet</h1><p>' + r.error + '</p>');
    }
    return authRedirectPage_(r.url);
  }

  if (p.code || p.error) {
    var out = authCompleteCallback_(e);
    if (!out.ok) {
      return authShell_('Sign-in did not complete',
        '<h1>sign-in did not complete</h1><p>' + String(out.error).replace(/[<>&]/g, '') + '</p>'
        + '<p><a href="' + AUTH_REDIRECT_URI + '?view=home" target="_top">back to the site</a></p>');
    }
    return authStorePage_(out);
  }

  return null;
}
