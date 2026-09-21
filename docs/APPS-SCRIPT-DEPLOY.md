# The Apps Script in this repository is the one sfdc24.com runs

**Status as of 2026-09-21: RECONCILED.** `apps-script/governor-page-api/` now
holds a verbatim `clasp pull` of the live script, so `clasp push` is a no-op
rather than a disaster. What follows is what it was, because the shape of the
mistake is worth keeping.

## What it was, and it was not hypothetical

`.clasp.json` names script `1lTbqTZ3...` with `rootDir:
apps-script/governor-page-api`. Mr Salam confirmed on 2026-09-21 that the live
`/exec` the homepage calls - deployment `AKfycbx0D-5DAnMq...` - belongs to
**that exact script**, named *SFDC24 - Governor Page API*.

So the push target was the live site's Apps Script, and every one of its seven
files disagreed with what was deployed:

| file | was in the repo | live | delta |
|---|---|---|---|
| `Code` | 55,247 | 58,181 | **-2,934, and no grok routing at all** |
| `Auth` | 16,982 | 12,683 | +4,299 |
| `Index.html` | 33,284 | 36,591 | -3,307 |
| `Reception.html` | 23,642 | 19,908 | +3,734 |
| `Monitor` | 14,965 | 14,646 | +319 |
| `appsscript.json` | 586 | 567 | +19 |
| `PublicInbox` | 7,443 | 7,443 | same size, still not identical |

Not "slightly behind" - a different generation, differing in **both
directions** across six of seven files. A `clasp push` would have replaced the
live reception page, the governor auth, the monitor and the chat engine
wholesale, and removed a model provider on the way past.

**And nothing warned you.** The old `Code.gs` and the live `Code.js` both open
with the identical line, `SFDC24 - site engine (v3 . 2026-09-02)`. A header
match is not evidence of sameness, and it is the check a person reaches for
first.

## How it was proved, rather than argued

1. Ten real questions through the live `/exec` on 2026-09-20 all returned
   `by=grok`. A deployment cannot answer as grok from a source with no grok in
   it.
2. `clasp pull` into a scratch directory - never over the repo - returned seven
   files, and its `Code.js` is **byte-identical** to the untracked `gas/Code.js`
   that had been sitting in one laptop's working tree. That file was the only
   faithful copy of production anywhere.
3. An anchored key-shape scan across all seven returns zero. Every credential
   comes from Script Properties at run time; the only literals are the Alpha DB
   id and the `/exec` URL, both already public here.

**`clasp pull` refuses a symlinked path** - *"Security Error: Content directory
is a symlink."* Run it from a real canonical-case directory, not a temp path.

## The rule that stays

1. **Pull before you push, every time.** This directory mirrors a live system
   that can be edited in a browser by someone who never touches git. If
   `clasp pull` produces a diff, production moved without the repository, and
   that diff is the news - read it before overwriting it.
2. **Push the whole project or none of it.** `clasp push` sends the rootDir as
   the entire script. A rootDir holding fewer files than the live project
   deletes the difference - here that would have been the reception page, the
   governor auth and the monitor.
3. **Never leave `Auth.gs` beside `Auth.js`.** Two copies of one server file in
   one rootDir, and the older can win.
4. **`gas/` is redundant now** and stays gitignored. These seven files are the
   tracked copy, and they were already tracked and public before this
   reconciliation - what changed is that they are now true.

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
