# The WhatsApp prefix gate: what is ready, and the one thing only he can do

**Status: PREPARED, NOT DEPLOYED.** Everything below is reviewed and waiting on
a single action in the Pipedream builder that no credential on this box can
perform. Read *Why the API cannot do this* before anyone offers to "just use the
API" — that was measured on 2026-09-20 and the answer is no.

## What he asked for

WhatsApp, 2026-09-19T04:26Z:

> Claude-code-cli give me one short prefix i can write that will get immediate
> response from whoever (not you or VM but API driven) so that anytime of day i
> can activate and work with agents without messaging them personally

Voice note, 2026-09-19T23:06Z. It went unheard for seventeen hours, because
inbound media lands on the board as a bare media id and nothing on this fleet
fetched it:

> Hey, Claude Code CLI. So I'm going to open Chrome browser and I'll load up
> Pipe Dream. Do you need that? You actually have API to Pipe Dream so you can
> fix it on your own. But let me know if you need me to do anything on the
> laptop or in the VM.

**The answer to that question is yes, he is needed.** The API cannot.

## Half of the ask is already live without touching Pipedream

`scripts/agent_waker.py` answers a WhatsApp row whose text begins with an
agent's tag, because the prefix IS the address — see `addressed_to`. As of
2026-09-20 that serves **foundry, gemini and grok**, all three API-driven, none
of them this laptop and none of them the VM. So `Grok what is the site status`
already reaches an API agent at any hour and is answered on the board.

What it does **not** do is put the answer back on his phone. That last hop is
the Pipedream step, and that is what this document is about.

## Why the API cannot do this

Measured 2026-09-20 with the Connect OAuth client in `.env`:

| call | result |
|---|---|
| mint client-credentials token | **ok** |
| `GET /v1/users/me` | 200, and `data` comes back **empty** |
| `GET /v1/workflows` | **404** `route not found` |
| `GET /v1/projects/proj_5DsGpGe` | **404** |
| `GET /v1/projects/proj_5DsGpGe/workflows` | **404** |

The credential is a **Connect** OAuth client. Connect authenticates *end users*
to third-party apps; it is not the workspace management API, and it cannot list,
read or write a workflow. There is no `PIPEDREAM_API_KEY` in `.env`, and even a
workspace key would only grant a READ — Pipedream's step source is editable in
the builder or not at all.

**So the one action only he can take is: open the builder and paste.**

## The system, as it actually is

One workflow, `SFDC 24 - WhatsApp to Blackboard`, v265, Active. It is the only
always-on inbound path for the entire fleet. Two steps: an HTTP trigger from
Meta's webhook, and ONE 327-line Node step confusingly named
`send_whatsapp_reply`, which in order:

1. ACKs Meta
2. extracts the message
3. routes it to Gemini / OpenAI / Anthropic / Groq
4. **writes the row to the board** (Apps Script append, returns 200)
5. *used to* send the generated answer back over WhatsApp

Step 5 was stopped on 2026-09-08 by a bare `return;` between 4 and 5, because it
answered everything within two seconds with no identity and no context. Each
execution still takes about 8,000 ms, so those four model calls still run and
their output is still thrown away.

**The board write and the reply live in the same step.** A mistake here does not
break the reply; it makes the whole fleet deaf.

## The change, exactly

Insert immediately **before** the existing bare `return;`, leaving the board
write above it untouched:

```js
// --- GATE: who may get a reply, and on what ------------------------------
// Three conditions, all required. Any one of them alone is a defect:
//   type === "text"   media, reactions, location pins and delivery/read
//                     callbacks reach this webhook with no text.body at all,
//                     and reading it throws - which stops the ACK, which makes
//                     Meta retry for up to seven days at four model calls a go.
//   from === HIM      a prefix alone would hand any client, lead or spammer
//                     who types it a raw model answer from the business number.
//   prefix            what he actually asked for.
const m      = event.body?.entry?.[0]?.changes?.[0]?.value?.messages?.[0];
const isText = m?.type === "text";
const text   = isText ? (m.text?.body ?? "").trim() : "";
const sender = m?.from ?? "";

const PREFIX = "?";
const HIM    = "<WA_TO from .env - 11 digits, no plus, the form Meta sends>";

if (!isText || sender !== HIM || !text.startsWith(PREFIX)) {
  return;                       // silence, exactly as today
}

// Strip the prefix so the model is asked the question, not the punctuation.
const ask = text.slice(PREFIX.length).trim();
if (!ask) { return; }

// --- REPLY: wrapped so it can never take the board write down with it -----
try {
  // existing step-5 send, with the answering model named in the first line
  // so he can tell a considered answer from an autocomplete.
} catch (replyErr) {
  console.error("reply failed, board write already succeeded:", replyErr.message);
  return;                       // never rethrow
}
```

## Order of operations, and it is not negotiable

1. **Extract the baseline first.** Copy all 327 lines out of the builder into a
   local file and commit it. There is no version control on that step; without
   this there is no way back. The file carries the Meta token, the bus secret
   and four model keys as plaintext literals, so it goes to a **gitignored path,
   ignored in the same commit** — never into a transcript, never onto the board.
2. **Answer the `$.respond` question before editing anything.** If step 1 does
   not call `$.respond()`, the HTTP 200 depends on the workflow completing, and
   any runtime error during the edit returns 500 to Meta, which retries. Search
   the extracted baseline for `$.respond`: one grep once the source exists
   locally, and it is the probe that was never run on 2026-09-19.
3. **Paste the gate. Change nothing else.**
4. **Deploy to v266.**
5. **Verify in this order.** Send an unprefixed message from a regular phone →
   the board gets a row, the step exits at the gate, zero outbound. Then send a
   prefixed message from his number → board row AND a reply naming the model.

## What NOT to do, and why

Four refusals from the 2026-09-19 consultation. They still hold.

- **Do not move the model calls above the board write.** It is tempting: it
  stops paying for four discarded answers on every unprefixed message. It also
  makes the board write depend on model latency and uptime, so a slow or failed
  model means the message never reaches the board at all. The waste is cheaper
  than the deafness.
- **Do not move the plaintext secrets to environment variables in the same
  edit.** One casing typo breaks production across four models and the Meta API
  at once. Separate change, separate day.
- **Do not deploy without the sender check.** Without it this is an unmetered
  public LLM proxy on the business number.
- **Beware the builder's Test button.** The event in its dropdown executes live
  side effects — a real Apps Script append and, if the gate passes, a real
  WhatsApp message.

## The 24-hour window

Outside Meta's 24-hour customer service window, free-form text is refused with
error `131047`, and only an approved template delivers. There is no approved
template on this number. So a reply to a message he sent hours earlier may be
refused by Meta even after this ships. That is a Meta policy limit, not a bug in
the gate.

## While this is not deployed

Nothing here blocks him. `Grok ...`, `Gemini ...` and `Foundry ...` on WhatsApp
reach the API agents through the waker today; the answer lands on the board
rather than on his phone. The gate is what closes that last hop.

Related: `docs/PIPEDREAM-CONNECT.md`, `scripts/agent_waker.py`,
`docs/consultations/2026-09-19-editing-the-live-whatsapp-pipedream-step-to-add-a-prefix-rep.md`.
