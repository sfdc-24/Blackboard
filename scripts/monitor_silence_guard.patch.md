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

---

# ADDENDUM — 2026-09-06 1:30 PM EDT · the watchdog was MUTE, and now is not

Written by claude-code-cli after the fix above went live. The fail-open scan was
real and is fixed, but it was **not** why Mr. Salam never got an alert. This is.

## What was actually wrong

The Apps Script execution log for the 1:02:59 PM `monitorTick` run, verbatim:

```
MONITOR: email failed: Exception: You do not have permission to call
MailApp.sendEmail. Required permissions:
https://www.googleapis.com/auth/script.send_mail
```

**Every alert the monitor has ever raised failed silently.** `monitorNotify_`
catches the exception and returns false, so nothing surfaced anywhere. The
handover's line *"alert email will go to: abdus@sfdc24.com"* was never true.

**Proved it was the SCRIPT's grant, not the trigger's**, by running `monitorTick`
by hand from the editor at 1:16:05 PM and watching it fail identically. That
ruled out "just recreate the trigger", which was the cheaper hypothesis.

**Cause, read from his Google account rather than guessed.** The project's
granted scopes were: profile info, Sheets, run-when-you-are-not-present, connect
to an external service — **granted September 3 at 2:36 AM**, before `Monitor.js`
introduced `MailApp`. `appsscript.json` declared no `oauthScopes`, so the set was
inferred once at first authorisation and never revisited. The editor does not
re-prompt on its own.

## The fix

`appsscript.json` now declares `oauthScopes` explicitly: those four, plus
`script.send_mail`. **A strict superset** — the list was derived by enumerating
every Google service the code actually calls, and cross-checked against the
granted list above, because an explicit list REPLACES inference and one missing
scope would break the live web app. (`DriveApp` is not used; the `Drive` matches
in `Code.js` are comment text.)

`clasp push -f`, then the editor immediately said **"Authorization required"** —
which is the proof the manifest was the missing piece. Mr. Salam clicked Allow.

**Verified, not assumed:**

```
1:26:25 PM  MONITOR: emailed abdus@sfdc24.com - SFDC24 ANDON raised by claude-code-cli
```

and the message is in his inbox: subject *SFDC24 ANDON raised by claude-code-cli*,
17:26:25Z. Site re-checked immediately after: `www` 200, `/voice/` 200, apex 200.
The public web app serves **Version 30**, which carries its own manifest
snapshot, so pushing to Head never put visitors at risk — only the trigger runs
at Head.

## Second defect, found while fixing the first

Every state assignment advanced **whether or not the alert was delivered**:
`st.lastAndonTs`, `st.silence`, `st.www`, `st.apex`. So a failed alert was
recorded as sent and never retried — which is exactly how a three-day mail
outage stayed invisible. All four now advance only on a successful send;
undelivered means unchanged, so the next tick tries again. Retries stay bounded
by `MON_MAX_EMAILS_PER_DAY = 12`.

Strike counters (`wwwStrikes`, `apexStrikes`) still advance regardless — those
are measurement, and only notification state is allowed to lag a failed send.

Covered by `tests/test_monitor_silence.js` T8–T10, which make `MailApp` throw the
real exception string: an undelivered alert does not advance state, it is retried
once mail works, and a delivered one is not repeated.

## Still open

`MONITOR_STATE` currently reads `silence:"active"` with a healthy board, so the
history of that field before today cannot be recovered — the fail-open scan and
the mute mailer were both live at once, and either alone would have produced the
same silence. Both are fixed; neither can be blamed retrospectively.
