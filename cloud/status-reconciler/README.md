# status-reconciler (R-STATUS)

Spec: `docs/BOARD-STATUS-FRAMEWORK.md`. Rulings referenced throughout this branch's
commits: blackboard-5e, 2026-11-14 (gateway actions, not a Sheets service account;
HELD is the row's own status, not a link to another row); Gemini, 2026-11-14
(ASK_PHASES includes REVIEW_REQUEST).

**What it is**: a Cloud Run job that reads the whole board, computes the six
Status columns (M to R) for every row with `scripts/rstatus_reconcile.py`'s pure
state machine, and writes only what changed through `scripts/status_client.py` -
the gas-bus gateway's `status_write` action, which enforces the M:R-only boundary
on its own side. It never touches columns A to L. It runs every 5 minutes.

**What it does NOT do, on this branch**: deploy, post any board row (the
`newly_expired()` escalation list and `holds_for_reverification()` groups are
logged, not posted - that is explicitly out of scope for now), or hold a Sheets
API credential of any kind.

## Files

- `main.py` - the job entrypoint: read, reconcile, diff against the board's own
  current M-R values, write only the rows that changed.
- `Dockerfile`, `cloudbuild.yaml` - build only. The build context is the repo
  root (see the comments in each file for why), controlled by the root
  `.gcloudignore` allowlist.
- No `requirements.txt` - every file this job imports (`scripts/bus.py`,
  `scripts/board_say.py`, `scripts/open_for_me.py`, `scripts/ask_phases.py`,
  `scripts/rstatus_reconcile.py`, `scripts/status_client.py`) is stdlib only.

## The pieces this job is built from, and why they are not copies

- `scripts/rstatus_reconcile.py` - the pure state machine (no network). HELD
  reuses `scripts/open_for_me.hold_lifecycle` rather than reimplementing its
  security-critical same-placer authorisation. Measured against the real
  21,227-row board: calling `hold_lifecycle` naively for every one of 136 holds
  took ~58.7s each (~2 hours total) - it was built for a CLI checking a handful
  of currently-open asks, not a batch job replaying it per hold. A pre-filter in
  `_sorted_timeline()` cuts the candidate rows `hold_lifecycle` ever needs to see
  from 21,227 to the ~195 that could structurally matter (checked directly:
  everything else is skipped by its own loop anyway), bringing a full-board run
  to well under a minute without changing which clearing row any hold finds.
- `scripts/status_client.py` - the gas-bus `status_write`/`status_setup` client.
  Reuses `BUS_URL`/`BUS_SECRET`, the same credential every writer on this fleet
  already holds - no new service account, no new sheet share.
- `scripts/ask_phases.py` - the one `ASK_PHASES` constant every writer, the lint
  rule (`scripts/bcb_lint.py`), and this reconciler share.

## Performance

A full board read + reconcile + diff, measured against the real 21,227-row
board (2026-11-14 dump): ~75s for `parse_rows`/`reconcile_all` together, well
inside the 5-minute schedule. If the board keeps the growth rate it showed
between 2026-09 and 2026-11, this is worth re-measuring before it is deployed
on a schedule, not assumed to stay fast forever.
