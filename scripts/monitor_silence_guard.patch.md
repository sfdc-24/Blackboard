# Monitor patch — the watchdog must not fail open

**Target:** Apps Script project `SFDC24 - Governor Page API`, file `Monitor.js`
(pulled to the gitignored `gas/`). Written 2026-09-05 by claude-code-cli.

**Status: DEPLOYED AND VERIFIED — version 30, 2026-09-05 ~20:04 EDT.** Applied to
`gas/Monitor.js`, proved by `tests/test_monitor_silence.js` (19 assertions,
PASS), promoted on Mr. Salam's direct instruction, which overrode the
release-lane hold this file originally asked for. Production read back as `@30`.
`gas/` is gitignored, so this file is the durable record.

**One thing this deploy did NOT settle.** The code fix is live; the *trigger's*
liveness is still unproven, because reading the Triggers panel and
`MONITOR_ENABLED` needs the editor and the deploying session had no browser.

**And the obvious test does not work — do not run it and draw a conclusion.**
The idea was: the board went 21h silent then active again the same evening, so a
live trigger owes a board-recovered email. It owes nothing. The monitor emails on
**state change only**, and this file's own finding is that the malformed
timestamp made the scan report *active* right through the silence. So
`MONITOR_STATE.silence` was almost certainly already `active`, the board being
genuinely active now is not a change, and a perfectly healthy trigger sends
nothing. Absence of an email is consistent with both a live trigger and a dead
one, so it distinguishes nothing. (Checked: no monitor mail in the last two days.
That is the expected reading either way.)

Two things actually settle it:

- **The editor's Triggers panel** — one `monitorTick`, error rate `-`, plus
  `MONITOR_ENABLED` and `MONITOR_STATE`. Needs a browser.
- **Attach a standard GCP project to the script**, which turns on Cloud Logging
  and makes `clasp tail-logs` show every `monitorTick` execution from the shell.
  `clasp tail-logs` today answers *"GCP project ID is not set, unable to
  continue."* Worth doing on its own merits: it is the same prerequisite the
  Google sign-in work already needs (`docs/HANDOVER.md`), and it would replace
  "ask someone with a browser" with a command for every future question of this
  shape.

## What was observed

The board carried no row between `2026-09-05T02:38:17Z` and `23:47Z` — about 21
hours, against `MON_SILENCE_HOURS = 6`. No alert reached `abdus@sfdc24.com`;
the only SFDC24 mail in three days is a Search Console notice and a GitHub PR
comment.

The monitor is the only watchdog that keeps running with the laptop shut. One
that reports calm through a 21-hour stoppage is the exact failure it was built
to prevent.

**The cause of that specific miss is NOT established here.** Reading
`MONITOR_STATE` and `MONITOR_ENABLED` needs the Apps Script editor, so a dead
trigger or a flipped kill switch are still live possibilities and someone with
the editor should check both. What follows is a latent fail-open path found
while looking, which produces exactly that symptom.

## The defect

`scanBoard_` chose the newest row by **string comparison**:

```js
var t = iso_(vals[i][tsCol]);
if (t > res.newestTs) res.newestTs = t;
...
var ms = Date.now() - Date.parse(res.newestTs);
if (!isNaN(ms)) res.ageHours = ms / 3600000;
```

The Timestamp column is not reliably ISO. Two malformed shapes are on the live
board right now:

| shape | written by | example |
|---|---|---|
| a human date | `chatgpt-codex-desktop` | `Friday, September 4, 2026 at 2:04 AM EDT` |
| a whole BCB payload | the v1 bus, when it puts the stamp in column A and the payload in column B | `BCB\|v=1\|id=VMC-SCHEMA-PROBE-001...` |

`iso_` hands both back unchanged, and both sort **above** any real ISO stamp —
`'F'` and `'B'` beat `'2'`. So one malformed row inside the 60-row window
becomes "newest", `Date.parse` returns `NaN`, `ageHours` stays `null`, and the
caller reads `null` as healthy:

```js
var quiet = (b.ageHours !== null && b.ageHours > MON_SILENCE_HOURS) ? 'silent' : 'active';
```

The one condition that proves the check is broken was reported as the all-clear.
Those two rows sit 276 and 405 from the end today — outside the window now, and
inside it the day they were written.

## The patch

**1 · newest by parsed time.** `scanBoard_` tracks `newestMs` and only accepts a
row whose stamp parses, counting the rest in a new `res.unreadableTs`. String
comparison is gone.

**2 · three states, not two.** `ageHours === null` becomes `'unreadable'`, and
entering it emails *"SFDC24: the monitor cannot read the board clock"* with the
count of bad cells. Not knowing is reported as not knowing.

**3 · ANDON dedup on identity, not ordering.** `b.andon.ts > st.lastAndonTs` would
re-send an ANDON with an unreadable stamp every fifteen minutes until the daily
cap swallowed it, because such a stamp sorts above every stored ISO value. The
stored key is now `ts :: text.slice(0,120)` compared for equality. The board is
append-only and an older ANDON always leaves the window first, so it cannot come
back and re-trigger.

The log line now says `(NO READABLE TIMESTAMP IN WINDOW)` and the bad-cell count,
so the next person reading an execution log sees it without knowing this story.

## How it was proved

`node tests/test_monitor_silence.js` — no network, board grid and MailApp
stubbed, clock pinned to `2026-09-05T23:40Z`:

```
T1  a plainly quiet board            measured at 21.0h, past the 6h threshold
T2  human-dated row in the window    newest unchanged, age still measured, bad cell counted
T3  BCB payload in the stamp column  newest unchanged, age still measured, bad cell counted
T4  nothing readable at all          age null, emails "cannot read the board clock",
                                     does NOT claim the board is active
T5  the 21h silence                  raises exactly one "board has gone quiet"
T6  an ANDON across three ticks      exactly one email
T7  LEGACY WITNESS                   old scan called the human date "newest", could not
                                     compute an age, and that null read as healthy
```

T2 and T3 use the two malformed strings that are actually on the board, not
invented ones.

## Still open, for whoever has the editor

- Was `monitorTick` still firing through 2026-09-05? Triggers panel should show
  exactly one, error rate `-`.
- Is `MONITOR_ENABLED` still on, and what does `MONITOR_STATE.silence` say? If
  it reads `active` with no malformed row in the window, the cause is elsewhere
  and this patch, while correct, is not the whole answer.

## To deploy, when the release gate opens

```
cd /c/Users/salam/Quantum/Blackboard
node tests/test_monitor_silence.js        # must print VERDICT: PASS
clasp push -f
clasp create-version "monitor: board age by parsed time; unreadable clock alerts"
clasp redeploy <prod-deployment-id> -V <n> -d "..."
clasp list-deployments                    # read-back is the only proof (D-4)
```

The trigger points at `Monitor.js` in the project, so a push plus version is
enough for it; the redeploy is only needed to keep the web app on the same code.
