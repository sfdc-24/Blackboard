# `/xray/` — the Black Belt X-Ray console, held before release

`docs/evidence/xray-page-v1/index.html` is a **byte-exact copy of a file built by
another surface**, not source written in this repo. Do not reformat it, do not run
a formatter over it, and do not "fix" its indentation: the whole handoff rests on a
sha256 that any whitespace change destroys. Changes belong upstream with
`cowork-chrome`.

**It is not released, and as of 2026-09-08 it should not be.** A confirmed
DOM-injection defect and an information-disclosure question both stand against
this build — see "Four things to settle" below.

## Where it lives, and why not in `site/`

It is held under `docs/evidence/` as an attested artifact pending release.

`site/` in this repo is **not a deployment source**. `docs/HANDOVER.md`
(SITE-MIRROR-DRIFT-001, 2026-09-07) withdrew the old "copy `site/` into the public
repo" instruction: the deployed repository also carries `CNAME`, `.nojekyll`,
`.github/`, `docs/`, `tests/` and `tools/` that this directory does not mirror, so
replacing that tree can remove the custom-domain binding and the publisher's
controls. Putting a page destined for production into `site/` invites exactly that
mistake.

**Releasing it** means working from a current clone of `sfdc-24/sfdc24-site`,
creating a review branch there, adding `xray/index.html` and its `-text`
attribute, running that repository's checks, and merging under review — Pages
deploys from its `main`. A successful push is not a deployment receipt; verify the
live page and the retained public routes in a browser.

## Provenance

| | |
|---|---|
| Built by | `cowork-chrome`, session of 2026-09-08 |
| Dispatched as | board row `COWORK-XRAY-SITEDEPLOY-20260908T042731Z`, 2026-09-08 04:27:30Z, addressed to `vm-claude-code-cli` |
| Source document | Drive doc **`SFDC24 — REPORT · cowork-chrome · xray-page-v1`**, fileId `1uB4ys6UiqxvznIidAw415qq-H-WgUNP4wnA8GeupzaQ` |
| Extraction rule | everything **after** the line `===================== FILE: xray.html =====================` |
| Pinned sha256 | `af98f0351d737d1324c90f7ef729ebe2d4afb718025d240ed097c720d5b231d8` |
| Extracted by | `vm-claude-code-cli` on AkatiaVM (Spot), resourceGroup `COPILOT-DEV-RG`, VM id `e5e4ad7f-537f-4672-a682-ad043e289ae7` |

The committed file is 41,948 bytes, LF-only, and **has no trailing newline** —
that is the exact byte string the digest was taken over. `.gitattributes` beside it
marks it `-text` so `core.autocrlf` cannot rewrite it.

## The trap: two channels return the same document as different bytes

The digest reproduces from **one** channel only.

| Channel | Bytes after the marker | sha256 |
|---|---|---|
| **v1 bus** `action=read&title=...` (`.text`) | 41,768 chars | `af98f035…` **matches** |
| Drive API `files.export` as `text/plain` | 41,816 chars | `ee8f94c7…` does not match |

Same document, same day, a 48-character difference: the Docs plain-text export
adds a BOM and handles leading paragraph breaks differently. Twelve
normalisations of the export (CRLF/LF, trailing newline, BOM stripped, cut at
`</html>`, from-marker vs after-marker) were tried and **none** of them reaches
the pinned value. If you re-verify this file, read the doc **through the bus**.

The general lesson, which is not specific to this page: a sha256 attestation is
only meaningful together with the channel it was computed over. An agent that
pins a digest should say which reader produced the bytes.

## Re-verifying

```bash
git cat-file blob HEAD:docs/evidence/xray-page-v1/index.html | sha256sum
# af98f0351d737d1324c90f7ef729ebe2d4afb718025d240ed097c720d5b231d8
```

Hash the **blob**, not the working-tree copy — the checkout re-applies filters
and proves nothing.

## What the page is, verified locally

Served from a local web root and loaded in Chrome:

- Renders correctly in both themes; the in-page "Chalk / Paper" toggle works.
- **Zero console messages** on a fresh load with tracking active — no errors, no warnings.
- **No backend calls.** Zero `fetch`/`XMLHttpRequest`/`WebSocket`/`EventSource`
  and zero `<form>` elements in the file. The `scores.json` loader is an
  `<input type="file">` read locally with `FileReader`; nothing is uploaded.
- **External origins: two.** The file declares one — `fonts.googleapis.com` — but
  the stylesheet it pulls fetches the font binaries from `fonts.gstatic.com`.
  Both need allowing in any CSP written for this page.
- **No credentials, tokens or API keys.** It does contain a real Salesforce
  username and live org telemetry, which is an information-disclosure question
  rather than a secret-in-source one; see item 2 below.

## Four things to settle before it goes live

Item 1 is a defect and blocks release. Items 2 and 3 are publication choices that
belong to the acceptance gate. Item 4 is a wording defect on a claim about money.
None of them was fixed in place: changing one byte breaks the attestation the
handoff rests on, so all four go back to `cowork-chrome`.

**1. DOM injection from a crafted `scores.json` — blocking.** Raised by GitHub
Copilot's code review on PR #36 and reproduced here. The renderer escapes most
untrusted fields through `esc()` (`index.html:338`) but three are interpolated
raw:

| Line | Interpolation | Context |
|---|---|---|
| 449 | `s${f.severity}` | inside a `class` attribute |
| 507 | `s${f.severity}` | inside a `class` attribute |
| 506, 511 | `${f.sigma}` | element text |
| 511 | `${f.priority}` | element text |

A `severity` value such as `5" onmouseover=…  x="` breaks out of the class
attribute. The blast radius is limited — the page is static, has no cookies,
credentials or backend, and the person choosing the file is the victim — but it is
a real injection on a page we would be inviting prospects to drop a JSON file
into, and "load this scores.json" is a plausible social-engineering shape. The
author clearly knew to escape and missed three fields, so this is an oversight,
not a design decision. Fix upstream: wrap in `esc()`, or coerce `severity`,
`sigma` and `priority` to numbers before interpolating.

**2. The page publishes SFDC24's own org telemetry, including a Salesforce login.**
`index.html:323` hardcodes:

```js
const LIVE = { org:"abdus.omnistudio@sfdc24.com", date:"2026-09-07",
  activeUsers:10, staleUsers90:9, apexClasses:32, apexTriggers:0,
  permissionSets:38, profiles:46, activePricebooks:1 };
```

rendered in a visible "Live org sample" tile, on a page whose risk register names
"Salesforce Health Check score below 80". The page is careful to label the *scores*
as demo data, and the aggregates are genuinely row-level-free — but the org
identity is real and the readout is ours. Ship as-is, anonymise the tile, or put
the page behind an unlisted path: a decision for Mr. Salam and codex. Copilot
raised this independently.

**3. Indexing.** `/xray/` would be absent from the deployed `sitemap.xml` unless
someone adds it, and no `robots.txt` rule is proposed. That matches `/voice/`,
which is also unlisted, so the page would be reachable but not advertised. If
`/xray/` is meant to be a public funnel entry it needs a sitemap entry; if it is
meant to be a demo link handed out deliberately, leaving it unlisted is correct.

**4. The cost meter says "live" and "prepaid", and it is neither.** Raised by the
Codex client-rehearsal task `01a04639` on 2026-09-10 and verified here against the
attested bytes. `index.html:219` carries
`title="Live cost meter — every action priced in pennies against your prepaid scan budget"`,
above a readout of `$0.000 of $25.00`. Nothing behind it is live: the meter is
`const METER={spent:0, budget:25, paused:false}` (`index.html:556`), and every
movement is a hardcoded call in the page's own script — `meterTick(0.011)` for the
scan (line 571), `0.002` per click (line 570), `0.004` per copied work order
(line 543). No price is fetched, nothing is billed, and no prepaid budget exists.

That is a different kind of problem from the demo scores, which the page labels as
demo. This one is a claim about **money**, in a tooltip, on a page meant for
prospects, and the words "live" and "prepaid" are the two that make it a claim
rather than an illustration. Relabelling it is the fix — not removing the meter,
which is a good idea presented honestly once the wording matches.

It is not covered by items 1 to 3 and was not in Copilot's review.

A minor one, also from Copilot: the live-sample tile divides by
`LIVE.activeUsers` without a zero guard. With the value hardcoded to `10` it
cannot fire in this build, so it is a robustness nit for upstream rather than a
release blocker.
