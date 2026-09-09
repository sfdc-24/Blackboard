# SFDC24 Consultant — Zoom RTMS backend

Real-time AI assistance on client calls. This backend listens for Zoom's RTMS
events over a WebSocket subscription, joins the live media stream when a meeting
starts, and pipes the transcript into an assistant hook you can wire to an LLM.

```
┌────────────┐  rtms_started/stopped   ┌──────────────┐   media frames   ┌───────────┐
│ Zoom cloud │ ──── events-ws.js ────▶ │   rtms.js    │ ───────────────▶ │ assistant │
└────────────┘   (event WebSocket)     │ (@zoom/rtms) │  transcript/etc  └───────────┘
```

Matches the Marketplace app **SFDC 24 - Consultant** (user-managed General App,
WebSocket event delivery, meeting RTMS scopes for audio / video / screen share /
transcript / chat).

## Prerequisites

- Node.js **22+** (the locked `@zoom/rtms` 1.1.0 requires it; 24 LTS also fine)
- The Marketplace app configured as we set it up: RTMS scopes, Event
  Subscription (WebSocket) with `meeting.rtms_started`, `meeting.rtms_stopped`,
  `meeting.rtms_interrupted`

## Setup

1. **Install dependencies**

   ```bash
   npm install
   ```

2. **Configure environment**

   ```bash
   cp .env.example .env
   ```

   Then fill in `.env`:

   | Variable | Where to find it |
   |---|---|
   | `ZOOM_CLIENT_ID` | Basic Information → App Credentials (prefilled) |
   | `ZOOM_CLIENT_SECRET` | Basic Information → App Credentials → Show/Copy |
   | `ZOOM_WS_ENDPOINT` | Features → Access → Event Subscription "RTMS media streams" → **Endpoint URL → Copy** (full `wss://ws.zoom.us/ws?subscriptionId=...` URL) |
   | `ZOOM_REDIRECT_URI` | Must exactly match Basic Information → OAuth Redirect URL (`http://localhost:3000/oauth/callback`) |

3. **Run it**

   ```bash
   npm start
   ```

4. **Authorize (first run only)** — the console prints an authorize URL. Open
   it in your browser, click **Allow**. Zoom redirects to
   `localhost:3000/oauth/callback`, the backend stores tokens in
   `.tokens.json`, and immediately connects to the event socket. From then on
   it refreshes tokens automatically.

5. **Start a test meeting.** When RTMS starts for the meeting, you'll see:

   ```
   [events] meeting.rtms_started
   [rtms] joining media stream for <uuid>…
   [rtms] join confirm …
   [transcript] Abdus: Hello everyone…
   ```

> **If no events arrive when you start a meeting:** RTMS must be enabled for
> your account/meetings. In the Zoom web portal (Settings), look for the
> Real-Time Media Streams / auto-start setting and enable it — and confirm the
> app was authorized by the meeting **host** account.

## Project layout

| File | Role |
|---|---|
| `src/index.js` | Entry point — wires everything together |
| `src/config.js` | Env loading; mirrors creds to the SDK's `ZM_RTMS_*` vars |
| `src/oauth.js` | User-managed OAuth: Allow-click → tokens, auto-refresh |
| `src/events-ws.js` | Event WebSocket: keep-alive ping, token recycle, backoff reconnect |
| `src/rtms.js` | Joins/leaves media streams; one client per `rtms_stream_id` |
| `src/assistant.js` | **Your AI goes here** — transcript buffer + LLM hook stubs |

## Extending toward the real product

- **Live suggestions:** in `assistant.js`, batch the last ~30 transcript lines
  and send them to your LLM; deliver output via a Zoom App panel, Slack, or a
  dashboard.
- **Screen-share understanding:** `onShareData` in `rtms.js` receives H.264
  frames — decode keyframes and feed them to a vision model.
- **Audio pipelines:** `client.setAudioParams(...)` before `join()` for raw
  PCM / per-participant streams (see the `@zoom/rtms` docs).
- **Production events:** when you deploy, either keep WebSocket delivery (run
  one instance) or switch the subscription to Webhook with a public HTTPS URL.

## Troubleshooting

| Symptom | Likely cause / fix |
|---|---|
| `Not authorized yet` | Complete the OAuth flow (open the printed URL) |
| Socket closes with 1008 | Token invalid/expired — delete `.tokens.json`, re-authorize |
| `rtms_started` arrives but join fails | Client ID/Secret mismatch — SDK signs with `ZM_RTMS_*` (auto-mirrored from `.env`) |
| Duplicate join / stream kicked | Only one connection per stream — this scaffold guards via the `active` map |
| Segfault on start | Node too old — upgrade to 22+ / 24 LTS |
| No transcript lines | Transcript scope missing, or meeting captions/transcription not active |

---

## The assistant is now wired (2026-09-08)

`assistant.js` was a stub that buffered the transcript and logged it. It now
does the job the project exists for.

**Live, during the call.** It stays silent until someone says a wake word
(`WAKE_WORDS`, default `sfdc24` / `hey consultant`). Then it takes the last
`CONTEXT_LINES` of transcript, asks SFDC24, and puts the answer on the
consultant's phone over WhatsApp while he is still on the call.

Proven against the real backend with `node test/simulate-call.mjs` — a scripted
discovery call where the consultant says *"SFDC24, what should I be asking them
about those flows right now?"*. The answer came back over the wire:

> Ask which of the eleven flows touch close date or stage, since that is what is
> breaking their trust in durations. Then ask if any of them fire on the same
> trigger — that is usually where the fear comes from.

**After the call.** A summary, the action items with owners where named, and
anything said about a Salesforce org worth checking later — to WhatsApp, and as
an evidence-backed finding on the blackboard. The finding carries an explicit
`evidence_boundary`: it is derived from a transcript, nothing in it was verified
against an org, and speech-to-text mishears names and identifiers.

### Why it does not call a model directly

Everything goes through SFDC24's Apps Script `reception()` — the same function
`www.sfdc24.com` and the WhatsApp gateway call. One persona, one set of spend
caps, one quarantined log. A second private path to a model would drift from all
three and bypass the caps that keep the bill bounded.

### Why it stays quiet by default

`ASSIST_EVERY_N=0` ships as the default. A suggestion every N lines would buzz
his phone through a client call and the whole thing would get switched off. Set
it above zero to let the agent volunteer, and even then it is instructed to
answer `NOTHING` when there is nothing worth interrupting for.

One outstanding request per meeting is enforced. Live speech arrives faster than
a model answers, and without that guard a busy call stacks up stale questions
that all land after the moment has passed.

## What it still needs to run for real

Three things, and only the first costs money:

1. **Zoom Developer Pack credits on the account.** RTMS is metered and this is
   the hard prerequisite — see the [RTMS docs](https://developers.zoom.us/docs/rtms/).
   Without credits the app authorizes fine and simply never receives a stream.
2. **`ZOOM_WS_ENDPOINT`** from Marketplace → Features → Access → *RTMS media
   streams* → Endpoint URL. It is not a secret we can derive; it must be copied.
3. **A Linux host.** `@zoom/rtms` is a native module that refuses to install on
   Windows (`npm error notsup`), which is why this project originally lived on
   WSL Ubuntu — an environment that no longer exists on this laptop. The VM is
   the right home: it already runs continuously and the laptop lid closes.

It needs **no public URL, no ngrok, no inbound firewall rule.** Events arrive
over an outbound WebSocket. Only the one-time OAuth consent uses localhost.

## Privacy, stated plainly

This records what clients say on calls, sends excerpts to SFDC24's backend, and
puts summaries on a phone and on a shared board other agents read. That is a
deliberate capability, not an accident — but it should be a decision made on
purpose, and clients should be told a call is being assisted. `LOG_TRANSCRIPT`
is `false` by default so transcripts are not sprayed into a terminal log.
