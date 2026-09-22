# The Apps Script in this repository is the one sfdc24.com runs

**Status as of 2026-09-22: RECONCILED, and pushed for the first time since.**
The live deployment `AKfycbx0D-5DAnMq...` is at **v44**, pushed from a fresh
pull on 2026-09-22 — see *The first push after the reconciliation* below.

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

## The first push after the reconciliation, 2026-09-22

Finding 6a of his 2026-09-20 report — *on a two-option question, name the call
before asking the follow-up* — is a line in `SYSTEM_PROMPT_()`, so it could not
be made on the static side. It is the first change pushed since the
reconciliation, and the procedure below is the one that worked.

```
mkdir C:\Users\salam\Quantum\.gas-pull-20260922      # real path, not a symlink
# .clasp.json with the scriptId and rootDir "."
clasp pull                                           # 7 files
# diff against apps-script/governor-page-api/, edit, node --check
clasp push --force
clasp create-version "<what changed>"
clasp deploy --deploymentId AKfycbx0D-5DAnMq... --versionNumber <n> --description "..."
```

**Edit the pull, not the repo copy, and update the repo from the pushed files
afterwards.** That ordering matters — see the next section.

### The tracked copy and the live script differ on every pull, and it is line endings

The 2026-09-22 pull came back differing from the tracked copy in four of seven
files — `appsscript.json`, `Auth.js`, `Code.js`, `Monitor.js` — with the repo
copy larger every time. That reads exactly like the Sep-21 drift. It is not.

All four are byte-identical once carriage returns are stripped. The working
copy is checked out CRLF; Apps Script serves LF. The size delta is the line
count and nothing else — `Code.js` was +1,049 bytes on 1,049 lines.

**So a plain `cmp` against the working tree will report drift on every future
pull.** Normalise line endings before concluding that production moved:

```
cmp -s <(tr -d '\r' < "$PULL/$f") <(tr -d '\r' < "apps-script/governor-page-api/$f")
```

And push from the pulled directory rather than from the repo, so the live
script is not rewritten wholesale with CRLF as a side effect of a prompt edit.

### `vid` is not ignored — it is the throttle key

It was recorded that the live endpoint ignores a caller-supplied `vid`. That is
true of **attribution**: the visitor identity comes from a server-signed token
(`readSession_`), and a caller-supplied id never reaches the published stats.
It is **not** true of the rate limiter. `Code.js` line 336 takes
`sid = String(p.vid || '').slice(0, 60)`, and the per-session cap is keyed
`rc_ + sid` — so every caller that omits `vid` shares **one** bucket of
`CHAT_SESSION_CAP = 12` replies.

Twelve probes in without a `vid`, the endpoint returns

```
{"ok":true,"reply":"I can't reply live right now...","degraded":"session-cap"}
```

with **HTTP 200** and no `by`. That reads like a provider outage and is not one.
Send a unique `vid` per probe, and read `degraded` before drawing any conclusion
from a fallback reply. Real visitors carry their own `vid`, so probing does not
spend their allowance — but it does count against the whole-site
`CHAT_DAILY_CAP`, default 150 a day.
