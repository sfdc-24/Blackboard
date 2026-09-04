# Handover — 2026-09-03

Read this first, then `MEMORY.md` in the memory directory. Written so a
restarted session resumes without re-deriving anything.

---

## Security fix — DEPLOYED AND VERIFIED 2026-09-03

**Version 13 is live.** The four editor utilities that any visitor could call
are now guarded.

`post_reset()` was the serious one: it appends a reset row that blanks the live
site's project pages while forging `by=Governor`. `seed_state()`, `test_chat()`
and `test_read()` were also open.

**How it was verified, not assumed.** An anonymous request to the Governor-only
endpoint returns `{"ok":false,"error":"not authorized"}`, which proves
`whoami_().isGovernor` is `false` for visitors — so `requireGovernor_()` throws
for all four. That is the same check `getState` and `postRow` already relied on
in production, so it permits the owner. No exploitation needed to confirm it.
Reception still serves (200, `google.script.run` present) and the homepage copy
is still live.

**Known automation limit, recorded so nobody retries it:** "New version" in the
Manage-deployments dropdown cannot be selected by automation — confirmed across
two browsers, two viewport sizes, mouse and keyboard. Worse, the Deploy that
completes then silently re-publishes the SAME version while reporting
"Deployment successfully updated". Always read the version number. Mr. Salam
clicked it by hand.

---

## sfdc24.com outage — FIXED AND VERIFIED 2026-09-03

The domain was serving an infinite redirect loop. **It is fixed.**
`http://sfdc24.com` -> 301 -> `https://sfdc24.com/` -> 301 ->
`https://www.sfdc24.com/` -> **200 with the full homepage**, offer section
included. Verified end to end with `curl --resolve`, not assumed.

**Cause, proved not guessed.** NameSilo Domain Forwarding (301 -> www) is fine
and stays ON. The bug was that its parking template put the same three
forwarding A records on **`www` as well as `@`** — and that server 301s
whatever it is asked for to `www`. So `www` redirected to itself, forever:

```
Host: sfdc24.com      -> Location: https://www.sfdc24.com/   correct
Host: www.sfdc24.com  -> Location: https://www.sfdc24.com    the loop
```

**Zone now, do not undo this:** `www` = CNAME -> `ghs.googlehosted.com`.
`@` = the three A records `45.77.75.133`, `45.77.92.157`, `207.246.78.75`
(NameSilo forwarding). **`www` must never carry an A record** — that is the
entire failure mode. TTLs on everything touched dropped 7207 -> 3600.
TXT/MX/DKIM/DMARC untouched; email was never affected.

**If it ever looks broken again, flush DNS first.** The dead records had a 2h
TTL, so `curl` kept reporting the loop from cache long after the zone was
already correct. `nslookup ... 1.1.1.1` showed the truth; `curl --resolve`
bypasses cache entirely. See `docs/ISSUES.md` ISS-006 and ISS-008.

---

## Site work — Sep 3 evening (v16 live)

**Mobile bug FIXED and measured.** Mr. Salam reported on iPhone 13: "the chat
was down to half of the screen and the bottom was blank." Two causes:
`.thread{min-height:210px}` forced a fixed empty block, and the page laid out
top-down inside a fixed-height Sites iframe so leftover frame height rendered as
dead background. Now the document owns the full height of its frame, the shell
is a flex column, and `.thread` takes `flex:1` to absorb slack — so composer and
footer pin to the bottom at any height.

Measured in a local harness at five viewports (the screenshot tool renders at a
fixed size and cannot show a phone layout):

| frame | thread | dead space below footer | h-overflow | textarea | send btn |
|---|---|---|---|---|---|
| 390x844 | 518px | 18px | none | 16px | 44px |
| 390x600 | 274px | 18px | none | 16px | 44px |
| 390x420 | 94px | 18px | none | 16px | 44px |
| 320x568 | 200px | 18px | none | 16px | 44px |

The 18px is the wrap's own padding. **The 16px textarea is not cosmetic** — below
16px iOS Safari zooms on focus and never zooms back. Buttons went 31px -> 44px
(Apple minimum). Same treatment applied to the governor console.

**Links: all clean.** Six nav links and both footer legal links return 200 with
real content. Two greps suggested otherwise; both were false alarms (one matched
Google's own JavaScript, one was grep escaping braces on a binary-detected
file). Check rendered text, not grep counts.

**Security posture held through v16.** All seven non-entry-point exposed
functions are guarded by `requireGovernor_()`, including the two new ones
(`monitorTick`, `setup_monitor`). Anonymous request to the Governor-only
endpoint still returns `{"ok":false,"error":"not authorized"}`.

---

## Monitor — LIVE AND RUNNING (v17)

`monitorTick` is registered as a time-based trigger firing every 15 minutes,
verified in the Triggers panel: **"Showing 1 trigger"**, error rate `-`. Exactly
one, so none of the orphan-duplicate trouble of ISSUE 023.

Registration log:
```
removed 0.0 existing monitorTick trigger(s)
monitorTick triggers now: 1.0   (MUST be exactly 1)
MONITOR_ENABLED = on
alert email will go to: abdus@sfdc24.com
--- running one pass now ---
site: up (www 200 with marker present, apex chain resolves to 200)
board: 820 rows, newest 0.0h ago
Execution completed
```

It watches the public site on **both** hostnames plus a content marker, board
silence past 6h, and ANDON rows. Email fires on **state change only** — a site
still down at 03:00 does not re-email; a recovery does — with a 12/day hard cap
and `MONITOR_ENABLED=off` as an instant kill switch needing no deploy. It runs
on Google infrastructure, so it keeps watching when the laptop lid is shut.

No email was sent on the first pass, which is correct: state moved from
`unknown` to `up`, and unknown is not a change worth alerting on.

`ZSetupMonitor.gs` was deleted after registration; the trigger points at
`Monitor.gs` and is unaffected. v17 is the clean deployed version.

## Google sign-in — NOT DONE, and here is the real blocker

Mr. Salam asked for Google login for other users. It is not a quick change:

1. The script has **no GCP project attached** (`clasp list-apis` -> "GCP project
   ID is not set"). Google Identity Services needs an OAuth client ID, which
   needs a standard Cloud project plus a configured consent screen.
2. Apps Script's own identity is not a shortcut. `executeAs: USER_DEPLOYING`
   does not return an external visitor's email; `executeAs: USER_ACCESSING`
   does, but then the script runs as the visitor and **loses write access to the
   Alpha DB spreadsheet**.
3. GIS One Tap is blocked in cross-origin iframes, and the reception is embedded
   in one from Google Sites. A popup (`ux_mode:'popup'`) flow would be needed.

**Recommended path:** attach a standard GCP project, create a Web OAuth client
with `https://www.sfdc24.com` and the googleusercontent origin authorised, then
add a popup-mode "Sign in with Google" button that posts its ID token to a new
guarded route for server-side verification. Identity must NOT confer authority —
a signed-in visitor is still `instruction_authority=NONE`.

---

## DO NOT BUILD THE UBUNTU PACKAGE — vm-cli leads it

**Mr. Salam assigned this to `vm-cli` on 2026-09-03.** An Ubuntu package that
can be deployed and hosted on a VM Ubuntu instance. My role, in his words, is to
support and keep an eye out — **not** to lead and not to duplicate.

If you are a restarted `claude-code-cli` session reading this: **do not start
designing or building it.** Do not write a spike, a Dockerfile, a systemd unit
or a packaging script "just to help". Check the board for
`id=UBUNTU-LEAD-001` and its `phase=RESULT` replies, then offer support.

**What we legitimately supply** (all posted to the board as UBUNTU-LEAD-001):
Apps Script deploys, since clasp is authenticated on this laptop only and
rollback is one command; the pushed repo; the bus client patterns and the
`.env` contract under D-18; DNS on sfdc24.com if the package needs serving
(read **L-81** first, there is a trap); and any laptop-only credential or
browser step the VM cannot reach.

**Watch, do not pester.** I asked vm-cli for one beacon per milestone —
a single `phase=RESULT id=UBUNTU-LEAD-001` row with a `state=` field. Check the
board for those. As of 2026-09-03 22:01Z **zero board rows mention Ubuntu,
packaging, systemd or .deb**, and `vm-cli` has never written to the board under
that tag — so the workstream is currently invisible to the fleet. That is the
thing to keep an eye on, not the package itself.

---

## Deploys are autonomous now — clasp works (2026-09-03)

Mr. Salam enabled the Apps Script API; `clasp login` completed as
`abdus@sfdc24.com`. **The "New version" dropdown no longer matters.**

```
cd /c/Users/salam/Quantum/Blackboard      # canonical case, see below
clasp push -f
clasp create-version "what changed"
clasp redeploy AKfycbx0D-5DAnMqOm9YbN3iKDwuiBApEi_xex60f6pwdvObEyQBF5jcOK715pl1mN-Nzn6gng -V <n> -d "..."
clasp list-deployments                     # read-back: D-4 still applies
```

That deployment id is the **production web app**. `@HEAD` is a separate dev
deployment. **Rollback is one command:** `clasp redeploy <id> -V 13`.

**clasp must run from `/c/Users/salam/Quantum/Blackboard`.** From the harness
cwd (lowercase `c:\users\salam\...`) every write is refused as *"Content directory is
a symlink. Possible race attack."* Nothing is a symlink — the case difference
alone trips its check, and `--allow-symlinks` does not help. Also: `clasp push`
never deletes remote files; delete them in the editor UI.

Live source is pulled to `gas/`, **gitignored** — it is production code and
carries `ALPHA_ID`. It holds no secrets (`ANTHROPIC_KEY` and the governor
passphrase are Script Properties). `.clasp.json` IS tracked; it only has the
scriptId.

**On a machine that is not this laptop:** `.clasp.json` used to hardcode
`rootDir: "C:/Users/salam/Quantum/Blackboard/gas"`, so every clasp command run
from the repo root died with *"srcDir ... escapes project root"*. It is now the
relative `"gas"`, confirmed working on AkatiaVM by vm-cli. One thing that does
not follow from the fix: `gas/` is gitignored, so a fresh clone has only the
`.gitkeep`. Read commands (`list-deployments`, `clone`) do not care, but
**`clasp push` resolves `rootDir` first and will fail until you pull the source
down** — run `clasp pull` before your first push on a new machine.

---

## Quarantine is LIVE — Version 14, verified 2026-09-03

`logVisitor_` now delegates to `logVisitorQuarantined_`, so all six call sites
reroute at once. Visitor text lands in `PUBLIC_INBOX`, never on the agent board.

**Proved by running `test_quarantine` against the live sheets, not assumed:**

```
board  rows: 728 -> 728   (MUST NOT CHANGE)   PASS
inbox  rows: 1 -> 2       (MUST +1)           PASS
inbox last row: PI-56751A2D | ... | visitor | QUARANTINE ROUTING TEST ... |
                EXTERNAL_UNTRUSTED | NONE | PUBLIC_RECEPTION | UNREVIEWED
VERDICT: PASS - visitor text quarantined, board untouched
```

The test function was deliberately kept out of version 14 and deleted after.
One test row sits in `PUBLIC_INBOX` as the evidence. Reception still serves 200.

---

## Idempotency FIXED — Version 15, verified 2026-09-03

Duplicate Governor events (confirmed 1.7s apart) are gone. `appendRow_` had no
dedup at all — it minted a fresh UUID and appended unconditionally. The
LockService lock only serialised writes; it never stopped repeats.

**The fix, and why it is shaped this way.** A dedup check now runs INSIDE the
script lock, and it reads the sheet, not CacheService. Gemini's finding was
right: CacheService is eventually consistent, so two executions racing can both
`cache.get()`, both miss, and both write. The sheet is the only strongly
consistent record, and read inside the lock it is authoritative.

Two details that matter:
- `SpreadsheetApp.flush()` fires at the top of the critical section. Reads can
  be served from a snapshot taken when the spreadsheet was first touched — and
  `sheet_()` touches it before the lock. Without the flush the scan can miss a
  row committed a second earlier, which is exactly the case being fixed.
- **The read-back moved inside the lock too.** It used to sit outside, so a
  concurrent append between release and read-back made a perfectly good row
  report `MISSING`. That was a real latent bug, unrelated to duplicates.

Match is on payload + `Source_Tag` within `DEDUP_WINDOW_MS` (90s), scanning the
last 40 rows. Source is part of the key on purpose: two agents posting the same
text are two real events. A duplicate returns `{ok:true, duplicate:true}` with
the EXISTING row_id — success, not failure, so a client that retried after a
timeout does not retry again.

**Verified with three runs, not one:**

| scenario | expected | result |
|---|---|---|
| same execution, inside window | deduped, board +1 | PASS |
| **separate execution, inside window** | **both deduped, board +0** | **PASS** |
| after the 90s window expires | writes again, board +1 | PASS |

The second is the one that matters — same-execution dedup would pass even with
a broken design. The third matters just as much: it proves this is a window and
not a permanent block that would silently swallow real repeated events.

Test function kept out of version 15 and deleted after. Reception serves 200,
sfdc24.com serves 200.

---

## What is live right now

| Thing | State |
|---|---|
| Reception prompt | Humour removed, qualify-or-close in. Verified both directions. Now serving under **Version 13**. |
| Homepage copy | **LIVE and verified.** sfdc24.com went from ~0 to **3,502 indexable characters**. |
| Site title | `SFDC24` (was `Home`). Published. |
| Exposed-function guards | **DEPLOYED — carried into Version 15. Verified.** |
| PUBLIC_INBOX quarantine | **LIVE — Version 14, carried into 15. Verified.** |
| Idempotent board writes | **LIVE — Version 15. Verified across executions.** |
| Glasses capture loop | Running (`pythonw`, Startup shortcut). Untouched. |
| Board worker | Not scheduled, not running. Dormant by design. |
| Per-prompt log | Live via Stop hook. |

---

## Verified facts — do not re-derive these

**Rate limits DO exist** (previously flagged unverified):
`CHAT_SESSION_CAP = 12`, `CHAT_DAILY_DEFAULT = 150`, `CHAT_MAX_INPUT = 1000`,
plus `CHAT_ENABLED` as a kill switch. Cost-exhaustion is bounded. Model is
`claude-sonnet-4-5`.

**Clickjacking is confirmed, not suspected** — `setXFrameOptionsMode(ALLOWALL)`
at Code.gs line 58. Required for the Google Sites embed, so a deliberate trade,
but any site can frame the reception.

**The exposed-function audit, complete.** Nine functions in Code.gs are callable
via `google.script.run`. Five are fine — `doGet`, `doPost`, `getState`,
`postRow`, `reception` are entry points or check authorisation internally.
`getState` carries the comment *"scope enforced here, not by obscurity"*, which
is correct. The four unguarded ones were `post_reset`, `seed_state`,
`test_chat`, `test_read`.

**WhatsApp does not deliver marketing templates to US numbers.** Confirmed on
Meta's own docs (updated Jun 17 2026), error `131049`. **Canada is not covered**
— the rule is "+1 with a US area code". Inbound works everywhere. EEA/UK/Japan/
Korea are exempt from the throttle. So WhatsApp is dead for US outbound and fine
for inbound, which is what the intake funnel actually needs.

**Salesforce trademark policy does NOT name "SFDC"** — zero occurrences in their
guidelines PDF. Gemini claimed it did; that was wrong. But the policy bans
**"abbreviations … of any of Salesforce's trademarks"** and any "recognizable
portion" in a **domain name**. Interpretation, not a quoted prohibition.
**Decision: keeping sfdc24.com. Exposure accepted, not resolved.**

**There is no Meta chat model on the Groq account.** The only Meta models
reachable are `llama-prompt-guard-2` (22m/86m), tiny safety classifiers. The
gateway lane labelled "Meta Llama" is `openai/gpt-oss-120b`.

**Six pre-registered trading hypotheses all failed** out-of-sample after costs.
A real result. 19 names are worth 3.6–10.8 *independent* observations, so the
Six Sigma n≈20 rule does not transfer to correlated markets.

**Economics:** overhead is ~42% of job cost, agent tokens ~4%. Utilisation, not
token efficiency, sets price.

---

## The quarantine rewire — one line, do it with the deploy

`scripts/public_inbox_quarantine.gs` is written and **inert**. It was
deliberately not wired because nothing could be deployed or tested from that
session, and rewiring a live logging path you cannot verify or roll back is
reckless.

To apply: paste the file into the project as a new `.gs`, then replace the body
of `logVisitor_` in Code.gs with:

```js
function logVisitor_(sid, who, text) { logVisitorQuarantined_(sid, who, text); }
```

That reroutes all six call sites at once. Then send one reception message and
confirm a row lands in `PUBLIC_INBOX` and **not** on the board.

**Hazard already avoided, do not undo it:** the quarantine sheet's first column
is `Inbox_ID`, **not** `Row_ID`. `sheet_()` finds the operational board by
scanning for a `Row_ID` header, so a second sheet with that header could make it
bind to the wrong one and break every board write.

---

## Open agent conversations — reuse, do not restart

- **ChatGPT** — site review, naming, business models, homepage copy:
  `https://chatgpt.com/c/6a9922ef-5f74-83e9-b99f-02ed4a0fa443`
  *Freezes on long renders. Click the conversation in the sidebar to force a
  re-render; do not fight it with screenshots.*
- **Gemini** — architecture, open-source stack, demand, quarantine/idempotency:
  `https://gemini.google.com/app/258380d5e09ea83c`
- **Meta AI** — WhatsApp platform reality:
  `https://www.meta.ai/prompt/00dad258-484e-458b-8733-254c0d6066af`

**Groq works now** (`scripts/ask_groq.py`). 14 models; best chat model is
`openai/gpt-oss-120b`. Use it for bulk work — free and fast (3.4s for a
1,500-token critique).

---

## Strategy — what the round-table settled

**All four models independently said: abandon ecommerce, fitness and games; go
deeper on Salesforce.** Training/education is the one real bridge (ChatGPT and I
say real, Gemini said no — 2:1).

**Groq broke the plan we were about to build, correctly.** Automating the
diagnostic destroys what made it sellable — it becomes a lead magnet, not a
product. **Give the diagnostic away, charge downstream** for interpretation,
implementation and monitoring. `intake/` needs revising for this.

**New product direction nobody else raised:** make the audit trail *the
product*, not a consulting differentiator — a recurring API for buyers who need
auditability. **But the honest first step is tamper-evident logging.** Groq
claimed the trail has "cryptographic hashes"; it does not. It is a Google Sheet
with UUIDs.

**Groq's own bias, for calibration:** it assumes distribution is free because
production is free. Ten micro-products and "autopilot sales" presume an audience
that does not exist. Selling is the binding constraint, not building.

---

## Still open

1. Quarantine rewire — still to do; one line in `logVisitor_`, needs its own
   deploy (Mr. Salam has to click "New version" by hand).
2. **No proof on the homepage.** Four problem statements, zero evidence. Caps
   conversion until one anonymised engagement exists.
3. Every homepage section is a problem statement — a reader can agree four times
   without learning what they receive. The (now free) diagnostic offer belongs
   on the page.
4. ~~Idempotency~~ — **DONE, Version 15.** See above.
5. Bus secret rotation (deferred by Mr. Salam). `codegs_rotation_window.gs`
   exists to make it zero-downtime.
6. Human gate (`scripts/human_gate.gs`) still not wired.
7. Real numbers for `scripts/quote.py` — rates are marked UNVERIFIED.
8. llm-wiki v0.24.4 is installed and enabled; its commands load on restart.
   `archive`, `checkpoint`, `ingest`, `librarian` are the relevant ones. The
   Obsidian vault is `C:\Users\salam\My Drive\SFDC 24 - Claude` — its 98 files
   are `.gdoc`/`.gsheet` **pointers Obsidian cannot read**, which is why it
   indexes as empty. Real markdown is in `Claude Archive/`.

---

## Tools built

```
trading/     fetch.py, backtest.py, research.py   market data + honest backtesting
intake/      questions.json, README.md            intake funnel and project record
scripts/prompt_log.py       per-prompt time and tokens, real data, Stop hook
scripts/quote.py            job estimator with overhead absorption
scripts/ask_groq.py         free bulk offload
scripts/public_inbox_quarantine.gs   quarantine (inert)
scripts/audit_exposed_functions.gs   REST-API function audit (unused; editor search sufficed)
web/sfdc24-cli.html         CLI-mode site prototype (unpublished)
prompts/consensus-attack.md the prompt that broke our own consensus
```

**Local only, gitignored, keep it that way:** `docs/security-review.md`,
`docs/multi-agent-review-2026-09-03.md`. They map attack paths into a live
system. The security review was force-pushed off the remote at his instruction.

---

## Standing doctrine (also in memory)

- **No Anthropic spend cap.** Declined; never raise it again. No auto-reload is
  the ceiling. Just report if credits run out.
- **Standing permission** to edit the live Apps Script and Google Site without
  asking. DNS is *not* covered — I broke the site with it once.
- **Delegate first** to ChatGPT/Gemini/Groq, have them error-proof each other,
  and **verify every claim against a primary source** before acting. Several
  were wrong today.
- **Batch questions** with time estimates. He is bottlenecked by availability,
  not willingness.

---

## Queue — surface these ONE at a time, never as a list (L-86)

Mr. Salam's doctrine, 2026-09-04: *simple, fluid, specific and guiding*. His
reason for banning lists is mechanical, not stylistic — *"I read first one, then
I may not understand the second, and I may have a question on the third.
Basically I'm already discombobulated by you starting out."*

So his phone shows exactly **one** open item. The rest live here and get
promoted only when the current one closes. Do not put two on the board at once,
and do not re-list these to him.

1. **NOW — send vm-chrome the Governor passphrase.** `SFDC24 - Governor Page
   API` → Project Settings → Script Properties → row `GOVERNOR_PASS`. Out of
   band, not into an agent chat.
2. Did his rotation retire the bus secret vm-chrome still holds? If yes it is
   cut off right now and cannot say so.
3. Look at the new site on the test link, then approve the DNS switch.
4. One anonymised engagement for the homepage. Still the ceiling on the
   business, and still the only thing no agent can do for him.
