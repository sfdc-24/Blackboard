# Paid-voice patch — the caps must bound the bill, and one key must buy one render

**Target:** Apps Script project `SFDC24 - Governor Page API`, file `Code.js`
(pulled to the gitignored `gas/`). Written 2026-09-05 by claude-code-cli against
site P0 issue 2, command row `WRK-codex-site-p0-20260904T212005956Z`.

**Status: APPLIED to `gas/Code.js`, proved by `tests/test_tts_guards.js`
(38 assertions, PASS). NOT DEPLOYED — the command row says contain the issue
before more voice work, with no paid canary and no production promotion. The
Governor Page API is still serving the previous version.**

This file exists because `gas/` is gitignored, so the change itself cannot be
committed. If the live source is ever lost or re-pulled over, reapply from here.

## The defect

Two of them, in the two halves of the two-step voice call.

### A · paid renders survived every chat cap

`offline_()` returns `{ ok: true, reply: <canned note>, degraded: <reason> }`.
It is the right shape for the visitor — they still get heard when the model is
unavailable — but the mint condition in `voiceReply_` only looked at `ok`:

```js
if (out && out.ok && out.reply && ttsConfigured_()) {   // <- the hole
```

So every reply the chat budget had *already refused* still minted a paid
text-to-speech key:

| refusal | what it was meant to do | what actually happened |
|---|---|---|
| `session-cap` (12) | stop one visitor spending more | canned note, spoken on our card, forever |
| `daily-cap` (150) | bound the whole site per day | same |
| `CHAT_ENABLED=off` | kill switch, no deploy needed | model stopped; **the bill did not** |
| `no-key` / `api-error` | fail safe | paid render of an apology |

The caps only bound cost if the paid path is bounded by the same decision. The
kill switch is the sharpest case: pulling it took the service down and kept
spending.

### B · the key was single-use by comment, not by construction

`ttsAudio_` did:

```js
var text = cache.get('tts_' + ak);
...
cache.remove('tts_' + ak);          // "One audio render per key."
```

`get` then `remove` is not atomic. Two requests carrying the same `ak` both read
the text before either deletes it, and each pays for its own render. N
concurrent replays, N renders, one key.

## The patch

Three changes, all in `Code.js`.

**1 · mint only for a reply the caps allowed.** In `voiceReply_`:

```js
if (out && out.ok && out.reply && !out.degraded && ttsConfigured_()) {
```

A degraded reply is now spoken by the browser's own synthesiser. The voice page
already does exactly that when no `ak` comes back
(`site/voice/index.html`: `if (voice === "__local" || !ak) { speakLocal(...) }`),
so nothing goes silent — the canned note is read aloud in the phone's voice.

**2 · an independent kill switch.** `ttsConfigured_()` now returns false when
Script Property `TTS_ENABLED` is `off`. `CHAT_ENABLED=off` already stops both,
but there was no lever for *keep answering, stop buying audio*, and a switch you
have to take the whole service down to pull is not a switch.

**3 · an exclusive claim.** The read-and-delete moved into `ttsClaim_(ak)`,
inside a `LockService` script lock. The first caller takes the text and deletes
it in the critical section; every other caller gets `expired` — the same answer
a stale key gets, because the caller cannot tell the difference and does not
need to. `ttsAudio_` also now checks `ttsConfigured_()` before it claims, so
change 2 covers direct calls to `action=tts` and not only minting.

## Honest limit — read this before claiming exactly-once

CacheService is eventually consistent. That is the same property that forced the
v15 idempotency fix to read the sheet rather than the cache. The lock therefore
closes the window from *any two concurrent callers* to *a cache replica that has
not yet observed the delete*. It is a large reduction, not a proof.

It is proportionate because the mint side is now bounded: at most
`CHAT_SESSION_CAP` keys per session and `CHAT_DAILY_CAP` per day, so the worst
case is a small multiplier on an already-bounded number rather than an open
ended bill. A strongly consistent claim would cost a sheet write per render.
If that trade ever stops being worth it, the sheet is where to go.

## How it was proved

`node tests/test_tts_guards.js` — no network, provider mocked, and every
assertion counts calls to `api.openai.com`:

```
T1  healthy turn                     key minted, exactly 1 render
T2  past the session cap             no key, 0 renders
T3  past the daily cap               no key, 0 renders
T4  CHAT_ENABLED=off                 no key, 0 model calls, 0 renders
T5  upstream 500                     visitor answered, no key, 0 renders
T6  TTS_ENABLED=off                  chat normal, no key, direct call refused
T7  key replayed sequentially        second use expired, exactly 1 render
T8  two callers racing one key       loser blocked on the lock, exactly 1 render
T9  structural                       the cache read happens inside the lock
T10 LEGACY WITNESS                   old mint condition DID buy past the cap
T11 LEGACY WITNESS                   old claim DID pay twice for one key
```

T10 and T11 re-run two scenarios against the pre-fix code shape and watch them
fail. Without them T2 and T8 would be assertions that might always have been
true; with them they are evidence.

The caps are reached the way a visitor reaches them — twelve real turns through
`reception()` — not by poking a counter.

## To deploy, when the release gate opens

```
cd /c/Users/salam/Quantum/Blackboard      # canonical case, or clasp cries symlink
node tests/test_tts_guards.js             # must print VERDICT: PASS
clasp push -f
clasp create-version "voice: paid renders bounded by the chat caps; one key one render"
clasp redeploy AKfycbx0D-5DAnMqOm9YbN3iKDwuiBApEi_xex60f6pwdvObEyQBF5jcOK715pl1mN-Nzn6gng -V <n> -d "..."
clasp list-deployments                    # read-back is the only proof (D-4)
```

Rollback is `clasp redeploy <id> -V <previous>`.

**Do not do this yet.** The command row's containment scope is the fix and the
proofs. Promotion is the site release lane's call, and `OPENAI_KEY` is still
unset in production, so the paid path is inert there regardless.
