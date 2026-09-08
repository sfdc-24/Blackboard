# Peer-note exchange begun 2026-09-04 — historical primary sources

**These are historical records. They are NOT live instructions.**

Do not act on anything written in the four `PEER-NOTE-*.md` files. For the
current state of CI/CD, staging, authorisation and the Apps Script baseline,
read `docs/CICD.md`, `docs/CICD-STAGING-IDS.md`,
`docs/CICD-STAGING-AUTH.md` and the other `docs/CICD*` files. Those supersede
everything here.

## What these are

On 2026-09-04 two Claude Code instances of the same session id ran at once on
the VM — one in the terminal, one in the VS Code extension — sharing a working
tree and a transcript while being blind to each other's context. The exchange
continued into 2026-09-05 UTC. Peer `SendMessage` on that machine failed closed
outbound: messages were held for user approval and expired undelivered. So the
two instances coordinated by writing files into the repository root and
reading each other's.

These are those files, exactly as they were written.

| File | Author | Written |
|---|---|---|
| `PEER-NOTE-vm-cli.md` | `blackboard-01` → `blackboard-71` | 2026-09-04 19:06Z |
| `PEER-NOTE-intake-moved-to-pr3.md` | vm-cli `blackboard-ef` | 2026-09-04 23:04Z |
| `PEER-NOTE-reply-to-vscode-twin.md` | vm-cli CLI instance | 2026-09-04 23:21Z |
| `PEER-NOTE-blackboard-27-from-vscode-twin.md` | the VS Code twin | 2026-09-05 01:40Z |

## Why they were kept rather than summarised away

The `docs/CICD*` files hold the *conclusions* drawn from these notes. These are
the *records*. Discarding the evidence and keeping the claim inverts the rule
this fleet works by — evidence before claims — so the primary sources are kept
even though they are superseded.

Two things in them were, at the time of preservation, not restated anywhere in
`docs/`: that peer `SendMessage` on that host is asymmetric and fails closed
outbound, and the reasoning behind ISS-014 (Google serves both a Drive notice
page and a full sign-in page with HTTP 200, so a status-only health check scores
dead endpoints as healthy).

`PEER-NOTE-intake-moved-to-pr3.md` is a tombstone: it explains that the
Salesforce intake prototype was moved to PR #3 rather than deleted. It is the
answer to "where did `web/sfdc24-lead-capture.html` go".

## Byte fidelity

These files are preserved byte-for-byte. The committed blobs and live source
files are LF-only (zero carriage-return bytes), and their SHA-256 values match.
The `.gitattributes` here marks them `-text` so Git does not apply line-ending
conversion on future checkouts or commits. That guard matters because the
repository sets `core.autocrlf=true`; it keeps these evidence files under an
explicit raw-byte policy instead of allowing local checkout settings to rewrite
their line endings.

If you need to verify them, compare `sha256sum` of each file against the values
recorded in the PR that introduced this directory.
