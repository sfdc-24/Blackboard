# `/xray/` — the Black Belt X-Ray console on sfdc24.com

`site/xray/index.html` is a **byte-exact copy of a file built by another surface**,
not source written in this repo. Do not reformat it, do not run a formatter over
it, and do not "fix" its indentation: the whole handoff rests on a sha256 that any
whitespace change destroys. Changes belong upstream with `cowork-chrome`.

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
that is the exact byte string the digest was taken over. `.gitattributes` in this
directory marks it `-text` so `core.autocrlf` cannot rewrite it.

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
git cat-file blob HEAD:site/xray/index.html | sha256sum
# af98f0351d737d1324c90f7ef729ebe2d4afb718025d240ed097c720d5b231d8
```

Hash the **blob**, not the working-tree copy — the checkout re-applies filters
and proves nothing.

## What the page is, verified locally

Served from `site/` over `http://127.0.0.1` and loaded in Chrome:

- Renders correctly in both themes; the in-page "Chalk / Paper" toggle works.
- **Zero console messages** on a fresh load with tracking active — no errors, no warnings.
- **No backend calls.** Zero `fetch`/`XMLHttpRequest`/`WebSocket`/`EventSource`
  and zero `<form>` elements in the file. The `scores.json` loader is an
  `<input type="file">` read locally with `FileReader`; nothing is uploaded.
- **One external origin:** `fonts.googleapis.com` (IBM Plex Sans, IBM Plex Mono,
  Caveat). No other external host, script or stylesheet.
- No secrets, tokens or credentials in the source.

Every claim `cowork-chrome` made about the file in its dispatch checks out.

## Two decisions this page needs before it goes to production

Neither is a defect in the build. Both are publication choices that belong to the
acceptance gate, not to the surface that committed the file.

**1. The page publishes SFDC24's own org telemetry, including a Salesforce login.**
`site/xray/index.html:323` hardcodes:

```js
const LIVE = { org:"abdus.omnistudio@sfdc24.com", date:"2026-09-07",
  activeUsers:10, staleUsers90:9, apexClasses:32, apexTriggers:0,
  permissionSets:38, profiles:46, activePricebooks:1 };
```

rendered in a visible "Live org sample" tile. On a public URL that discloses an
internal Salesforce username and a live inventory of our own org, next to a risk
register naming "Salesforce Health Check score below 80". The page is careful to
label the *scores* as demo data, and the aggregates are genuinely row-level-free —
but the org identity is real and the readout is ours. Ship as-is, anonymise the
tile, or put the page behind an unlisted path: a decision for Mr. Salam and codex.

**2. Indexing.** `/xray/` is deliberately absent from `site/sitemap.xml` and no
`robots.txt` rule was added. That matches `/voice/`, which is also unlisted, so
the page is reachable but not advertised. If `/xray/` is meant to be a public
funnel entry it needs a sitemap entry; if it is meant to be a demo link handed
out deliberately, leaving it unlisted is correct.

## Release state

Per ORDER 044 and the dispatch's own wording — *staging first, codex acceptance
before prod, silence does not promote* — this page is **committed to the repo and
not published**.

There is no staging target for the static site: `sfdc24.com` is served by GitHub
Pages from `sfdc-24/sfdc24-site`, mirrored by hand from `site/` in this repo
(`docs/HANDOVER.md`), and there is no second Pages site to stage into. The local
render above is the closest equivalent and is stated as exactly that, not as a
staging deploy.

**Promoting to production** means copying `site/xray/index.html` into
`sfdc-24/sfdc24-site` at `xray/index.html` and pushing. Do not do it until codex
accepts and decision 1 is answered.
