# Do not run `clasp push` from this repository yet

**Status as of 2026-09-20: the push target is behind the live deployment, and
pushing it would silently remove a model provider from sfdc24.com.**

## What was measured

`.clasp.json` at the repository root:

```json
{ "scriptId": "1lTbqTZ3…", "rootDir": "apps-script/governor-page-api" }
```

So `clasp push` uploads `apps-script/governor-page-api/Code.gs`. That file
contains **zero** references to `XAI_API_KEY`, `api.x.ai` or `askGrok_`.

The live site demonstrably routes to grok. Ten real questions were put through
the live `/exec` endpoint on 2026-09-20 and **every one came back
`by=grok`**, at 13–59 seconds. A deployment cannot answer as grok using a
source that has no grok in it.

There is a second, newer source in the working tree — `gas/Code.js`, which
`.gitignore` deliberately keeps untracked — carrying six references to that
routing, the `agent=` hint handling, and the provider-order comment that
explains the design. It matches live behaviour. It is not in this repository
and this document does not reproduce it.

**Both files open with the identical header, `SFDC24 — site engine (v3 ·
2026-09-02)`, so the version line gives no warning whatsoever.**

## The consequence

`clasp push` would overwrite the live script with a source that cannot route to
grok. The visible result would not be an error. The site would keep answering —
as claude, every time — and the only evidence would be the `by` field on
replies nobody is reading. A provider would be gone and nothing would say so.

## What has to happen before anyone pushes

1. **Establish which script the live `/exec` is.** The homepage calls
   deployment `AKfycbx0D-5DAnMq…`. `.clasp.json` names script
   `1lTbqTZ3…`. A deployment id is not a script id, and nothing in this
   repository maps one to the other. Until that mapping is confirmed, every
   statement about "the deployed source" is a guess.
2. **Reconcile the two sources**, in whichever direction the answer to (1)
   requires — then delete the loser rather than leaving two files that disagree
   and look identical at the top.
3. **Only then** consider whether `gas/Code.js` should be tracked. `.gitignore`
   holds it back "until it has been read and cleared". It *has* now been read
   and cleared: an anchored scan for key shapes returns zero, and every
   credential is fetched from Script Properties at run time. The one literal is
   the Alpha DB spreadsheet id, which is already public in this repository and
   is an accepted risk. **But this repository is PUBLIC**, and committing that
   file publishes the governor authentication logic and the chat cap logic
   along with it. That is a decision for Mr Salam, not a cleanup.

## Why the routing change of 2026-09-20 did not need a deploy

The engine takes an `agent=` hint off the query string and moves the named
provider to the **front** of the list rather than replacing it — deliberately,
so a bad routing guess cannot remove the fallback. Making claude answer first
was therefore one token in `index.html` in the site repository
(`agent:(routed || "claude")`), with grok still behind it.

**Prefer that shape.** A change that can be made on the static side, reviewed in
a PR and reverted with a revert is worth a great deal more than one that needs
an unverified push to a live script.

Related: `docs/PIPEDREAM-WA-PREFIX-GATE.md` records the same class of problem on
the WhatsApp workflow — a live surface whose only source of truth is a builder
UI, with no reviewable copy.
