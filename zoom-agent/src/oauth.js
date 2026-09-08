// OAuth for a USER-MANAGED General App.
// One-time: user clicks Allow → Zoom redirects here with ?code= → we exchange it
// for access+refresh tokens. After that, refresh tokens keep us authorized forever.
import fs from 'node:fs';
import express from 'express';
import { config } from './config.js';

let tokens = null; // { access_token, refresh_token, expires_at }

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
  fs.writeFileSync(config.tokenFile, JSON.stringify(next, null, 2));
}

export function authorizeUrl() {
  const url = new URL('https://zoom.us/oauth/authorize');
  url.searchParams.set('response_type', 'code');
  url.searchParams.set('client_id', config.clientId);
  url.searchParams.set('redirect_uri', config.redirectUri);
  return url.toString();
}

async function tokenRequest(params) {
  const basic = Buffer.from(`${config.clientId}:${config.clientSecret}`).toString('base64');
  const res = await fetch('https://zoom.us/oauth/token', {
    method: 'POST',
    headers: {
      Authorization: `Basic ${basic}`,
      'Content-Type': 'application/x-www-form-urlencoded',
    },
    body: new URLSearchParams(params),
  });
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

/** Returns a valid access token, refreshing if it expires within 5 minutes. */
export async function getAccessToken() {
  if (!tokens) loadTokens();
  if (!tokens) return null;
  if (Date.now() >= tokens.expires_at - 5 * 60 * 1000) {
    await refreshTokens();
  }
  return tokens.access_token;
}

/** Express server for the redirect URL configured in the Marketplace console. */
export function startOAuthServer(onAuthorized) {
  const app = express();

  app.get('/', (_req, res) => {
    res.send(
      `<h3>SFDC24 Consultant backend</h3>` +
        (tokens
          ? `<p>Authorized ✔ — listening for RTMS events.</p>`
          : `<p><a href="${authorizeUrl()}">Authorize with Zoom</a> to get started.</p>`),
    );
  });

  app.get('/oauth/callback', async (req, res) => {
    const { code, error } = req.query;
    if (error || !code) {
      return res.status(400).send(`Authorization failed: ${error ?? 'missing code'}`);
    }
    try {
      await exchangeCode(String(code));
      res.send('<h3>SFDC24 Consultant authorized ✔</h3><p>You can close this tab — the backend is connecting to Zoom now.</p>');
      onAuthorized?.();
    } catch (err) {
      console.error('[oauth] token exchange failed:', err.message);
      res.status(500).send('Token exchange failed — check the backend logs.');
    }
  });

  return app.listen(config.port, () => {
    console.log(`[oauth] listening on http://localhost:${config.port}`);
  });
}
