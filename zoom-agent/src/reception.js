// The SFDC24 brain, reached the same way every other surface reaches it.
//
// This is deliberately NOT a direct model call. www.sfdc24.com, the WhatsApp
// gateway and /voice/ all ask the same Apps Script `reception()` function, so
// they share one persona, one set of spend caps and one quarantined log. A
// second, private path to a model would drift from all three and bypass the
// caps that keep the bill bounded.
//
// TWO CONTRACT FACTS THIS FILE IS BUILT AROUND, both read from the canonical
// source rather than assumed:
//
//   1. `q` IS TRUNCATED AT 1,000 CHARACTERS (Code.gs:34,196). Sending more is
//      not an error — it is a silent cut, and the model answers confidently
//      from the fragment. So an over-budget query is refused HERE, loudly,
//      instead of being quietly halved on the wire.
//
//   2. IDENTITY IS SERVER-MINTED, NOT CALLER-CHOSEN. `conversationIdentity_`
//      (Auth.gs:190) ignores any caller id and returns its own `ct` on every
//      branch (Code.gs:216). A client that invents a `vid` and throws away `ct`
//      gets a brand-new anonymous conversation on every single request: no
//      continuity, and no per-conversation budget accounting. Production v33
//      still tolerates `vid`, which is why the earlier live test appeared to
//      work — appeared being the operative word.
import { config } from './config.js';
import { MAX_QUERY_CHARS } from './prompt.js';

const TIMEOUT_MS = 45_000;

/**
 * A per-meeting conversation handle.
 *
 * `ct` starts empty: the server mints one on the first call and we keep it for
 * the rest of the meeting. `legacyVid` is the bounded fallback for the
 * deployment still running the pre-`ct` contract — it is sent ONLY until a real
 * `ct` has been seen, and never again afterwards.
 */
export function newConversation() {
  return {
    ct: '',
    legacyVid: 'z' + Date.now().toString(36) + Math.random().toString(36).slice(2, 8),
    usedLegacy: false,
  };
}

/**
 * Ask SFDC24 a question. Returns { ok, reply } — never throws, because a live
 * call must not be interrupted by a backend hiccup.
 *
 * @param {string} question   must already fit MAX_QUERY_CHARS; see prompt.js
 * @param {object} conv       from newConversation(); mutated to carry `ct`
 */
export async function ask(question, conv) {
  if (!config.receptionExec) {
    return { ok: false, reply: null, error: 'SFDC24_EXEC not configured' };
  }
  // Refuse rather than let the server cut it. A caller that hits this has a
  // bounding bug, and a truncated answer would hide it indefinitely.
  if (question.length > MAX_QUERY_CHARS) {
    return {
      ok: false,
      reply: null,
      error: `query is ${question.length} chars; the backend truncates at ${MAX_QUERY_CHARS} `
        + '(Code.gs CHAT_MAX_INPUT) — bound it with prompt.js before sending',
    };
  }

  const url = new URL(config.receptionExec);
  url.searchParams.set('action', 'say');
  url.searchParams.set('q', question);
  if (conv?.ct) {
    url.searchParams.set('ct', conv.ct);
  } else if (conv && !conv.usedLegacy) {
    // First request of the meeting, or a backend that never returned a ct.
    // Bounded: the moment a real ct arrives this branch is never taken again.
    url.searchParams.set('vid', conv.legacyVid);
  }

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
    // Keep the signed token. This is the whole of conversational continuity and
    // of per-conversation budget accounting; discarding it was blocker 2.
    if (conv && data.ct) {
      conv.ct = String(data.ct);
      conv.usedLegacy = true;
    }
    return { ok: true, reply: String(data.reply).trim(), ct: data.ct ?? null };
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
