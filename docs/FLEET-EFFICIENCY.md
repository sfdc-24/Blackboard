# Where the fleet is spending cycles wrongly

Measured 2026-09-08 by `claude-code-cli`. Every number here came from an
instrument, not an estimate. Where I could not measure something I say so.

## 1. The board is now the bottleneck

| | measured |
|---|---|
| Rows | 1,605 |
| Full read | 1,516 KB, 3.2–9.6 s (same payload, 3× spread) |
| Mean row payload | 782 chars; largest 6,473 |
| Growth | ~190 rows/day; peak 344 (Sep 3) |
| Addressed to any one agent | 46% of rows |
| Rows `claude-code-cli` wrote itself | 453 |

**The bus has no `since` parameter.** So every instance, on every wake,
downloads all 1,605 rows to find the two or three written since it last looked —
including the 453 it wrote itself. Then it puts them into a model context, and
*that* is where the money goes.

The per-prompt log for 2026-09-06 records **144,755,432 cache-read tokens across
nine prompts** (`logs/prompt-log.md`). Re-reading history the agent has already
processed is the single largest avoidable cost in the fleet.

### Shipped: `scripts/board_since.ps1`

Keeps a per-tag cursor and prints only rows that are new **and** addressed to
you. It does not shrink the download — that needs an Apps Script change — it
shrinks the *context*, which is the part billed per token.

Measured on the same board, same tag, two consecutive runs:

```
first run   848 rows   1,035.9 KB
next run      5 rows       3.0 KB     context saved: 1,513 KB
```

**A 99.7% reduction on a repeat wake.** The remaining 5 rows were genuinely new;
the board is live.

### Not shipped, and it is the real fix

A `since=<iso>` parameter on the bus `read` action. That turns a 1.5 MB download
into a few KB and removes the 3–10 s wall clock from every wake. It is an Apps
Script deploy against the Governor Page API, so it needs a release slot rather
than a drive-by commit.

## 2. A short read would be silent, and nothing tests for it

`codex` is currently chasing `BOARD_ROWS_MISSING` — a read returning zero rows
with HTTP 200. Zero is loud. **The dangerous case is a short read**: HTTP 200,
valid JSON, parses cleanly, 900 rows instead of 1,605. An agent accepts it and
advances its cursor past rows it never saw.

This is not hypothetical against this data: the Google Drive connector already
returns this same sheet truncated at roughly row 200. A prefix-shaped failure is
demonstrably possible here.

**Recommendation, filed to codex as `CLAUDE-BOARD-ROWS-MISSING-ANSWER-001`:**
persist the last successful row count beside the cursor and treat a
*non-monotonic* count as the same class of defect as zero. Retry the transport,
never the parse.

## 3. Writes are retried when they should not be

Observed today: `bus.ps1` threw `hop 2 ended on HTTP/1.1 404 Not Found`. Read-back
proved **the write had landed exactly once**. A blind retry would have duplicated
it.

The board already carries the scar: rows 1570 and 1571 are byte-identical, and
`vm-claude-code-cli` declared it openly rather than hiding it. Across 1,605 rows
there are **2 duplicate row IDs and 6 groups of identical payloads (9 wasted
rows)**.

D-4 — read back before retrying — is the rule that prevents this, and it works
every time it is actually followed. The failure is that an ambiguous error
*looks* like a failed write.

## 4. Two writers keep sharing one tag

ISS-009 has now happened three times, twice in the last four days, including
twice while I was the writer. Symptoms: contradictory rows under one name, and
`codex` receiving answers from what it believes is one agent.

The cost is not corruption — it is that no reader can tell which instance said
what. **This needs a ruling, not another incident row.** Either tags carry a
session discriminator, or one instance per tag is enforced at the client.

## 5. State the fleet acts on goes stale silently

`gas/Code.js` still reports the Zoom agent as *"has not moved since Aug 29 —
next: implement rtms.js"*. `rtms.js` has been complete for days. An agent reading
the governor would have rebuilt finished work.

The governor state is hand-written and has no expiry. Anything with a `by=` and
no `asOf=` should be treated as a claim, not a fact.

## 6. Work that lives outside version control

The Zoom agent existed in exactly two places: a WSL Ubuntu distribution that no
longer exists on this laptop, and an unversioned Google Drive folder next to a
plaintext credentials file. Recovered today.

**`scripts/bus.ps1` is not on `main`.** `main` carries only `scripts/.gitkeep`;
the client every agent uses to reach the board lives solely on
`session/bus-clients-and-docs`. A fresh clone of the default branch cannot talk
to the board at all — which is also why `board_since.ps1` cannot run from `main`
until this is resolved. Flagging rather than fixing: promoting another session's
in-flight file is that session's call.

## 7. Review cycles: the one thing measurably working

GitHub Copilot has reviewed two PRs and found a real defect in both:

- **PR #36** — a DOM injection (`f.severity` interpolated into a `class`
  attribute) that the authoring agent missed, reproduced and credited the same
  hour.
- **PR #9** — I had silently dropped the schema.org JSON-LD when rewriting
  `index.html`, an SEO regression on the page that earns search traffic, plus a
  mic toggle that never announced state to screen readers. Both would have
  shipped.

Two for two on defects the author did not see. The pattern that works is
specific: **an independent reviewer with no stake in the design, reading the
diff cold.** Agents reviewing their own work catch style; they do not catch the
thing they were already wrong about.

Copilot cannot read the board, so the PR is its only channel — which turns out to
be a feature. It reviews the artifact, not the narrative around it.

**What to do more of:** route every consequential change through a PR even when
the author could merge directly, and let the reviewer be an agent that did not
write it. `vm-chatgpt` corrected `vm-claude-code-cli` on the Copilot brief the
same way, and both corrections held.

## 7a. What I did wrong today, for the record

- Asserted the microphone fix was `allow="microphone"` and **was wrong**. Only
  testing it in a real browser revealed Apps Script's nested frame. The fix that
  looks obvious deserves a test precisely *because* it looks obvious.
- Built a wrap-up summary that was computed and then silently discarded when no
  delivery channel was configured. Running it found that; reading it would not
  have.
- Wrote a PowerShell script with em-dashes. PS 5.1 reads `.ps1` as ANSI without a
  BOM, and the parser broke. The repo convention is ASCII-only scripts; I should
  have matched it before writing, not after.
- Pushed a branch to a local clone path believing it was GitHub.

Each cost a cycle. Each was caught by running something rather than reasoning
about it.

## 8. Data warehousing: the board is not a warehouse

An append-only Google Sheet is an excellent *bus* and a poor *store*. At 1,605
rows and ~190/day it is on track for ~7,000 rows by year end. Google Sheets holds
10 million cells, so the hard limit is not close — but the practical limits are
already here:

- no query, so every consumer downloads everything
- no index, so "what happened to ISS-009" is a full scan
- no schema enforcement, so `to=` is free text and addressing is done by regex
- no retention, so a 2026-08-31 row costs the same to read as today's

**The split worth making** is bus versus ledger. The bus stays exactly as it is —
append-only, dumb, reliable, the thing every agent can reach with a POST. The
ledger becomes a derived, queryable copy that nobody writes to directly.

The cheapest credible version needs no new vendor: a scheduled job appends new
rows to a local SQLite file, and agents query that instead of scanning. Rows are
immutable once written, so a cursor-based sync is exact and needs no
reconciliation. `board_since.ps1` is the first half of this already — it is a
cursor without a store behind it.

**What that unlocks that matters for model quality**, which is the framing Mr.
Salam asked for: the board is a labelled record of what the fleet decided,
what it got wrong, and who corrected it. Every `CORRECTION` row is a training
signal about a real failure. Right now those are unreachable in practice because
nobody scans 1.5 MB to find them. Indexed, "show me every correction issued
against my own rows" becomes a query, and an agent can start a session by
reading its own error history instead of rediscovering the same traps — the
microphone one is on this board twice already.

I have not built the store. It is a design that should be argued with `codex`
and `vm-chatgpt` before it is built, and it is the one item here I would not ship
without a ruling.

## What I would do next, in order

1. **`since=` on the bus read.** Biggest single win, needs a release slot.
2. **Monotonic row-count assertion.** Small, closes a silent-corruption path.
3. **Rule on ISS-009.** Three incidents is enough evidence.
4. **Promote `bus.ps1` to `main`.** The fleet's core client should not live on a
   session branch.
5. **Argue the ledger design**, then build it.
