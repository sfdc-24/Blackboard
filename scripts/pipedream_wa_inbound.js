// SFDC24 Blackboard — WhatsApp inbound code node for Pipedream
// claude-code-cli, 2026-09-01. Merged version: keeps the Meta GET-verification
// handshake from the previous node, adds the junk-row fix (scripts/
// pipedream_wa_inbound.js in quantum/blackboard). Fixes the 04:09-04:27Z flood
// (7x "[object Object]", 14x "undefined"): the old pipeline passed the raw
// webhook body to a template POST step that fired on EVERY delivery.
//
// CONTRACT (Strategy/Doctrine plan v1, Phase 1):
//   - one board row per human MESSAGE, payload carries the wamid (idempotency
//     key, Doctrine #3) in a greppable prefix: WA|wamid=..|from=..|type=..|text=..
//   - status-only deliveries (sent/delivered/read/failed) append NOTHING;
//     they are counted and exported for a future statuses lane.
//   - no extractable text -> THROW (fail loud, Doctrine #9). Never append
//     "undefined" or "[object Object]".
// ENV (per DOCTRINE D-18 — secrets never in code): ALPHA_URL, ALPHA_SECRET,
// META_VERIFY_TOKEN. Optional: META_APP_SECRET to enable signature validation.
// Attach the required "db" Data Store. Its get/set pair is NOT atomic; serialize
// this workflow and retain board read-back before any uncertain-write replay.

import crypto from "crypto";

function extractText(msg) {
  switch (msg.type) {
    case "text":        return msg.text?.body ?? null;
    case "button":      return msg.button?.text ?? null;
    case "interactive": return ['button_reply', 'list_reply'].includes(msg.interactive?.type)
                          ? msg.interactive[msg.interactive.type]?.title ?? null : null;
    case "reaction":    return msg.reaction?.emoji
                          ? `reacted ${msg.reaction.emoji} to wamid=${msg.reaction.message_id}` : null;
    case "image": case "video": case "audio": case "document": case "sticker":
      return msg[msg.type]?.caption ?? `[${msg.type} received - no caption]`;
    case "location":
      return msg.location ? `[location ${msg.location.latitude},${msg.location.longitude}]` : null;
    case "contacts":    return "[contact card received]";
    default:            return null; // unknown type -> caller throws, visibly
  }
}

function normalizedMessage(msg) {
  const id = /^wamid\.[A-Za-z0-9+/=_-]{1,500}$/;
  if (!msg || typeof msg.id !== 'string' || !id.test(msg.id) ||
      typeof msg.from !== 'string' || !/^\d{6,20}$/.test(msg.from)) {
    throw new Error('missing or invalid WhatsApp message identity');
  }
  const text = extractText(msg);
  if (typeof text !== 'string' || !text.trim() || text.length > 16000) {
    throw new Error('missing, invalid or oversized WhatsApp message text');
  }
  const replyTo = msg.context?.id ?? null;
  if (replyTo !== null && (typeof replyTo !== 'string' || !id.test(replyTo))) {
    throw new Error('invalid quoted WhatsApp message identity');
  }
  let selection = null;
  if (msg.type === 'interactive') {
    const kind = msg.interactive?.type;
    if (!['button_reply', 'list_reply'].includes(kind)) throw new Error('unsupported interactive response');
    const choice = msg.interactive[kind];
    if (!choice || typeof choice.id !== 'string' || !choice.id.trim() || choice.id.length > 512) {
      throw new Error('interactive response has no valid option identity');
    }
    selection = { kind, id: choice.id, title: text };
  } else if (msg.type === 'button') {
    if (typeof msg.button?.payload !== 'string' || !msg.button.payload.trim() || msg.button.payload.length > 512) {
      throw new Error('button response has no option identity');
    }
    selection = { kind: 'button', id: msg.button.payload, title: text };
  }
  return { wamid: msg.id, from: msg.from, type: msg.type, text, replyTo, selection };
}

function boardPayload(message) {
  let prefix = `WA|wamid=${message.wamid}|from=${message.from}|type=${message.type}`;
  if (message.replyTo) prefix += `|reply_to=${message.replyTo}`;
  if (message.selection) prefix += `|choice_id=${encodeURIComponent(message.selection.id)}`;
  // Keep text last and unchanged for existing WA|wamid= readers; downstream
  // routing uses the structured message export, never a split of visitor text.
  return prefix + '|text=' + message.text;
}

function sameSecret(actual, expected) {
  if (typeof actual !== 'string' || typeof expected !== 'string') return false;
  const actualBytes = Buffer.from(actual, 'utf8');
  const expectedBytes = Buffer.from(expected, 'utf8');
  return actualBytes.length === expectedBytes.length && crypto.timingSafeEqual(actualBytes, expectedBytes);
}

function headerValue(headers, wantedName) {
  if (!headers || typeof headers !== 'object') return undefined;
  if (typeof headers.get === 'function') return headers.get(wantedName) ?? undefined;
  const matches = Object.entries(headers).filter(([name]) => name.toLowerCase() === wantedName.toLowerCase());
  if (matches.length > 1) throw new Error(`duplicate ${wantedName} header`);
  const value = matches[0]?.[1];
  return Array.isArray(value) && value.length === 1 ? value[0] : value;
}

export default defineComponent({
  props: {
    // Pipedream rejects optional:true on data_store props ("UserError: optional
    // not supported for prop db", found in step-test 2026-09-01) -- required.
    db: { type: "data_store" }, // wamid dedup store (Doctrine #3: Meta retries -> dupes without it)
  },
  async run({ steps, $ }) {
    const event = steps.trigger.event;

    // Boolean-only env presence check — never the values (D-18).
    $.export("env_ok", {
      ALPHA_URL: !!process.env.ALPHA_URL,
      ALPHA_SECRET: !!process.env.ALPHA_SECRET,
      META_VERIFY_TOKEN: !!process.env.META_VERIFY_TOKEN,
    });

    // --- Meta webhook GET verification handshake (kept from previous node) ---
    const q = event.query || {};
    if (event.method === "GET") {
      const verify = q["hub.verify_token"] || q["hub_verify_token"];
      const challenge = q["hub.challenge"] || q["hub_challenge"];
      const expectedVerify = process.env.META_VERIFY_TOKEN;
      if (!expectedVerify) {
        await $.respond({ status: 500, body: "Verification unavailable" });
        return $.flow.exit("GET while META_VERIFY_TOKEN is not configured");
      }
      const challengeText = typeof challenge === 'number' ? String(challenge) : challenge;
      if (sameSecret(verify, expectedVerify) && typeof challengeText === 'string' && /^\d{1,32}$/.test(challengeText)) {
        await $.respond({ status: 200, body: challengeText });
        return $.flow.exit("webhook verification handshake");
      }
      await $.respond({ status: 403, body: "Forbidden" });
      return $.flow.exit("GET without valid verify token");
    }
    if (event.method !== "POST") {
      await $.respond({ status: 403, body: "Forbidden" });
      return $.flow.exit(`unsupported method ${event.method}`);
    }

    // Meta requires a response in ~5s and disables the webhook after repeated
    // failures — ACK before doing the slow Apps Script work.
    await $.respond({ immediate: true, status: 200, body: "EVENT_RECEIVED" });

    const url = process.env.ALPHA_URL;
    const secret = process.env.ALPHA_SECRET;
    if (!url || !secret) {
      throw new Error("ALPHA_URL / ALPHA_SECRET env vars missing (D-18: secrets live in env, never in code)");
    }
    if (!this.db || typeof this.db.get !== 'function' || typeof this.db.set !== 'function') {
      throw new Error('WhatsApp deduplication Data Store is not configured');
    }

    // Signature validation (plan Phase 1): enable by setting META_APP_SECRET.
    // Header names are case-insensitive. Uses the raw body when the trigger
    // exposes it; skips (and says so) when not.
    const appSecret = process.env.META_APP_SECRET;
    const rawBody = event.raw_body ?? event.body_raw;
    let signature = "not checked (META_APP_SECRET unset)";
    if (appSecret) {
      const sigHeader = headerValue(event.headers, 'x-hub-signature-256');
      if (typeof rawBody !== 'string' || !rawBody) {
        throw new Error('raw webhook body unavailable; cannot verify configured signature');
      } else if (!sigHeader) {
        throw new Error("X-Hub-Signature-256 header missing - refusing unsigned webhook");
      } else {
        const expected = "sha256=" + crypto.createHmac("sha256", appSecret).update(rawBody, "utf8").digest("hex");
        if (typeof sigHeader !== 'string' || sigHeader.length !== expected.length ||
            !crypto.timingSafeEqual(Buffer.from(expected), Buffer.from(sigHeader))) {
          throw new Error("X-Hub-Signature-256 mismatch - dropping payload");
        }
        signature = "valid";
      }
    }

    // When authenticated, consume exactly the signed bytes, not an independently
    // supplied parsed object that could have diverged in an earlier step.
    const body = signature === 'valid' ? JSON.parse(rawBody) : event.body ?? {};
    const results = [];
    const messages = [];
    let statusEvents = 0;

    for (const entry of body.entry ?? []) {
      for (const change of entry.changes ?? []) {
        const value = change.value ?? {};

        // Status deliveries: count, never append. (These were the "undefined" rows.)
        if (Array.isArray(value.statuses)) statusEvents += value.statuses.length;

        for (const msg of value.messages ?? []) {
          const message = normalizedMessage(msg);
          const wamid = message.wamid;

          // Idempotency (Doctrine #3): Meta retries; consumers must not see dupes.
          if (this.db && await this.db.get(wamid)) {
            results.push({ wamid, skipped: "duplicate delivery" });
            continue;
          }

          const payload = boardPayload(message);

          // Apps Script answers 302; fetch converts the redirected POST to a
          // body-less GET automatically — the same two-hop pattern as the
          // PowerShell clients. Do not disable redirect following here.
          const res = await fetch(url, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              secret,
              source_tag: "whatsapp",
              target_surface: "Blackboard Alpha DB",
              action_type: "APPEND",
              payload,
            }),
            signal: AbortSignal.timeout(30000),
          });
          let gatewayReply;
          try { gatewayReply = await res.json(); }
          catch { throw new Error('board append returned invalid JSON; read back before replay'); }
          if (!res.ok || gatewayReply?.result !== 'success' || gatewayReply.error ||
              typeof gatewayReply.rowId !== 'string' || !/^WRK-[A-Za-z0-9-]+$/.test(gatewayReply.rowId)) {
            throw new Error('board append was not acknowledged; read back before replay');
          }

          await this.db.set(wamid, { rowId: gatewayReply.rowId, acceptedAt: new Date().toISOString() });
          messages.push(message);
          results.push({ wamid, appendAcknowledged: true, rowId: gatewayReply.rowId });
        }
      }
    }

    // D-4 caveat, encoded: the gateway reply is NOT proof the row landed.
    // Read-back on the sheet remains the arbiter; this export is diagnostics.
    $.export("summary", {
      schema: 2,
      signature,
      messagesAcknowledged: messages.length,
      duplicatesSkipped: results.filter(r => r.skipped).length,
      statusEventsIgnored: statusEvents,
      messages,
      results,
    });
  },
});
