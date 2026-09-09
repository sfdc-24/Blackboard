// Delivery to the consultant's phone — or rather, HANDOFF to WhatsApp, which is
// a different claim and the one this file is careful to make.
//
// He is on the Zoom call and cannot read a terminal. WhatsApp is the channel he
// already answers, and scripts/wa_notify.ps1 proved the Cloud API path works
// from this machine. This is the same call in Node so the agent does not need
// PowerShell to speak.
//
// WHY NOTHING HERE SAYS "DELIVERED"
//   A 200 from Graph with a `wamid` means ACCEPTED FOR DELIVERY. It does not
//   mean the message reached the handset, and it certainly does not mean anyone
//   read it. Actual delivery arrives later, on a status webhook this project
//   does not yet receive. Counting an accepted POST as a delivery would put a
//   number on a board finding that reads like evidence and is not — so the
//   metric is called `accepted` everywhere, all the way through to the board.
//
// META'S 24-HOUR RULE: free-form text only delivers within 24 hours of his last
// inbound message. Outside it Graph returns error 131047 and NOTHING ARRIVES.
// A message that silently fails is worse than one never sent, so failures are
// surfaced, never swallowed.
import { config } from './config.js';

const GRAPH = 'https://graph.facebook.com/v21.0';

export function whatsappConfigured() {
  return Boolean(config.metaToken && config.waPhoneNumberId && config.waTo);
}

/**
 * Hand one message to WhatsApp. Returns { ok, id?, error? } and never throws —
 * a delivery failure must not take down the meeting listener.
 *
 * `ok: true` means Graph ACCEPTED the message, not that it arrived.
 */
export async function sendWhatsApp(text) {
  if (!whatsappConfigured()) {
    return { ok: false, error: 'WhatsApp not configured (META_TOKEN / WA_PHONE_NUMBER_ID / WA_TO)' };
  }
  // The stored token already begins with "Bearer " in this project's .env.
  // Sending "Bearer Bearer EAA…" produces a 401/code 190 that reads exactly
  // like an expired token and has cost hours before. Strip it.
  const token = config.metaToken.replace(/^Bearer\s+/i, '');

  const ctl = new AbortController();
  const timeoutMs = config.notifyTimeoutMs;
  const timer = setTimeout(() => ctl.abort(), timeoutMs);
  try {
    const res = await fetch(`${GRAPH}/${config.waPhoneNumberId}/messages`, {
      method: 'POST',
      signal: ctl.signal,
      headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' },
      body: JSON.stringify({
        messaging_product: 'whatsapp',
        to: config.waTo,
        type: 'text',
        text: { preview_url: false, body: text.slice(0, 4000) },
      }),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      const code = data?.error?.code;
      const hint = code === 131047
        ? ' — outside the 24-hour window; he must message the number first'
        : '';
      return { ok: false, error: `Graph ${res.status} code=${code}${hint}` };
    }
    return { ok: true, id: data?.messages?.[0]?.id };
  } catch (err) {
    return {
      ok: false,
      error: err.name === 'AbortError' ? `Graph did not answer within ${timeoutMs / 1000}s` : err.message,
    };
  } finally {
    clearTimeout(timer);
  }
}
