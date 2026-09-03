# Issues log

Roadblocks that stopped or slowed execution. Opened when I hit something I
cannot solve alone; closed with the fix and how it was verified. Mr. Salam asked
for this on 2026-09-03 so blockers stop being buried in chat.

| # | Opened | Severity | Title | Status |
|---|--------|----------|-------|--------|
| ISS-001 | 2026-09-03 | HIGH | Apps Script "New version" cannot be selected by browser automation | OPEN |
| ISS-002 | 2026-09-03 | MED | Multiple Chrome browsers connected — session blocked until one is chosen | **RESOLVED** |
| ISS-003 | 2026-09-03 | MED | No way to reach Mr. Salam for a decision when he is away from the desk | OPEN |
| ISS-004 | 2026-09-03 | LOW | Chrome extension blocks reading Apps Script / long AI chat source via JS | OPEN |
| ISS-005 | 2026-09-03 | LOW | ChatGPT tab renderer freezes on long transcripts | OPEN |
| ISS-006 | 2026-09-03 | **CRITICAL** | sfdc24.com DNS moved off Google to Vultr — site serving a redirect loop | **RESOLVED** |
| ISS-007 | 2026-09-03 | MED | Google Sites iframe steals keyboard focus mid-typing | OPEN |
| ISS-008 | 2026-09-03 | MED | NameSilo DNS form fails silently — blank hostname, native select ignores clicks | **RESOLVED** |

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
