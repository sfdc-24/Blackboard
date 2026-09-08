# Blockers page — the phone channel

`web/blockers.html`, published as an Artifact with the `db` capability.
Built 2026-09-03 after Mr. Salam pointed out that work stops whenever a decision
needs him and he is away from the desk.

**Live URL:** https://claude.ai/code/artifact/f4c3d1a0-4de7-43b7-9cf9-c99b0a17a44b

Recorded here on 2026-09-08 because it was written down nowhere in the repo, so
reaching the channel meant listing every published artifact first. A channel you
have to go looking for is a channel that does not get used when it matters.

## What it is

A phone-first console listing what Claude is blocked on. Each blocker is one
card: what it is, why it is blocked, the actual question, and two to four large
buttons. One tap answers it. The answer persists server-side and Claude reads it
back with `read_db` on its next cycle.

## Why an artifact and not a page on sfdc24.com

The obvious home was a `?view=blockers` route on the Governor Page app. That was
rejected for a specific reason: **adding a route needs an Apps Script deploy,
and deploys are the exact thing that is blocked** (see ISS-001). A channel for
reporting blockers must not itself be blocked by one.

The artifact needs no deploy, is org-internal by default (the `db` capability
forbids public sharing), and can be republished from any session.

## Design decisions

**One tap, not a form.** He may be walking. Every card is answerable without
typing; the note field is collapsed and optional.

**Unanswered first, then by severity.** The list re-sorts live, so the thing
that matters is always at the top and answered cards fade rather than vanish —
he can see what he already decided.

**Live, not polled.** `onSnapshot` means an answer given on the phone updates
any other open view immediately.

**Designed for absence.** `claude.use('db')` can resolve `null`. The page
renders and says so rather than looking broken.

**Colour carries meaning, not decoration.** Red / amber / grey left stripe is
severity; green is answered. The status bar reads "4 waiting on you" or "all
clear", which is the only thing worth seeing from arm's length.

## How Claude uses it

Seed or update: `Artifact action=write_db db_op=batch collection=blockers`.
Read answers: `Artifact action=read_db db_op=list collection=blockers`.

Blocker doc shape:

```json
{
  "title": "short, plain",
  "why": "one or two sentences of context",
  "question": "the decision, phrased as a question",
  "options": [{"label": "Do it", "kind": "go"}, {"label": "No", "kind": "stop"}],
  "severity": "high | med | low",
  "status": "open | answered",
  "opened": "ISO-8601",
  "answer": "the label he tapped",
  "note": "optional free text",
  "answeredAt": "ISO-8601"
}
```

`kind` is cosmetic only — `go` renders green, `stop` red, anything else neutral.

## Rules for writing a blocker

- **Only open one when genuinely stuck.** A page that cries wolf gets ignored,
  and then it is worse than nothing.
- **Give real options.** "What should I do?" is not answerable on a phone.
- **Say what happens if he does nothing**, in `why`. Most blockers should be
  safe to leave.
- **Close them.** A stale card is noise; delete or mark answered once acted on.
