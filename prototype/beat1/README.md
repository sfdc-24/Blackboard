# Beat 1 and Beat 3 — the prototype

Talk, watch your thing draw itself, correct it, and leave with a page you can
send to your manager. This is `docs/PRODUCT.md` beats 1 to 3 running end to end.

## Run it

```
node prototype/beat1/serve.js
```

Then open:

- **http://127.0.0.1:8732** — typed
- **http://127.0.0.1:8732/voice.html** — voice, with the sketch on screen while it speaks

Stop it with ctrl-c. The per-process browser capability disappears with the
process. Conversation history is only a ten-message rolling window in the open
page; the voice page keeps only its device-voice preference in browser storage.

**If the URL is dead, the server just isn't running.** It is a foreground process
tied to whoever started it, so it dies when that session ends — that is the whole
explanation, and the command above is the whole fix.

## What you should see

Type something real, like *"leads come in from a web form, a partner spreadsheet
and reps typing them in, and two of those skip our assignment rules so they sit
unowned for days"*.

1. A blocky sketch assembles beside the conversation — the intake paths, the
   assignment step, and a red box for the part that breaks.
2. The reply does **not** describe the picture. It asks you to correct it. That
   is beat 2 arriving on its own, because beat 1 is visible.
3. The blue button turns it into a self-contained page in a new tab.

## Why it looks unfinished

Because it is meant to. Blocky reads as *"tell me what is wrong with this"* and
invites the correction beat 2 needs. A polished diagram invites agreement, and
agreement is not information. Speed beats polish in beats 1 and 2 — a rough thing
in ninety seconds beats a beautiful thing in a week.

## Boundary and outbound behavior

- The HTTP listener binds only to `127.0.0.1`. Every API request also needs an
  unguessable capability rendered into the page for that one server process;
  the server rejects any `Host` other than its exact numeric-loopback authority
  before rendering that capability. That is `127.0.0.1:<port>`, with portless
  `127.0.0.1` accepted only when the listener actually owns HTTP's default port
  80. This is the loopback DNS-rebinding boundary.
- Visitor descriptions and recent conversation text **do leave the machine**:
  they are sent over HTTPS only to `api.anthropic.com/v1/messages`. A normal
  typed or voice turn reserves exactly one provider attempt and requests one
  structured `{reply, sketch}` envelope. The fields are validated independently:
  a malformed sketch cannot cost a valid reply or trigger a retry.
- Building the final page makes **no** provider request. Its short prose is
  deterministically grounded only in the re-sanitised corrected sketch.
- The default ceiling is 60 provider attempts for one server process. Failed
  attempts count. To choose a
  lower reviewed ceiling, set `BEAT1_PROVIDER_CALL_CAP` before starting; accepted
  values are 1 through 500. A synchronous reservation means concurrent callers
  cannot both consume the final slot.
- Provider responses have a 2 MiB ceiling and a 45-second absolute wall-clock
  deadline; response trickle does not extend it.
- Voice output uses the browser/device voice. The prototype does not call a TTS
  provider, expose a JSONP route, put text/capabilities in query strings, or
  pretend to implement the production Google sign-in flow.
- Microphone listening begins only after an explicit **Start talking** tap.
  Entering a nonempty text draft ends microphone listening immediately while
  preserving any reply already in progress. Replaying an answer or pressing Stop
  cancels the previous exchange. Interim speech text is removed when capture
  stops; late responses and speech callbacks cannot restart a stopped exchange.
- Generated pages are self-contained, carry a restrictive CSP, make no runtime
  network requests, and are not uploaded anywhere by this prototype.

## What it does not do, and why

- **It never touches your Salesforce org.** The generated page says so in its own
  footer. It is built from what someone said, not from an audit, and implying
  otherwise would sell better and be a lie.
- **It asks for nothing.** No email, no form in front of the result. Money comes
  after the working thing, not in exchange for it.
- **It does not mutate project systems.** The server reads
  `ANTHROPIC_API_KEY` and optional `ANTHROPIC_MODEL` from the ignored repository
  root `.env`; it performs no board writes, deploys, DNS changes or provider
  configuration changes.

## Why it is not in the deploy source

`prototype/beat1/` is not referenced by `.clasp.json` or any deployment workflow.
The reviewed Apps Script source remains under `apps-script/governor-page-api/`.
This prototype can therefore be inspected and run without silently changing a
staging or production target.

## Shape of it

| file | what it is |
|---|---|
| `sketch.js` | **Rebuilds every sketch field** from the one-call envelope — the rendering trust boundary. |
| `build.js` | Deterministic. Sketch plus four short strings in, a whole HTML page out. No network, no key. |
| `serve.js` | One-envelope turn contract, fixed-host HTTPS transport, whole-call reservation, strict Host/capability boundary, and deterministic Beat-3 grounding. |
| `history.js` | Shared browser rolling-window contract; retains accepted user text even when upstream fails and trims before serialisation. |
| `index.html` | The typed surface using same-origin POST. |
| `voice.html` | The local voice surface using same-origin POST, browser speech recognition and the device voice. |

The tests assert what the code **refuses**: injected fields, ambiguous ids,
dangling arrows, over-building, invented prose, cross-origin loopback use,
source-file serving, one-call cardinality, concurrent final-slot reservation,
absolute deadlines, long-session history, deterministic Beat-3 grounding, and
claims about work that never happened. Tests use fake credentials and stubbed
providers; they make no external provider request.

These offline tests prove the request, reservation, parsing, sanitisation and
rendering contracts. They do not prove that a live probabilistic model will
produce a useful sketch, follow the envelope reliably, or avoid invented facts;
that remains an explicit exact-head live acceptance step.

```
node tests/test_sketch_contract.js
node tests/test_build_page.js
node tests/test_beat1_history.js
node tests/test_beat1_provider.js
node tests/test_beat1_runtime.js
node tests/test_beat1_voice.js
```
