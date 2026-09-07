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

Stop it with ctrl-c. Nothing is left behind.

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

## What it does not do, and why

- **It never touches your Salesforce org.** The generated page says so in its own
  footer. It is built from what someone said, not from an audit, and implying
  otherwise would sell better and be a lie.
- **It asks for nothing.** No email, no form in front of the result. Money comes
  after the working thing, not in exchange for it.
- **It runs locally only.** Bound to `127.0.0.1`, credentials from `.env`, no
  board writes and no deploys.

## Why it is not in `gas/`

`gas/` is byte-identical to deployed **v31**, and the published fingerprint
(`GAS-V31-DIGEST-002`) is only worth something while that stays true. The site
release gate is also open with PR4. A product idea is not a reason to break a
guarantee other people were asked to rely on, so this moves into the Apps Script
project only once it has been seen and accepted.

## Shape of it

| file | what it is |
|---|---|
| `sketch.js` | Apps-Script-shaped. Asks for the sketch and **rebuilds every field** — the trust boundary, since the model generates from visitor text. |
| `build.js` | Deterministic. Sketch plus four short strings in, a whole HTML page out. No network, no key. |
| `serve.js` | Local server. Loads the other two **verbatim** with `UrlFetchApp` shimmed onto https, so what runs is what the tests proved. |
| `index.html` | The typed surface. |
| `voice.html` | The real voice page with the sketch stage grafted on — speech handling untouched. |

Covered by `tests/test_sketch_contract.js` (26) and `tests/test_build_page.js`
(25). Both assert what the code **refuses**: injected fields, dangling arrows,
over-building, invented prose, and claiming work that never happened.

```
node tests/test_sketch_contract.js
node tests/test_build_page.js
```
