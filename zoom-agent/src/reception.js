// The SFDC24 brain, reached the same way every other surface reaches it.
//
// This is deliberately NOT a direct model call. www.sfdc24.com, the WhatsApp
// gateway and /voice/ all ask the same Apps Script `reception()` function, so
// they share one persona, one set of spend caps and one quarantined log. A
// second, private path to a model would drift from all three and bypass the
// caps that keep the bill bounded.
import { config } from './config.js';

const TIMEOUT_MS = 45_000;

/**
 * Ask SFDC24 a question. Returns { ok, reply } — never throws, because a live
 * call must not be interrupted by a backend hiccup.
 *
 * @param {string} question
 * @param {string} conversationId  stable per meeting, so the backend keeps context
 */
export async function ask(question, conversationId) {
  if (!config.receptionExec) {
    return { ok: false, reply: null, error: 'SFDC24_EXEC not configured' };
  }
  const url = new URL(config.receptionExec);
  url.searchParams.set('action', 'say');
  url.searchParams.set('vid', conversationId);
  url.searchParams.set('q', question);

  const ctl = new AbortController();
  const timer = setTimeout(() => ctl.abort(), TIMEOUT_MS);
  try {
    const res = await fetch(url, { redirect: 'follow', signal: ctl.signal });
    if (!res.ok) return { ok: false, reply: null, error: `HTTP ${res.status}` };
    const body = await res.text();
    const data = parseMaybeJsonp(body);
    if (!data || !data.ok || !data.reply) {
      return { ok: false, reply: null, error: 'backend returned no reply' };
    }
    return { ok: true, reply: String(data.reply).trim() };
  } catch (err) {
    return { ok: false, reply: null, error: err.name === 'AbortError' ? 'timed out' : err.message };
  } finally {
    clearTimeout(timer);
  }
}

// The endpoint answers raw JSON when no callback is requested, but it also
// serves JSONP to the browser clients. Accept either rather than depending on
// which one today's deployment happens to return.
function parseMaybeJsonp(body) {
  const text = String(body).trim();
  try {
    return JSON.parse(text);
  } catch {
    const m = text.match(/^[A-Za-z0-9_$.]+\((.*)\);?$/s);
    if (!m) return null;
    try { return JSON.parse(m[1]); } catch { return null; }
  }
}
