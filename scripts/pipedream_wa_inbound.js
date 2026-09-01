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
// ENV (per DOCTRINE D-18 — secrets never in code): ALPHA_URL, ALPHA_SECRET.
// Optional: META_APP_SECRET to enable signature validation.
// OPTIONAL PROP: attach a Data Store as "db" for wamid dedup — Meta retries
// deliveries, and without the store a retry appends a duplicate row.

import crypto from "crypto";

function extractText(msg) {
  switch (msg.type) {
    case "text":        return msg.text?.body ?? null;
    case "button":      return msg.button?.text ?? null;
    case "interactive": return msg.interactive?.button_reply?.title
                            ?? msg.interactive?.list_reply?.title ?? null;
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
    });

    // --- Meta webhook GET verification handshake (kept from previous node) ---
    const q = event.query || {};
    if (event.method === "GET") {
      const verify = q["hub.verify_token"] || q["hub_verify_token"];
      const challenge = q["hub.challenge"] || q["hub_challenge"];
      if (verify === "sfdc24_verify_2024") {
        await $.respond({ status: 200, body: challenge });
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

    // Signature validation (plan Phase 1): enable by setting META_APP_SECRET.
    // Uses the raw body when the trigger exposes it; skips (and says so) when not.
    const appSecret = process.env.META_APP_SECRET;
    const sigHeader = event.headers?.["x-hub-signature-256"];
    const rawBody = event.raw_body ?? event.body_raw;
    let signature = "not checked (META_APP_SECRET unset)";
    if (appSecret) {
      if (!rawBody) {
        signature = "SKIPPED - raw body not exposed by trigger; enable raw body in trigger config";
      } else if (!sigHeader) {
        throw new Error("X-Hub-Signature-256 header missing - refusing unsigned webhook");
      } else {
        const expected = "sha256=" + crypto.createHmac("sha256", appSecret).update(rawBody, "utf8").digest("hex");
        if (!crypto.timingSafeEqual(Buffer.from(expected), Buffer.from(sigHeader))) {
          throw new Error("X-Hub-Signature-256 mismatch - dropping payload");
        }
        signature = "valid";
      }
    }

    const body = event.body ?? {};
    const results = [];
    let statusEvents = 0;

    for (const entry of body.entry ?? []) {
      for (const change of entry.changes ?? []) {
        const value = change.value ?? {};

        // Status deliveries: count, never append. (These were the "undefined" rows.)
        if (Array.isArray(value.statuses)) statusEvents += value.statuses.length;

        for (const msg of value.messages ?? []) {
          const wamid = msg.id;
          if (!wamid) {
            throw new Error("message with no wamid - refusing to append an unkeyed row: " +
              JSON.stringify(msg).slice(0, 200));
          }

          // Idempotency (Doctrine #3): Meta retries; consumers must not see dupes.
          if (this.db && await this.db.get(wamid)) {
            results.push({ wamid, skipped: "duplicate delivery" });
            continue;
          }

          const text = extractText(msg);
          if (!text) {
            // Fail loud (Doctrine #9). This surfaces in Pipedream as a visible
            // error with the offending type — the dead-letter lane's raw input.
            throw new Error(`no extractable text: wamid=${wamid} type=${msg.type} - not appending`);
          }

          const payload = `WA|wamid=${wamid}|from=${msg.from}|type=${msg.type}|text=${text}`;

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
          });
          const gatewayReply = (await res.text()).slice(0, 200);

          if (this.db) await this.db.set(wamid, new Date().toISOString());
          results.push({ wamid, from: msg.from, type: msg.type, appended: true, gatewayReply });
        }
      }
    }

    // D-4 caveat, encoded: the gateway reply is NOT proof the row landed.
    // Read-back on the sheet remains the arbiter; this export is diagnostics.
    $.export("summary", {
      signature,
      messagesAppended: results.filter(r => r.appended).length,
      duplicatesSkipped: results.filter(r => r.skipped).length,
      statusEventsIgnored: statusEvents,
      results,
    });
  },
});
