# Issues log

Roadblocks that stopped or slowed execution. Opened when I hit something I
cannot solve alone; closed with the fix and how it was verified. Mr. Salam asked
for this on 2026-09-03 so blockers stop being buried in chat.

| # | Opened | Severity | Title | Status |
|---|--------|----------|-------|--------|
| ISS-001 | 2026-09-03 | HIGH | Apps Script "New version" cannot be selected by browser automation | OPEN |
| ISS-002 | 2026-09-03 | MED | Multiple Chrome browsers connected — session blocked until one is chosen | OPEN |
| ISS-003 | 2026-09-03 | MED | No way to reach Mr. Salam for a decision when he is away from the desk | OPEN |
| ISS-004 | 2026-09-03 | LOW | Chrome extension blocks reading Apps Script / long AI chat source via JS | OPEN |
| ISS-005 | 2026-09-03 | LOW | ChatGPT tab renderer freezes on long transcripts | OPEN |

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
