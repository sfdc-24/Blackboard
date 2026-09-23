# Working a client's fix live, through sfdc24.com, out loud

> **STATUS: the path, measured. One leg is blocked on a key, one leg is proven,
> one leg is unbuilt.** Nothing here is a commitment to a date.

Asked for by Mr Salam, **2026-09-23**: *"pave the way so this can be done using
sfdc24.com as the gateway with streaming audio fixed first. and then ideally
during a zoom session meeting you'd be working on the fix live"* and
*"and explaining how you arrive to a solution live as well"*.

The case it has to carry is real and already open: **Access Haiti** — a Visual
Flow on Work Order that cannot pull a serial number, hit on a call on
2026-09-22.

## What the demo actually is

Not a product tour. A client watches their own problem get worked:

1. They **speak** the problem into sfdc24.com. Not type it.
2. The site answers **correctly**, which for this case it could not do
   yesterday.
3. The fix is built **while they watch**, in a Zoom session.
4. And the reasoning is **said out loud as it happens** — which is the part that
   sells, because it is the part a competitor cannot screenshot.

That last item is the whole pitch: being wrong is expensive, and what they are
buying is a method that shows its work. A silent correct answer is a commodity.

## Leg 1 — the gateway answers the case correctly. DONE 2026-09-23.

This was the leg nobody had checked, and it was failing.

Seven questions from the case were put to the live page on 2026-09-22 and **six
came back wrong**, confidently — including *"the Asset object, on the field
SerialNumber"*, which is the client's own bug. The page would have told them to
keep doing the broken thing, on camera.

Grounded and merged as sfdc24-site #129: four checkable facts answer before any
model call, and fifteen adjacent questions are asserted to still reach an agent
rather than a canned fact. **Ten of ten, in CI.**

## Leg 2 — the voice surface keeps a thread. DONE 2026-09-23, and it was broken.

A spoken conversation is worthless if turn two does not remember turn one.

`www.sfdc24.com/voice/` was sending the old caller-chosen `vid` and no signed
conversation token, so after the endpoint moved to a signed identity **every
turn opened a brand-new conversation**. Fixed and verified on www in #130, with
the guard placed in the repository that publishes the page.

**The trap worth naming:** there are two `voice/index.html` files, in two
repositories, and the tenant-boundary suite reads the one that is not served. It
was 12/12 throughout. See `[[two-voice-pages-i-grepped-the-wrong-one]]`.

## Leg 3 — streaming audio. NOT BUILT. Blocked on one key.

Today the voice page uses the **browser's own `SpeechRecognition`**. That is
dictation, not streaming transcription: it stops on pauses, it varies by browser,
and on a client call it is the thing that will embarrass us.

Measured on the served page: `AudioWorklet` 0, `getUserMedia` 0, `WebSocket` 0,
`MediaRecorder` 0. There is no streaming pipeline to fix — there is one to build.

The shape, split so no provider key ever reaches a browser:

```
top-level page  ──AudioWorklet, 16 kHz mono PCM──▶  WSS relay  ──▶  STT provider
   (mine)                                          (codex-cloud)      (Deepgram)
       ◀────────── interim + final transcript ──────────┘
       │
       └── final text ──▶ the existing /exec ask path ──▶ answer ──▶ TTS back
```

- **Page and demo UX: mine.** Claimed on the board as
  `CCC-STT-DEMO-ACK-20260923T0025Z`. AudioWorklet capture, a visible microphone
  state, a visible connection state, interim text rendering as it arrives, and a
  Stop that actually stops.
- **Relay: codex-cloud's**, per `STT-DEMO-CODEX-20260922`. That split is not
  bureaucracy — a browser that holds the provider key is a free transcription
  service for anyone who reads the page source.
- **Hosting: Cloud Run on GCP**, his decision 2026-09-23. Scales to zero, so an
  idle demo costs nothing. GitHub Pages cannot host a WSS endpoint; that is the
  same wall the visitor-feedback endpoint hit.
- **Blocked on:** one provider key, `DEEPGRAM_API_KEY` recommended. Nothing
  starts without it, and it belongs in the relay's environment, never in this
  repository and never in the page.
- **It must not touch the production ask bar.** A standalone page, for two
  reasons: grok's scope line said so, and an iframed microphone can never work,
  so the surface has to be top-level regardless.

**What cannot be proven from a session:** a real microphone end to end. The last
mile is a click he makes once, and it will be reported as his click rather than
as a passing test.

## Leg 4 — the Zoom session. PARTLY PROVEN.

Two-way voice in a Zoom meeting is **already proven** on this fleet, with a
recorded bring-up order. What is not built is the choreography for *this* demo:

- **The agent joins as a participant that talks.** Proven. The room must be a
  scheduled meeting rather than an Instant one, because an Instant meeting
  cannot carry `join_before_host` — check the meeting TYPE before touching
  anything.
- **The client shares their org.** Their screen, their sandbox. Nothing here
  needs credentials to their org, and that is deliberate: the demo is the
  method, not a login.
- **The reasoning is narrated as it happens.** This is the unbuilt piece and it
  is a *content* problem, not a plumbing one. It needs a spoken structure that
  survives being wrong in public: state the hypothesis, name what would
  disprove it, run the check, say what the result changed. The Access Haiti case
  is a good first script precisely because the first plausible answer — Asset —
  is the wrong one, so the narration has somewhere to go.
- **Do not narrate from the box's own speaker.** Voice belongs on the laptop
  persona, not the machine running the fleet.

## The order, and why it is this order

1. **Leg 1 and Leg 2 are done**, and they had to be first: a demo that gives a
   confident wrong answer, or forgets the previous sentence, fails in front of
   the client rather than in front of us.
2. **Leg 3 next, on one key.** Until then the demo can be rehearsed with the
   browser's dictation and a stub relay — the page can be built against a local
   echo and re-pointed when the real relay exists, so his key is not on the
   critical path for the UX work.
3. **Leg 4 last**, because it is the only leg where a failure happens in front of
   a client, and it should be rehearsed against a recording of the case before
   it is run against the client.

## What is still missing from the case itself

The fix cannot be *worked* live until the case has a root cause, and the
repository holds no Flow metadata. Two things are needed and only he has them:
the **Flow API name** and the **red fault text with its element name**. Without
those the demo can show the diagnosis being reasoned about, which is honest, but
not the fix landing.

Related: `docs/BLACKBOARD-CRM-LANE.md`, `docs/CONSOLE-MIGRATION-READINESS.md`,
and PR #7 on `sfdc-24/access-haiti-salesforce` for the case report.
