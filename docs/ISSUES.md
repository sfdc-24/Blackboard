# Issues log

Roadblocks that stopped or slowed execution. Opened when I hit something I
cannot solve alone; closed with the fix and how it was verified. Mr. Salam asked
for this on 2026-09-03 so blockers stop being buried in chat.

| # | Opened | Severity | Title | Status |
|---|--------|----------|-------|--------|
| ISS-001 | 2026-09-03 | HIGH | Apps Script "New version" cannot be selected by browser automation | **RESOLVED** |
| ISS-002 | 2026-09-03 | MED | Multiple Chrome browsers connected — session blocked until one is chosen | **RESOLVED** |
| ISS-003 | 2026-09-03 | MED | No way to reach Mr. Salam for a decision when he is away from the desk | OPEN |
| ISS-004 | 2026-09-03 | LOW | Chrome extension blocks reading Apps Script / long AI chat source via JS | OPEN |
| ISS-005 | 2026-09-03 | LOW | ChatGPT tab renderer freezes on long transcripts | OPEN |
| ISS-006 | 2026-09-03 | **CRITICAL** | sfdc24.com DNS moved off Google to Vultr — site serving a redirect loop | **RESOLVED** |
| ISS-007 | 2026-09-03 | MED | Google Sites iframe steals keyboard focus mid-typing | OPEN |
| ISS-008 | 2026-09-03 | MED | NameSilo DNS form fails silently — blank hostname, native select ignores clicks | **RESOLVED** |
| ISS-011 | 2026-09-03 | MED | Monitor false-positived within 13 min of going live | **RESOLVED** |
| ISS-009 | 2026-09-03 | **HIGH** | Tag collision — three writers share `claude-code-cli`; caused the outage | OPEN |

---

## ISS-001 · Apps Script "New version" cannot be selected by automation — HIGH

**Symptom.** In `SFDC24 - Governor Page API`, Deploy → Manage deployments →
pencil → Version dropdown opens and lists "New version" correctly. Clicking it
closes the list and leaves the previous version selected. Reproduced across two
Chrome browsers, two viewport sizes, several click x-positions, and keyboard
navigation.

**Why it matters.** Saving code is not deploying it. Every fix I write sits
inert until a human clicks that one option. It stopped a live security fix
(`post_reset()` callable by any visitor) for an hour.

**Worse.** The Deploy that then completes silently re-publishes the SAME version
while reporting "Deployment successfully updated". That message is not proof —
only the version number is.

**Candidate fix: `clasp`.** Google's official Apps Script CLI. `clasp deploy`
creates a new version and deploys it with no browser at all. Needs a one-time
`clasp login` (browser OAuth) and the Apps Script API enabled for the account.
This is the real unblock and is being attempted.

**Fallback.** A different automation stack (Playwright via the
`browser-automation` skill) dispatches real trusted events and may select the
option where the extension cannot.

---

## ISS-002 · Multiple Chrome browsers connected — MED

**Symptom.** A second Chrome connected mid-session. Every browser action then
failed with "Multiple Chrome browsers are connected and none has been selected",
and the tool requires asking Mr. Salam to pick rather than choosing myself.
Selecting one also destroyed the session's tab group, losing all tab IDs.

**Mitigation being built.** `list_connected_browsers` reports which is which
before acting, so a stale session can re-select without a question. If names are
ambiguous, the Glasses Intake capture frames show the actual screen and can
identify which window holds the working tabs.

---

## ISS-003 · No channel to Mr. Salam when away from desk — MED

**Symptom.** Work stops on any decision that needs him. WhatsApp is unusable for
outbound to US numbers (verified) and I must not send messages on his behalf
unprompted anyway.

**Fix being built.** A mobile-first `?view=blockers` page on sfdc24.com showing
open blockers and letting him answer from a phone. His answer lands on the board
and I pick it up. See `docs/blockers-page.md`.

---

## ISS-004 · Extension blocks source reads via JS — LOW

`javascript_tool` returns `[BLOCKED: Cookie/query string data]` for the Apps
Script editor and for AI chat pages containing code with URLs. **Workaround that
works:** screenshots are not blocked, and Monaco's Ctrl+F with regex gives exact
match counts. Audit by searching, not extracting.

---

## ISS-005 · ChatGPT renderer freezes on long transcripts — LOW

Screenshots time out and `get_page_text` returns ~99 chars. **Workaround:** click
the conversation in the sidebar to force a re-render. Cheaper still: ask it to
restate the answer briefly rather than extracting the long one.


---

## ISS-006 · sfdc24.com is down — DNS moved off Google — CRITICAL

**Found** 2026-09-03 ~16:40Z while verifying a homepage edit. The verification
caught it; nothing else would have.

**Symptom.** `https://www.sfdc24.com` returns `301 Moved Permanently` from
**nginx**, redirecting to itself — an infinite loop. The site is unreachable.

**Diagnosis.** Both apex and www now resolve to `45.77.75.133`,
`45.77.92.157`, `207.246.78.75`. Reverse DNS on those is
**vultrusercontent.com**. `www` was a CNAME to `ghs.googlehosted.com` earlier
today; it is now an A record set pointing at Vultr.

**Everything else is healthy** — this is only the domain pointer:

| | |
|---|---|
| Google Sites content | 200, 934 KB |
| Reception app | 200, 25 KB |
| Email (MX) | intact, `SMTP.GOOGLE.com` |

**Almost certainly NameSilo's parking/forwarding layer.** The identical failure
happened on 2026-09-02: re-saving NameSilo forwarding injected A records for
both `@` and `www`, overriding the `www` CNAME. Note the apex now resolves,
where it was NXDOMAIN a few hours ago — consistent with forwarding being
(re)enabled.

**Not caused by this session.** No DNS was touched. Work this session was Google
Sites content and Apps Script only, neither of which can alter DNS records.

**The fix, which worked last time.** In NameSilo: delete the A records for `@`
and `www`, re-add `www` as a CNAME to `ghs.googlehosted.com`. Verify with
`Server: ESF` in the response headers.

**Why I did not just do it.** DNS is explicitly outside standing permission —
Mr. Salam granted Apps Script and Google Sites only — and I broke this exact
thing on 2026-09-02 by touching NameSilo forwarding. Raised instead as BLK-005
on the blockers page with a one-tap "fix it now", which is the case that page
was built for.

---

## ISS-007 · Google Sites iframe steals keyboard focus mid-typing — MED

**Symptom.** While typing into a Sites text box on a page that embeds the
reception app, focus jumps into the reception's chat input part-way through.
Text intended for the page lands in the live chat, and pressing Enter **sends a
real message**, spending tokens and creating junk rows.

**Cause.** The reception page focuses its input on any keypress — the "type
anywhere" affordance that makes the terminal UI feel right. In an editor iframe
it is a hazard.

**Happened twice**, costing a partial publish and two junk visitor messages.

**Workaround that works.** Re-click into the text box before EVERY chunk and
screenshot after each. Do not type more than one paragraph per click. Verified:
three paragraphs typed this way all landed correctly.

**Real fix, when reception is next deployed.** Gate the focus-steal on
`window.self === window.top` so it never fires inside an iframe. One line, and it
also stops the same thing happening to a real visitor embedding the page.

---

## ISS-006 · RESOLVED 2026-09-03 — sfdc24.com redirect loop

**Root cause, found and proved.** NameSilo **Domain Forwarding was ON** with a
301 to `https://www.sfdc24.com`. Its parking template had put the same three
forwarding A records (`45.77.75.133`, `45.77.92.157`, `207.246.78.75`) on
**both `@` and `www`**. The forwarding server 301s *everything it is asked for*
to `www`. So `www` pointed at a server whose only job was to redirect to `www`.

Proved directly rather than inferred, by pinning the Host header to the
forwarding IP:

```
Host: sfdc24.com      -> 301 Location: https://www.sfdc24.com/   correct
Host: www.sfdc24.com  -> 301 Location: https://www.sfdc24.com    ITSELF - the loop
```

The forwarding was never wrong. Having `www` among the records it answers for
is what was wrong.

**Fix applied.** Deleted all six A records. Re-added `www CNAME
ghs.googlehosted.com` (Google Sites) and the three forwarding A records on
**`@` only**. TTL dropped 7207 -> 3600 on everything touched. Domain Forwarding
left ON and untouched — it is doing exactly the right job for the apex, which
Google Sites cannot serve natively.

**Final zone:** `www` CNAME -> Google. `@` A -> NameSilo forwarding -> 301 ->
`https://www.sfdc24.com`. `www` carries no A record, so the loop cannot recur.
All TXT / MX / DKIM / DMARC untouched throughout; email was never at risk.

**Verified.** `www.sfdc24.com` returns **200 from Google** with the full
homepage including the new offer section ("first piece of work looks like",
"short document, not a slide deck").

**Cost of the stale TTL.** The old records carried TTL 7207 (~2h), so resolvers
and the local OS cache kept serving the dead nginx for a while after the zone
was already correct. `curl` reported the loop from cache while `nslookup`
against 1.1.1.1 already showed Google. **Flush before concluding a DNS fix
failed** — and test with `curl --resolve` to bypass cache entirely.

**The diagnostic that actually settled it:** `Get-DnsClientCache -Name
'*sfdc24*'` listed the stale `www -> 45.77.x` A records still sitting in the
Windows resolver cache while `nslookup` against 1.1.1.1 returned Google. Adding
`-w '%{remote_ip}'` to curl showed it connecting to `207.246.78.75`, proving the
loop was local, not live. Flushing with `Clear-DnsClientCache` gave 200 on both
hostnames immediately. The cache repopulated stale once *after* an earlier
flush, so a single passing test is not proof — check `remote_ip`.

---

## ISS-008 · RESOLVED — NameSilo DNS form fails silently — MED

Three separate traps in one form, each of which cost a wasted cycle:

**1. The hostname field must be `@` for the apex, not blank.** Blank is
rejected — but on the first two submits the dialog simply closed and the record
was never created, with no error shown. Only the third attempt surfaced "Should
not be empty". **Two records I believed were saved did not exist.**

**2. The native `<select>` for record Type ignores synthetic clicks on its
options** — the same failure as ISS-001. It opens and lists correctly, the
click closes it, the old value stays. **What works: click the select to open
it, then Down / Down / Enter.** Worth retrying on ISS-001 with this technique.

**3. `ctrl+a` inserts a literal `a`** into these inputs instead of selecting
all — `ghs.googlehosted.com` was submitted as `aghs.googlehosted.com` and had
to be caught by eye. Use triple-click then `Delete`.

**The real lesson is the verification method, not the form.** Screenshots
cannot show a 16-row record list, so "the dialog closed" got read as success
three times. **`get_page_text` returns the entire NameSilo record table as
plain text** and settled it in one call. On any list-shaped page, read the text,
do not photograph it.

---

## ISS-002 · RESOLVED — it was not two Chromes

I was driving **Microsoft Edge**, not Chrome. It surfaced only when NameSilo's
device check flagged an unrecognised browser. The "two browsers" in the picker
are two different applications, not two profiles.

**Rule: check which browser is selected before blaming a site.** Where a login
or device trust matters, use Browser 2 (Chrome), which holds the Google session.

---

## ISS-001 · RESOLVED 2026-09-03 — deploys are autonomous now

**clasp is authenticated and working.** Mr. Salam turned on the Apps Script API;
`clasp login` completed as `abdus@sfdc24.com`. The dropdown that defeated six
browser attempts is now irrelevant — the whole deploy runs from the shell:

```
clasp push -f
clasp create-version "description"          -> "Created version 14"
clasp redeploy <deploymentId> -V 14 -d "..."
clasp list-deployments                       -> read-back proof
```

The production web app is deployment
`AKfycbx0D-5DAnMqOm9YbN3iKDwuiBApEi_xex60f6pwdvObEyQBF5jcOK715pl1mN-Nzn6gng`.
`@HEAD` is the separate dev deployment. **Rollback is now one command** —
`clasp redeploy <id> -V 13` — which is what made the quarantine safe to ship.

**Two Windows traps, both real:**

1. **clasp must run from `/c/Users/salam/Quantum/Blackboard`, the canonical-case
   path.** From the harness cwd (`c:\users\salam\quantum\blackboard`) every
   write is refused with *"Security Error: Content directory is a symlink.
   Possible race attack."* Nothing is a symlink — `os.path.islink` is false the
   whole way up. clasp compares the given path against the resolved one and the
   case difference alone trips its race check. `--allow-symlinks` does **not**
   fix it; only running from the canonical path does.
2. `clasp push` **never deletes remote files.** A file removed locally comes
   straight back on the next pull. Delete it in the editor UI.

**The editor's function picker is still broken, and no longer matters.** It was
retried once with the technique that works on native `<select>` elements
(click to open, then Down / Enter) and on a direct click on the option. Both
failed, on a second instance of the same Material listbox — so this is the
component, not that one dropdown.

**Workaround when a function must be run from the editor: give it its own
file.** The picker always pre-selects the first function in the open file, so a
file containing exactly one function needs no click at all. That is how
`test_quarantine` was run. Verified.

---

## ISS-009 · Tag collision — three writers share `claude-code-cli` — HIGH

**This caused the Sep 3 outage, and my earlier RCA was incomplete.** I reported
the loop as leftover NameSilo parking from Sep 2. It was not. It was created at
roughly 11:57 AM EDT **that same day, by another instance writing under my own
tag**, and I had no way to see it.

**The real sequence, reconciled from the board:**

1. Sep 2 DNS edits auto-removed NameSilo Domain Forwarding, so the apex went
   dark. (Board row 15:24:55Z — `ANDON` — apex A record GONE, www healthy.)
2. ~11:57 AM EDT Sep 3 an instance tagged `claude-code-cli` re-enabled Domain
   Forwarding to restore the apex, and filed **L-77**. (Board row 15:57:20Z —
   `ANDON-CLEAR`.)
3. Re-enabling forwarding made NameSilo **install its forwarder A records on
   `www` as well as the apex, silently replacing the `www` CNAME**. The
   ANDON-CLEAR line "www CNAME untouched throughout" was wrong.
4. That forwarder 301s every host it answers for to `www`, so `www` began
   redirecting to itself and the whole site went down — which is what I found
   at ~1:20 PM and fixed by splitting the records.

**So both RCAs were half right.** L-77 correctly identified that forwarding gets
auto-removed; mine correctly identified the self-loop. Neither saw the join:
**re-enabling forwarding destroys the `www` CNAME.** Filed as **L-81**, with
**L-78**'s dismissal of L-77 explicitly withdrawn.

**Why it happened.** At least three writers share the tag `claude-code-cli`:
this laptop session, the glasses capture loop posting `ASSET` rows every five
minutes, and whoever filed the noon ANDON-CLEAR. The check-in register exists to
prevent exactly this — one instance sees another holding a file and waits — and
it cannot work when three writers answer to one name. `REQ-R6WNT2` already says
a tag is a claimed identity, not a machine; nothing enforces it.

**Raised to Mr. Salam as BLK-008** with a concrete split: laptop keeps
`claude-code-cli`, the VM takes `vm-cli`, the glasses loop takes
`glasses-uploader`. Tag assignment is his call, not mine to take unilaterally.

**Mitigation already shipped:** `DNS-COORD-001` is on the board addressed to
ALL, carrying the trap, the correct end state, and the verification chain, so
the next instance to touch this domain cannot repeat it by accident.

---

## ISS-010 · Bus writes return redirect artifacts that look like failures — MED

Twice in ten minutes a bus append returned either `hop 2 redirected again --
one-shot key consumed or expired` or a Google Drive "Page Not Found" HTML body.
**Both writes had landed intact**, full payload, exactly once. A blind retry
would have created duplicates — and the v15 idempotency fix does **not** protect
these, because the bus is a different Apps Script with no dedup of its own.

Reads degrade the same way under rapid use; a ~45s backoff cleared it, which
points at rate limiting rather than a broken key.

**Rule, filed as L-80:** always read back before retrying any bus write, and
never assume the bus protects you the way the Governor API now does.

---

## ISS-011 · RESOLVED — the monitor cried wolf 13 minutes after going live

**What happened.** `monitorTick` was registered at 21:35 and reported the site
`up`. Its first scheduled run at 21:47 flipped `MONITOR_STATE` to
`{"site":"down"}` while the site was demonstrably serving 200 on both
hostnames with the homepage marker present, verified from the laptop with two
different User-Agents including a Google one.

**No alert reached Mr. Salam** — `mailCount` was 0 — but that was luck, not
design. The very next state change would have emailed a false outage.

**Two faults, both mine:**

1. **One boolean for two hostnames.** `checkSite_` returned a single `ok`, so a
   failure on the bare-domain leg condemned the whole site even though `www` —
   where visitors actually land — was perfect. The apex rides NameSilo's Caddy
   fleet with on-demand TLS, and I had already seen one transient handshake
   failure from the laptop that hour (`schannel: SEC_E_INTERNAL_ERROR`), which
   is almost certainly what it hit.
2. **No debounce.** A single sample could flip the state. On a 15-minute cadence
   one blip is not an outage.

**Fix (v18).** `checkSite_` now returns `wwwOk` and `apexOk` independently and a
detail string naming each. State flips only after `MON_FAIL_STRIKES = 2`
consecutive agreeing checks. `www` down is a full alert; the bare domain failing
while `www` is healthy gets a *different*, calmer message that says so, because
the site is up for anyone typing the full address. Old single-field state
migrates to `unknown` rather than inheriting a verdict the old logic reached for
different reasons.

**Verified after the fix:**
`site: www=up apex=up (www 200 with marker | apex chain 200)`

**The lesson worth keeping:** a monitor that cries wolf gets ignored exactly
when it is finally right. Alerting logic needs the same read-back discipline
(D-4) as writes — I declared this done before it had survived a single
unattended cycle.
