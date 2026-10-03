#!/usr/bin/env python3
"""The same-SHA gate card (U4 of docs/team/TEAM-ROUND-20261001.md).

WHY THIS EXISTS
  On 2026-10-01, #310's merge command was handed over while a Copilot
  BLOCKER was open on its head b6fa11e. Cursor caught it and the command
  was withdrawn. The unified plan's U4 answer: a merge handoff names ONE
  SHA and links every gate on that SHA, and a helper REFUSES while
  anything is open. This is that helper.

WHAT A CLOSED GATE MEANS HERE (all of these, else REFUSED):
  - The named SHA is one full 40-hex commit, it is the pull request's
    head before AND after the reads (a moved head restarts the reviews),
    and the PR is open, not merged and not a draft in BOTH of those
    reads: a merge or a draft conversion landing mid-read is not
    hidden by the first read's word.
  - CI on that SHA is green: every named required check (default
    required-ci; --required-check overrides) is present and concluded
    exactly `success` - skipped, neutral, cancelled, empty or still
    running is NOT a pass - and carries a run URL for the card to
    link, as U4 asks. Every other check must merely be completed
    without a failing conclusion, and the combined commit status must
    not be failure or pending. Silence is not a GO: zero runs refuse,
    and so does a SHA where only unrelated checks ran.
  - Cursor's latest verdict naming that SHA is GO. The verdict must be
    authored by cursor[bot] itself and start the comment; silence or
    NO-GO refuses, and a GO on any other SHA is no GO here. The
    verdict line must name this SHA and no other full SHA, and must
    carry one verdict token: "GO or NO-GO ..." is a question, not a
    verdict.
  - Codex's latest verdict naming that SHA is GO or NO-MAJOR. A Codex
    verdict is a comment whose FIRST line is its marker (CODEX-... or
    "## Codex:"), because dispatches quote "GO or NO-GO" in passing,
    AND whose author is on the trusted relay list (--codex-relay;
    default sfdc-24, the owner account that relays Codex on this
    repository): a marker alone must not let any commenter speak for
    Codex. While every agent shares that one login, this bounds who
    can forge a verdict at "can post as the owner", not less - real
    per-agent identity is U6's job. Where Codex does not own the
    scope (docs/EXPRESS.md section 2), --codex-optional waives a
    MISSING verdict; a present NO-GO still refuses.
  - A Copilot review exists on the exact SHA and the latest one carries
    no blocker in its summary ("a BLOCKER can sit in the summary alone"
    - Copilot's own amendment to U4). A blocker in an EARLIER head's
    summary is carried forward through its anchors, every one of which
    must then be resolved whatever its badge. A summary blocker that
    anchored no finding at all has no thread to resolve, so nothing
    can show it was addressed: it refuses, and a reader who judges it
    superseded records that with --accept-superseded <review url>,
    which the card names as their call rather than the tool's.
  - Every Copilot finding that is high/critical severity or worded as a
    blocker, FROM ANY HEAD of this PR, sits in a resolved thread. A
    moved head cannot drop one: unresolved findings from earlier heads
    carry forward (Codex's amendment to U4). A high/critical anchor
    whose thread was never fetched refuses: resolution that was not
    observed is not resolution. A thread that could not be read whole
    refuses whatever its resolved flag says.

WHAT IT NEVER DOES
  It performs no merge, no write, no state change anywhere: it reads and
  it judges. The card it prints is the handoff; the merge decision stays
  with the owner. The GitHub token is sent only as a request header and
  never appears in any output.

KNOWN LIMITS (deliberate, fail-closed):
  - The blocker scan is textual. A review summary that merely quotes the
    word "blocker" (say, a docs PR about blockers) refuses; the refusal
    quotes the matched line so a human can adjudicate. The safe failure
    direction is a false refusal, never a false pass.
  - A long review thread is PAGINATED, as U4 asks, so running past one
    page is not a refusal. Only a thread still unfinished after
    MAX_THREAD_PAGES pages is "not fully read", and that refuses
    rather than judging half a thread.
  - Thread resolution state only exists in GitHub's GraphQL API, so a
    live run needs a token; `--inputs` judges a recorded snapshot with
    no network at all.
  - The whole read is repeated until the same gate state comes back
    twice running, because a verdict, review, thread or check can move
    while the OTHER resources are being fetched and the head never
    changes. Each read keeps its lists from a closing pass, so a
    change inside a read's own stagger is seen by the next one.
    It is still NOT airtight, and gather_once() says what is left: a
    change that reverts before the next read, one inside the closing
    pass's own stagger, and anything landing after gather() returns.
    A card is evidence about the moment it was read: it is not a lock,
    and nothing stops a NO-GO landing a second after it prints. That
    is why the card says it holds for that SHA only, why any push
    voids it, and why the merge stays a person's decision.

USAGE
  python3 scripts/gate_card.py --pr 310                      # live, GITHUB_TOKEN
  python3 scripts/gate_card.py --pr 310 --sha <40-hex>       # pin the one SHA
  python3 scripts/gate_card.py --pr 310 --snapshot card.json # record the reads
  python3 scripts/gate_card.py --inputs card.json            # judge offline

Exit codes: 0 the gate is closed and the card was printed; 1 REFUSED,
every open item listed; 2 the inputs could not be read or gathered.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

API = "https://api.github.com"
DEFAULT_OWNER = "sfdc-24"
DEFAULT_REPO = "Blackboard"
# v2 carries the whole second PR read (`pr_after`), not just its head
# SHA: a merge or a conversion to draft landing between the reads has
# to be seen. A v1 snapshot cannot answer that, so it is refused
# rather than judged on partial evidence.
SCHEMA = "gate-card-inputs-v2"

# Identity, split by what it is FOR. A login that grants authority -
# Cursor's GO, the existence of a Copilot review on the SHA - must be
# the exact App login REST reports, because `cursor` and
# `copilot-pull-request-reviewer` are registrable account names a
# person could hold. A login used only to DETECT a finding may accept
# either form, because GraphQL reports thread authors without the
# suffix and being loose there can only ever add refusals.
CURSOR_VERDICT_LOGIN = "cursor[bot]"
COPILOT_REVIEW_LOGIN = "copilot-pull-request-reviewer[bot]"
COPILOT_FINDING_LOGINS = frozenset({
    "copilot-pull-request-reviewer",
    "copilot-pull-request-reviewer[bot]",
})
# Who may relay a Codex verdict. On this repository Codex posts through
# the owner account; a marker-first body from anyone else is not Codex.
CODEX_RELAY_LOGINS = frozenset({"sfdc-24"})
# The checks that must exist AND pass on the named SHA. An unrelated
# green check cannot stand in for the repository's required run.
REQUIRED_CHECKS = ("required-ci",)

SHA_RE = re.compile(r"^[0-9a-f]{40}$")
SHA_IN_TEXT = re.compile(r"(?<![0-9a-f])[0-9a-f]{40}(?![0-9a-f])")

# A GitHub Actions check run links its job as
# /actions/runs/<workflow run id>/job/<job id>. That workflow run id
# is the lineage `app_id` cannot see: two WORKFLOWS are one app with
# two run ids, while a re-run is a new ATTEMPT of the SAME run id
# with a new job id. See workflow_run_id() for which half of that is
# demonstrated and which is read from GitHub's model.
ACTIONS_RUN_IN_URL = re.compile(r"/actions/runs/(\d+)(?:[/?#]|$)")
# "blocker" as a standalone word, any case: Copilot has written
# "BLOCKER - READ-NOT-DEMONSTRATED", "READ-NOT-DEMONSTRATED (blocker):"
# and "VERDICT: BLOCKER" on this repository's PRs.
BLOCKER_WORD = re.compile(r"(?i)(?<![A-Za-z0-9])blocker(?![A-Za-z0-9])")
# A finding reference in a Copilot review body: a severity badge
# introduces the thread anchor that follows it. They usually share a
# line, but not necessarily, so they are matched as one ordered
# stream rather than per line.
FINDING_LINE = re.compile(r'alt="(High|Critical) severity"')
DISCUSSION_ANCHOR = re.compile(r"#discussion_r(\d+)")
BADGE_OR_ANCHOR = re.compile(
    r'alt="(?P<severity>High|Critical) severity"|#discussion_r(?P<did>\d+)'
)
CODEX_MARKER = re.compile(r"^CODEX-[A-Za-z0-9][A-Za-z0-9-]*$")
# A line that is ENTIRELY one HTML comment, and nothing else.
HTML_COMMENT_ONLY = re.compile(r"^<!--\s*(.+?)\s*-->$")
CODEX_VERDICT = re.compile(r"\b(NO-GO|NO-MAJOR|GO)\b")
# Verdict tokens. SINKING reads these case-insensitively and
# LIFTING does not, which is not an oversight: text may never lift a
# verdict to GO, but it must always be able to sink one. VERDICT_LINE
# below already matched case-insensitively while the token extraction
# did not, so `**no-go** on <head>` opened a verdict line, yielded no
# token, and became SILENCE - leaving an older GO as the latest
# verdict and printing a card over Cursor's withdrawal. A lowercase
# `go` stays silence instead, which is safe for the same reason it
# was wrong for no-go: silence leaves the previous verdict standing,
# and a reviewer writing `go` was not trying to reverse one.
VERDICT_TOKEN = re.compile(r"\b(NO-GO|GO)\b", re.IGNORECASE)
NO_GO_ANYWHERE = re.compile(r"\bNO-GO\b", re.IGNORECASE)
# A verdict LINE: the token opens the line, optionally wrapped in **
# or __, and is followed by punctuation, end of line, or on/at/for.
# Without that tail, "**GO** through the remaining tests on <sha>"
# read as a GO. A list bullet is deliberately not accepted.
VERDICT_LINE = re.compile(
    r"^(?:\*\*|__)?(?:NO-GO|GO)(?:\*\*|__)?\s*(?:[.:,;!?)\]]|on\b|at\b|for\b|$)",
    re.IGNORECASE,
)

FAILING_CONCLUSIONS = {
    "failure",
    "cancelled",
    "timed_out",
    "action_required",
    "startup_failure",
    "stale",
}


class GateError(Exception):
    """Inputs that cannot be read or gathered (exit 2), never a verdict."""


def utc_now() -> str:
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def raw_login(login: str | None) -> str:
    """The login exactly as GitHub gave it, case-folded only.

    Every identity check uses this. There is deliberately NO helper
    that strips a `[bot]` suffix: one existed, and it let the account
    `sfdc-24` pass as the App `sfdc-24[bot]` and the account `cursor`
    pass as `cursor[bot]`. Where both spellings of a reviewer must be
    accepted, the set of accepted spellings is written out in full
    (COPILOT_FINDING_LOGINS), so that looseness is visible at the
    place it applies rather than hidden in a shared helper.
    """
    return str(login or "").strip().lower()


def first_nonempty_line(body: str | None) -> str:
    for line in str(body or "").splitlines():
        if line.strip():
            return line.strip()
    return ""


def cursor_verdict(comment: dict, named_sha: str) -> str | None:
    """GO/NO-GO when this comment is a Cursor verdict naming the SHA.

    The asymmetry is the point, and it is the same one the Codex
    reader has: TEXT MAY NEVER LIFT A VERDICT TO GO, BUT IT MUST
    ALWAYS BE ABLE TO SINK ONE.

    EVERY line is read, not just the first. A NO-GO on the second
    line of a comment whose first line said GO was being ignored, and
    so was a lead line reading "GO on <head>. NO-GO." - in both cases
    an older GO stayed the latest verdict and the card printed over a
    withdrawal.

    A VERDICT LINE opens with GO or NO-GO, optionally wrapped in ** or
    __, and that token must be followed by punctuation, end of line,
    or on/at/for. That last rule exists because stripping decoration
    loosely turned the bullet "* **GO** through the remaining tests on
    <head>." into a GO: "go" is a verb as often as a verdict, and a
    list bullet is not a verdict line at all.

    Once a line IS a verdict line and names this SHA, a NO-GO token
    anywhere on it sinks the comment. So "NO-GO on <base>. <head> is
    unaffected." refuses: that is a false refusal and the right side
    to err on, since a clean GO clears it while the opposite error
    prints a card over a live NO-GO.

    Cursor reviewed this shape and named the residual it leaves, so
    it is recorded rather than guessed at: a GO comment that also
    carries a bare line OPENING with `**NO-GO**` and naming this SHA
    sinks. QUOTING A PREVIOUS VERDICT AS SUCH A LINE INSIDE AN
    APPROVAL IS THEREFORE A FALSE REFUSAL. A markdown blockquote -
    `> **NO-GO** on <this sha>` - does not open a verdict line and
    does not sink, which is the quoting form to use. The trade is
    deliberate: the alternative is guessing which bare NO-GO line in
    an approval is a quotation, and a wrong guess there prints a card
    over a live NO-GO.

    Both question forms - "GO or NO-GO for <head>?" and "NO-GO or GO
    for <head>?" - are silence, because the token is followed by "or"
    rather than by punctuation or on/at/for, so neither opens a
    verdict line. The previous version refused one and ignored the
    other depending on which word came first, which was the real
    defect: a question is not a verdict, and the two forms must not
    mean different things.

    A GO is strict: its line must carry one verdict token and name
    this SHA and no other full SHA.
    """
    if raw_login(comment.get("author")) != CURSOR_VERDICT_LOGIN:
        return None
    lines = [ln for ln in str(comment.get("body") or "").splitlines() if ln.strip()]
    go_found = False
    for raw in lines:
        line = raw.strip()
        if VERDICT_LINE.match(line) is None:
            continue
        shas = set(SHA_IN_TEXT.findall(line.lower()))
        if named_sha not in shas:
            continue
        spellings = VERDICT_TOKEN.findall(line)
        tokens = {t.upper() for t in spellings}
        if "NO-GO" in tokens:
            return "NO-GO"
        # A GO must be SPELT as one: the sink above takes any casing,
        # this does not.
        if tokens == {"GO"} and shas == {named_sha} and "GO" in spellings:
            go_found = True
    return "GO" if go_found else None


def codex_verdict(comment: dict, named_sha: str,
                  relays: frozenset | set = CODEX_RELAY_LOGINS) -> str | None:
    """GO/NO-GO when this comment is a Codex verdict naming the SHA.

    Only a comment whose first non-empty line IS the marker counts:
    dispatches say "Reply GO or NO-GO" and cite CODEX-... ids mid-body,
    and neither of those is a verdict. Only a trusted relay login may
    carry one: a marker is text anyone can type, an author is not.

    A Codex receipt names the head it reviewed FIRST and the base it
    was read against after it ("Exact reviewed head: X. Current main:
    Y"), so the verdict binds to the first full SHA in the body: a
    receipt for an older head that merely mentions this SHA later is
    not a verdict for this SHA.
    """
    if raw_login(comment.get("author")) not in {raw_login(r) for r in relays}:
        return None
    body = str(comment.get("body") or "")
    lines = [ln for ln in body.splitlines() if ln.strip()]
    if not lines:
        return None
    # Unwrap the lead line ONLY when the whole line is one HTML
    # comment. Stripping `<!--` and the first `-->` from a line with
    # trailing text glues that text onto the id: `<!-- CODEX-ID-->NO-GO`
    # became the marker `CODEX-IDNO-GO`, which then swallowed the
    # NO-GO along with the line.
    first = lines[0].strip()
    wrapped = HTML_COMMENT_ONLY.match(first)
    lead = wrapped.group(1).strip() if wrapped else first
    id_line = bool(CODEX_MARKER.match(lead))
    heading = lead.startswith("## Codex:")
    if not (id_line or heading):
        # A comment that CLAIMS to be a Codex receipt but whose lead
        # line parses as neither an id nor a heading cannot be read,
        # and silence would let an earlier GO stand. So, naming this
        # SHA, it refuses. A dispatch that merely quotes "GO or NO-GO"
        # does not claim to be a receipt and stays silence.
        probe = first[4:].strip() if first.startswith("<!--") else first
        if (probe.startswith("CODEX-") or probe.startswith("## Codex:")) \
                and named_sha in body.lower():
            return "NO-GO"
        return None
    # Within a Codex comment, a NO-GO ANYWHERE sinks the verdict - the
    # opaque id line included. Text may never lift a verdict to GO,
    # but it must always be able to sink it, so a NO-GO cannot be
    # hidden on the line that GO parsing drops.
    if named_sha in body.lower() and NO_GO_ANYWHERE.search(body):
        return "NO-GO"
    # A bare CODEX-... line is an opaque ID, not a verdict: an id like
    # CODEX-...-GO-... must not decide anything, so it is dropped
    # before the tokens are read. A "## Codex: GO / NO-MAJOR" heading
    # is the opposite - it carries the verdict - so it is kept even
    # when it leads the comment.
    #
    # The SUBJECT and the TOKEN must come from the SAME text, or the
    # two rules can be aimed at each other: an id line naming this SHA
    # with a heading saying "GO for <another SHA>" would otherwise
    # return GO for a commit the heading never reviewed. So the SHA is
    # sought in exactly the text the token is read from.
    scan = "\n".join(lines[1:] if id_line else lines)
    shas = SHA_IN_TEXT.findall(scan.lower())
    if not shas or shas[0] != named_sha:
        return None
    # A GO must be STATED, not merely mentioned. The token has to open
    # the verdict-bearing line - the first line below a CODEX-... id,
    # or the "## Codex:" heading itself - because prose anywhere below
    # it may be discussing a verdict rather than giving one: "Review
    # pending; this is not a GO" and "No GO has been issued" both read
    # as approval when the whole body is searched.
    verdict_line = (lines[1] if id_line and len(lines) > 1 else lines[0]).strip()
    for prefix in ("## Codex:", "#### Codex:", "###### Codex:"):
        if verdict_line.startswith(prefix):
            verdict_line = verdict_line[len(prefix):].strip()
            break
    verdict_line = verdict_line.lstrip("*_ ").strip()
    m = CODEX_VERDICT.match(verdict_line)
    if m is None:
        return None
    return "NO-GO" if m.group(1) == "NO-GO" else "GO"


def latest_verdict(comments: list[dict], named_sha: str, reader) -> tuple[str, dict] | None:
    """The newest verdict for the SHA, by created_at and then order.

    The docstring claimed created_at and the code never read it: it
    kept the last match in LIST order. A live gather happens to ask
    for `created` ascending, which hid that - but a snapshot in any
    other order left an older GO standing in front of a newer NO-GO.
    A verdict with no readable timestamp is ordered by its position,
    which is all that is known about it.
    """
    found: list[tuple] = []
    for index, comment in enumerate(comments):
        verdict = reader(comment, named_sha)
        if verdict is None:
            continue
        when = parse_time(comment.get("created_at"))
        found.append((when is not None, when, index, verdict, comment))
    if not found:
        return None
    timed = [f for f in found if f[0]]
    if timed:
        best = max(timed, key=lambda f: (f[1], f[2]))
        # An untimed verdict later in the list cannot be ruled out as
        # newer, so it still wins its position.
        untimed_after = [f for f in found if not f[0] and f[2] > best[2]]
        if untimed_after:
            best = max(untimed_after, key=lambda f: f[2])
    else:
        best = max(found, key=lambda f: f[2])
    return best[3], best[4]


def blocker_lines(body: str) -> list[str]:
    return [ln.strip() for ln in str(body or "").splitlines() if BLOCKER_WORD.search(ln)]


def copilot_reviews(reviews: list[dict]) -> list[dict]:
    return [r for r in reviews
            if raw_login(r.get("author")) == COPILOT_REVIEW_LOGIN]


def copilot_finding_anchors(reviews: list[dict]) -> dict[str, str]:
    """discussion id -> why it is tracked, over EVERY head's reviews.

    This is the carry-forward. Two kinds of finding stay tracked until
    their thread is seen resolved:
      - any high/critical-severity finding, by its severity badge;
      - EVERY finding anchored in a review whose summary carried a
        blocker, whatever its badge, because that review's verdict was
        "blocked" and the badge alone does not say which finding did
        it.
    """
    anchors: dict[str, str] = {}
    for review in copilot_reviews(reviews):
        body = str(review.get("body") or "")
        summary_blocked = bool(blocker_lines(body))
        # Walk badges and anchors in document ORDER, not line by line:
        # a badge and the link it introduces may be split across lines,
        # and a per-line scan silently dropped such a finding.
        pending = ""
        for token in BADGE_OR_ANCHOR.finditer(body):
            badge = token.group("severity")
            if badge:
                pending = f"{badge.lower()}-severity"
                continue
            did = token.group("did")
            if pending:
                anchors[did] = pending
            elif summary_blocked:
                anchors.setdefault(
                    did, "anchored in a review whose summary carried a blocker"
                )
            pending = ""
    return anchors


def thread_discussion_ids(thread: dict) -> set[str]:
    ids: set[str] = set()
    for comment in thread.get("comments") or []:
        explicit = comment.get("discussion_id")
        if explicit is not None:
            ids.add(str(explicit))
        for did in DISCUSSION_ANCHOR.findall(str(comment.get("html_url") or "")):
            ids.add(did)
    return ids


def thread_gate_reason(thread: dict, high_anchors: dict[str, str]) -> str | None:
    """Why this thread gates the merge, or None when it does not.

    Truncation is judged separately in judge(): a half-read thread
    refuses whatever its resolved flag says, so it must never be
    weighed here as though its contents were known.
    """
    for comment in thread.get("comments") or []:
        if raw_login(comment.get("author")) not in COPILOT_FINDING_LOGINS:
            continue
        hits = blocker_lines(str(comment.get("body") or ""))
        if hits:
            return "Copilot blocker: " + hits[0][:200]
    for did in thread_discussion_ids(thread):
        why = high_anchors.get(did)
        if why:
            return f"Copilot finding, {why} (discussion_r{did})"
    return None


def parse_time(value) -> datetime | None:
    """An ISO 8601 instant, or None when it cannot be read as one.

    String comparison is not time comparison: a run stamped
    2026-10-02T23:00:00-04:00 is LATER than one stamped
    2026-10-03T01:00:00Z, and sorts earlier. GitHub returns Z today;
    that is not a reason to compare text.
    """
    s = str(value or "").strip()
    if not s:
        return None
    if s.endswith(("Z", "z")):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def workflow_run_id(run: dict) -> str:
    """The Actions workflow-run id this check run belongs to, or "".

    DEMONSTRATED, from a live read of this repository at head
    `eb184d7`: three workflows posted `test (...)`, `required-ci` and
    `suites` on one commit under ONE app, with three different run
    ids in their job URLs - 37085487303, 37085487344, 37085487304.
    So the run id is exactly the lineage `app_id` collapses, and it
    is already in the payload this tool gathers.

    READ, NOT DEMONSTRATED: that a re-run keeps the run id and only
    adds an ATTEMPT. GitHub's run object carries `run_attempt` and
    `previous_attempt_url`, which is what makes an attempt a property
    of one run rather than a new run, and re-running posts to
    /actions/runs/<run id>/rerun. No commit in this repository has a
    second attempt, so this half is not demonstrated here.

    If that half is WRONG, a green re-run refuses instead of passing:
    a false refusal, loud, and naming both runs. The alternative
    error - grouping on (app, name) alone - prints a card over a
    check that is currently failing in another workflow, silently.
    For a fail-closed tool that is the worse side to be wrong on, so
    this is the trade, not an oversight.
    """
    for key in ("html_url", "details_url"):
        hit = ACTIONS_RUN_IN_URL.search(str(run.get(key) or ""))
        if hit:
            return hit.group(1)
    return ""


def outcomes_agree(runs: list[dict]) -> bool:
    """Whether these runs all report the same status and conclusion."""
    return len({(str(r.get("status") or ""), str(r.get("conclusion") or ""))
                for r in runs}) <= 1


def current_check_runs(check_runs: list[dict]) -> tuple[list[dict], list[tuple]]:
    """Every CURRENT check run, and the names whose lineage is unclear.

    Two reviewers found the opposite horns of one dilemma, and both
    were right:

      Judging EVERY run means a check that failed once can never pass
      again, because GitHub leaves the failed attempt on the commit
      beside its green re-run. That gate is unsatisfiable, and an
      unsatisfiable gate gets worked around by hand.

      Keeping only the newest run per NAME hides a genuinely
      different, currently failing check that happens to share a
      display name with another app's check.

    The question is not which run is newest but WHICH RUNS ARE THE
    SAME CHECK. Two runs are attempts of one check when the same APP
    posted them INSIDE THE SAME WORKFLOW RUN; the same name from a
    different app, or from a different workflow run, is a different
    check, and both are current. So the newest attempt per
    (app, workflow run, name) is current, and every such run is
    judged.

    The workflow run is the part two reviewers disagreed about, so
    the reasoning is recorded rather than the answer. Copilot found
    that `app_id` alone is not lineage: every GitHub Actions workflow
    shares one app id, so a failing `required-ci` in a second
    workflow was being discarded as a superseded attempt of a green
    one. Cursor had already ruled against the repair I offered for
    exactly that - grouping on CHECK SUITE - because "Re-run all
    jobs" creates a new suite, which would make a green re-run
    unsatisfiable, "and this payload cannot tell that new suite from
    a second workflow."

    Both are right, and the payload CAN tell them apart, through a
    key neither named: the Actions workflow-run id in the job URL.
    It differs between workflows and holds across re-run attempts -
    see workflow_run_id(), which states which half of that is
    demonstrated from a live read and which is taken from GitHub's
    documented attempt model. Suite id stays recorded for the reader
    and deliberately unused: Cursor's objection to it stands.

    THE RESIDUAL IS NOW NARROWER, and it is a property of the
    publisher rather than of GitHub Actions: an app whose check-run
    URLs carry no workflow-run id has no lineage here at all, so two
    same-named runs from it fall back to newest-by-time, and a
    failing one can still be read as superseded. Nothing in this
    payload improves on that.

    Where runs of one name cannot be sorted into attempts at all - no
    app recorded, or one app with no start times - they are only
    UNCLEAR if they disagree about the outcome. Runs that agree need
    no lineage: there is nothing to pick between. An unclear name
    refuses whether or not it is required, because a check whose
    current run is unknown cannot be judged either way.
    """
    by_name: dict[str, list[dict]] = {}
    for run in check_runs:
        by_name.setdefault(str(run.get("name") or ""), []).append(run)

    current: list[dict] = []
    unclear: list[tuple] = []
    for name, runs in by_name.items():
        if len(runs) == 1:
            current.append(runs[0])
            continue
        if any(not str(r.get("app_id") or "") for r in runs):
            if outcomes_agree(runs):
                current.append(runs[0])
            else:
                current.extend(runs)
                unclear.append((name, "no app is recorded, so an attempt of one "
                                      "check cannot be told from another app's "
                                      "check of the same name"))
            continue
        by_lineage: dict[tuple, list[dict]] = {}
        for run in runs:
            by_lineage.setdefault(
                (str(run.get("app_id")), workflow_run_id(run)), []).append(run)
        for attempts in by_lineage.values():
            if len(attempts) == 1:
                current.append(attempts[0])
                continue
            times = [parse_time(r.get("started_at")) for r in attempts]
            if all(x is not None for x in times) and len(set(times)) == len(times):
                current.append(max(zip(times, attempts), key=lambda pair: pair[0])[1])
                continue
            if outcomes_agree(attempts):
                current.append(attempts[0])
                continue
            current.extend(attempts)
            unclear.append((name, "its attempts carry no start time to order them"))
    return current, unclear


def check_runs_state(check_runs: list[dict]) -> str:
    if not check_runs:
        return "unknown"
    current = current_check_runs(check_runs)[0]
    if any(str(r.get("status") or "") != "completed" for r in current):
        return "pending"
    if any(str(r.get("conclusion") or "") in FAILING_CONCLUSIONS for r in current):
        return "failure"
    return "success"


def ci_state(check_runs: list[dict], combined_status: dict) -> str:
    """The SHA's CI state from its check runs and its legacy statuses.

    The legacy side is judged by its CONTEXTS wherever they are
    recorded, because the rollup word alone misleads in both
    directions: GitHub reports "pending" for a commit with NO
    contexts at all (normal for a check-runs repository, and reading
    it as pending CI made the gate unable to pass any modern PR),
    while a rollup can read "success" beside a context that is
    failing. A count or a word is only trusted when the contexts
    themselves were not recorded.
    """
    run_state = check_runs_state(check_runs)
    combined = combined_status or {}
    contexts = combined.get("contexts")
    word = str(combined.get("state") or "unknown")
    total = combined.get("total_count")

    if isinstance(contexts, list):
        # A list shorter than the count the API reported is a PARTIAL
        # read, and trusting it over the rollup word is how a failure
        # on a later page went missing.
        if isinstance(total, int) and len(contexts) != total:
            return "partial"
        states = {str(c.get("state") or "").lower() for c in contexts}
        # A context whose state cannot be read is not evidence of
        # anything, least of all success. An absent `state` field was
        # being stored as "" and then counted as a passing context.
        if states - {"success", "failure", "error", "pending", "expected"}:
            return "partial"
        # The rollup must not claim worse than the contexts show, in
        # EITHER direction: the first version checked only the failing
        # word, so a "pending" rollup over nothing pending - its exact
        # twin - still passed. The empty list is left alone, because
        # pending over no contexts is GitHub's no-legacy-status shape.
        if (word.lower() in {"failure", "error"}
                and not states & {"failure", "error"}):
            return "partial"
        if (word.lower() in {"pending", "expected"} and states
                and not states & {"failure", "error", "pending", "expected"}):
            return "partial"
        if states & {"failure", "error"}:
            status_state = "failure"
        elif states & {"pending", "expected"}:
            status_state = "pending"
        elif states:
            status_state = "success"
        else:
            status_state = "absent"  # no contexts: nothing legacy to wait for
    elif isinstance(total, int) and total == 0:
        status_state = "absent"
    else:
        status_state = word  # an older snapshot that recorded only the word

    if run_state == "failure" or status_state in {"error", "failure"}:
        return "failure"
    if run_state == "pending" or status_state in {"pending", "expected"}:
        return "pending"
    if run_state == "success":
        return "success"
    return "unknown"


class Judgement:
    def __init__(self) -> None:
        self.closed: list[str] = []  # evidence lines for the card
        self.open: list[str] = []  # refusal lines, every one of them
        self.info: list[str] = []  # never gates, printed for awareness

    @property
    def passed(self) -> bool:
        return not self.open


def judge(inputs: dict, require_codex: bool = True,
          codex_relays: frozenset | set = CODEX_RELAY_LOGINS,
          required_checks: tuple | list = REQUIRED_CHECKS,
          accept_superseded: frozenset | set = frozenset()) -> Judgement:
    out = Judgement()
    if inputs.get("schema") != SCHEMA:
        raise GateError(f"inputs schema is not {SCHEMA}")

    named_sha = str(inputs.get("named_sha") or "").lower()
    pr = dict(inputs.get("pr") or {})
    pr_after = dict(inputs.get("pr_after") or {})
    comments = list(inputs.get("issue_comments") or [])
    reviews = list(inputs.get("reviews") or [])
    threads = list(inputs.get("threads") or [])
    check_runs = list(inputs.get("check_runs") or [])
    combined = dict(inputs.get("combined_status") or {})

    # 1. The one SHA.
    if not SHA_RE.match(named_sha):
        raise GateError("the card names ONE full 40-hex commit; got "
                        f"{len(named_sha)} characters")
    if not pr or not pr_after:
        raise GateError("the inputs must carry both PR reads (pr, pr_after)")
    head = str(pr.get("head_sha") or "").lower()
    head_after = str(pr_after.get("head_sha") or "").lower()
    if head != named_sha:
        out.open.append(
            f"the PR head is {head[:12]} but the card names {named_sha[:12]}: "
            "a moved head restarts the reviews"
        )
    elif head_after != named_sha:
        out.open.append(
            f"the head moved to {head_after[:12]} during the reads: "
            "a moved head restarts the reviews"
        )
    else:
        out.closed.append(f"One SHA: `{named_sha}` is the head before and after the reads")

    # 2. The pull request itself, in BOTH reads: a merge or a
    # conversion to draft that lands between them must not slip
    # through on the first read's word.
    bad_state = False
    for label, read in (("", pr), (" (by the end of the reads)", pr_after)):
        state = str(read.get("state") or "")
        if read.get("merged"):
            out.open.append(
                f"the PR is already merged{label}; there is nothing to hand over"
            )
        elif state != "open":
            out.open.append(
                f"the PR is {state or 'in an unknown state'}, not open{label}"
            )
        elif read.get("draft"):
            out.open.append(
                f"the PR is a draft{label}; a draft cannot merge (mark it ready first)"
            )
        else:
            continue
        bad_state = True
    for label, read in (("", pr), (" (by the end of the reads)", pr_after)):
        if is_conflicted(read):
            out.open.append(
                f"the branch has a merge conflict with its base{label} "
                "(mergeable_state dirty): resolve it before any handoff"
            )
            bad_state = True
    if not bad_state:
        line = "PR: open and not a draft, before and after the reads"
        mergeable = str(pr_after.get("mergeable_state") or pr.get("mergeable_state") or "")
        if mergeable:
            line += f", mergeable_state {mergeable}"
        out.closed.append(line)

    # 3. CI on the named SHA. The required run must itself be present:
    # an unrelated green check cannot stand in for it, and the card
    # links the required run, as U4 asks.
    # The check-runs endpoint can return the same NAME from more than
    # one suite, newest first. Assigning each in turn left the OLDEST
    # in hand, so `[required-ci=skipped (new), required-ci=success
    # (old)]` linked the old success and passed. The latest run is
    # chosen by start time, falling back to the order given; when
    # duplicates cannot be ordered and disagree, the evidence is
    # ambiguous and that refuses.
    current, unclear = current_check_runs(check_runs)
    runs_by_name: dict[str, list[dict]] = {}
    for run in current:
        runs_by_name.setdefault(str(run.get("name") or ""), []).append(run)
    # An unclear name refuses whether or not it is required: filtering
    # to required names let a non-required check whose current run was
    # unknown print a card over a failing run.
    ambiguous = list(unclear)
    missing = [name for name in required_checks if name not in runs_by_name]
    # Present is not passed: a required run that was skipped, neutral
    # or carries no conclusion at all has not said this SHA is good.
    # And a run with no URL cannot be linked, which is what the
    # handoff is FOR, so it refuses rather than printing "None".
    not_green: list[str] = []
    unlinkable: list[str] = []
    for name in required_checks:
        # EVERY current run of a required name must pass: two apps can
        # both post a check by that name, and both are the gate.
        for run in runs_by_name.get(name) or []:
            status = str(run.get("status") or "")
            conclusion = str(run.get("conclusion") or "")
            if status != "completed" or conclusion != "success":
                not_green.append(f"{name}={conclusion or status or 'no conclusion'}")
            elif not str(run.get("html_url") or ""):
                unlinkable.append(name)
    ci = ci_state(check_runs, combined)
    # Everything known to be wrong, computed ONCE. The partial-read
    # branch used to return before this was built, so a count mismatch
    # suppressed the failing check run and the failing context the
    # read HAD seen - the refusal named only the partial read and sent
    # the reader looking for the wrong thing.
    bad = [
        f"{r.get('name')}={r.get('conclusion') or r.get('status')}"
        for r in current
        if str(r.get("status") or "") != "completed"
        or str(r.get("conclusion") or "") in FAILING_CONCLUSIONS
    ]
    bad += [
        f"legacy status {c.get('context')}={c.get('state')}"
        for c in (combined.get("contexts") or [])
        if str(c.get("state") or "").lower()
        in {"failure", "error", "pending", "expected"}
    ]
    for name, why in sorted(ambiguous):
        out.open.append(
            f"the check `{name}` ran more than once on this SHA with different "
            f"outcomes, and {why}: the evidence cannot say which run is current"
        )
    # The required checks and the aggregate are reported
    # INDEPENDENTLY. They were one if/elif chain, so a missing
    # `required-ci` consumed the branch and a failing `lint` the same
    # read had seen was never printed - this program's deliverable is
    # a refusal that lists EVERY open item, and that chain quietly
    # made it list the first.
    if missing:
        out.open.append(
            "the required check(s) " + ", ".join(missing)
            + " never ran on this SHA: silence is not a GO"
        )
    if not_green:
        out.open.append(
            "the required check(s) did not pass on this SHA: " + "; ".join(not_green)
            + " (only `success` is a pass)"
        )
    if unlinkable:
        out.open.append(
            "the required check(s) " + ", ".join(unlinkable)
            + " carry no run URL, so the handoff cannot link the run U4 requires"
        )
    # What the required lines above already named is not repeated in
    # the aggregate line; what they did not is the whole point of it.
    rest = [b for b in bad if b not in set(not_green)]
    if ci == "success":
        # Only CLOSED when the required side is closed too: the
        # aggregate can be green while a required check never ran,
        # and "CI: success" printed beside "required-ci never ran"
        # is a card contradicting its own refusal.
        if not (missing or not_green or unlinkable):
            required_links = "; ".join(
                f"{name} {run.get('html_url')}"
                for name in required_checks for run in runs_by_name.get(name) or []
            )
            names = ", ".join(sorted(str(r.get("name") or "?") for r in check_runs))
            out.closed.append(
                f"CI on this SHA: success - required run(s): {required_links} "
                f"({len(check_runs)} check runs in all: {names})"
            )
    elif ci == "partial":
        out.open.append(
            "the legacy status list for this SHA was not read whole "
            f"({len(combined.get('contexts') or [])} context(s) recorded, the API "
            f"counted {combined.get('total_count')}, rollup "
            f"{combined.get('state')}): refusing to judge a partial read"
        )
        if bad:
            out.open.append("and what this read DID see is already wrong: "
                            + "; ".join(bad))
    elif ci == "unknown":
        out.open.append("CI on this SHA reports nothing: silence is not a GO")
    elif rest or not (missing or not_green or unlinkable):
        detail = "; ".join(rest) if rest else f"combined status {combined.get('state')}"
        out.open.append(f"CI on this SHA is {ci}: {detail}")

    # 4. Cursor.
    cursor = latest_verdict(comments, named_sha, cursor_verdict)
    if cursor is None:
        out.open.append("no Cursor verdict names this SHA: silence is not a GO")
    elif cursor[0] != "GO":
        out.open.append(f"Cursor's latest verdict on this SHA is NO-GO: {cursor[1].get('html_url')}")
    else:
        out.closed.append(f"Cursor GO: {cursor[1].get('html_url')}")

    # 5. Codex.
    codex = latest_verdict(
        comments, named_sha,
        lambda c, s: codex_verdict(c, s, relays=codex_relays),
    )
    if codex is not None and codex[0] != "GO":
        out.open.append(f"Codex's latest verdict on this SHA is NO-GO: {codex[1].get('html_url')}")
    elif codex is not None:
        out.closed.append(f"Codex GO / NO-MAJOR: {codex[1].get('html_url')}")
    elif require_codex:
        out.open.append("no Codex verdict names this SHA: silence is not a GO "
                        "(--codex-optional only where Codex does not own the scope)")
    else:
        out.closed.append("Codex: no verdict required for this scope (--codex-optional)")

    # 6. The latest Copilot review on the exact SHA.
    on_sha = [r for r in copilot_reviews(reviews)
              if str(r.get("commit_id") or "").lower() == named_sha]
    if not on_sha:
        out.open.append("no Copilot review on this exact SHA yet: ask for one and wait")
    else:
        latest = on_sha[-1]
        hits = blocker_lines(str(latest.get("body") or ""))
        if hits:
            out.open.append(
                "the latest Copilot review on this SHA carries a blocker in its "
                f"summary: \"{hits[0][:200]}\" ({latest.get('html_url')})"
            )
        else:
            out.closed.append(
                f"Copilot review on this SHA: no blocker in the summary ({latest.get('html_url')})"
            )
        # A blocker in an EARLIER head's summary is carried forward
        # through its anchors, which rule 7 requires to be resolved.
        # A summary blocker that anchored NOTHING has no thread to
        # resolve, so nothing can demonstrate it was addressed: it
        # refuses. A reader who judges it superseded by Copilot's
        # later clean review says so explicitly with
        # --accept-superseded <review url>, and the card records that
        # it was a person's call, not the tool's.
        for review in copilot_reviews(reviews):
            body = str(review.get("body") or "")
            url = str(review.get("html_url") or "")
            if (str(review.get("commit_id") or "").lower() == named_sha
                    or not blocker_lines(body)
                    or DISCUSSION_ANCHOR.search(body)):
                continue
            if url and url in accept_superseded:
                out.closed.append(
                    "an earlier head's summary blocker that anchored no finding was "
                    f"accepted as superseded by whoever ran this: {url}"
                )
            else:
                out.open.append(
                    "an earlier head's summary carried a blocker that anchored no "
                    f"finding, so no thread can show it was addressed: {url}. Ask "
                    "Copilot to re-review this SHA, or record your own judgement "
                    f"with --accept-superseded {url}"
                )

    # 7. Copilot findings from EVERY head, judged by thread resolution.
    high_anchors = copilot_finding_anchors(reviews)
    tracked = 0
    open_findings = 0
    mapped_ids: set[str] = set()
    for thread in threads:
        mapped_ids |= thread_discussion_ids(thread)
        # A half-read thread refuses whatever its resolved flag says:
        # judging it from its first page would be judging data we do
        # not have.
        if thread.get("truncated"):
            tracked += 1
            open_findings += 1
            url = ""
            for comment in thread.get("comments") or []:
                url = str(comment.get("html_url") or "") or url
            out.open.append(
                "a review thread could not be read whole (still unfinished after "
                f"{MAX_THREAD_PAGES} pages); refusing to judge half a thread - "
                f"{url or thread.get('id')}"
            )
            continue
        reason = thread_gate_reason(thread, high_anchors)
        if reason is None:
            continue
        tracked += 1
        if not thread.get("is_resolved"):
            open_findings += 1
            url = ""
            for comment in thread.get("comments") or []:
                url = str(comment.get("html_url") or "") or url
            out.open.append(f"unresolved {reason} - {url or thread.get('id')}")
    # A tracked anchor whose thread was never fetched is not resolved:
    # resolution that was not observed is not resolution.
    for did in sorted(high_anchors):
        if did not in mapped_ids:
            tracked += 1
            open_findings += 1
            out.open.append(
                f"Copilot finding discussion_r{did} ({high_anchors[did]}) has "
                "no fetched thread: its resolution was never observed"
            )
    if open_findings == 0:
        out.closed.append(
            f"Copilot findings across every head of this PR: all resolved ({tracked} tracked)"
        )

    # Awareness only: open threads that do not gate still deserve eyes.
    other_open = [
        t for t in threads
        if not t.get("is_resolved")
        and not t.get("truncated")
        and thread_gate_reason(t, high_anchors) is None
    ]
    if other_open:
        urls = []
        for t in other_open:
            for comment in t.get("comments") or []:
                u = str(comment.get("html_url") or "")
                if u:
                    urls.append(u)
                    break
        out.info.append(
            f"{len(other_open)} other unresolved thread(s) do not gate this card: "
            + ", ".join(urls)
        )

    return out


def render(inputs: dict, judgement: Judgement) -> str:
    owner = inputs.get("owner", DEFAULT_OWNER)
    repo = inputs.get("repo", DEFAULT_REPO)
    number = inputs.get("number")
    named_sha = str(inputs.get("named_sha") or "")
    title = f"{owner}/{repo}#{number} @ {named_sha[:12]}"
    lines: list[str] = []
    if judgement.passed:
        lines.append(f"## Gate card - {title}")
        lines.append("")
        lines.extend(f"- {item}" for item in judgement.closed)
        lines.append("")
        lines.append(
            "This card holds for this SHA only; any new push voids it and "
            "restarts the reviews. The merge decision stays with the owner."
        )
    else:
        lines.append(f"REFUSED - the gate is not closed for {title}")
        lines.append("")
        lines.append("Open items (every one of them, not the first):")
        lines.extend(f"- {item}" for item in judgement.open)
    if judgement.info:
        lines.append("")
        lines.extend(f"Not gating: {item}" for item in judgement.info)
    lines.append("")
    lines.append(f"Judged at {utc_now()} from reads gathered at {inputs.get('gathered_at')}.")
    return "\n".join(lines)


# ----------------------------------------------------------------------
# Gathering. Everything below talks to GitHub; everything above is pure.
# ----------------------------------------------------------------------

def make_rest(token: str | None):
    def rest(path: str, params: dict | None = None):
        url = API + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "blackboard-gate-card",
        }
        if token:
            headers["Authorization"] = "Bearer " + token
        req = urllib.request.Request(url, headers=headers, method="GET")
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read().decode("utf-8", "replace"))

    return rest


THREADS_QUERY = """
query($owner: String!, $repo: String!, $number: Int!, $cursor: String) {
  repository(owner: $owner, name: $repo) {
    pullRequest(number: $number) {
      reviewThreads(first: 50, after: $cursor) {
        pageInfo { hasNextPage endCursor }
        nodes {
          id
          isResolved
          isOutdated
          comments(first: 100) {
            pageInfo { hasNextPage endCursor }
            nodes { databaseId body createdAt url author { login } }
          }
        }
      }
    }
  }
}
"""

# A long thread's remaining comments, by the thread's node id. U4 asks
# for every review and comment to be paginated, and a thread that
# merely runs past one page must be READ, not refused for ever.
THREAD_COMMENTS_QUERY = """
query($id: ID!, $cursor: String) {
  node(id: $id) {
    ... on PullRequestReviewThread {
      comments(first: 100, after: $cursor) {
        pageInfo { hasNextPage endCursor }
        nodes { databaseId body createdAt url author { login } }
      }
    }
  }
}
"""

# How many pages of one thread's comments to read before giving up and
# refusing it as unread. 50 pages is 5000 comments: past that, some
# other thing is wrong.
MAX_THREAD_PAGES = 50


def make_graphql(token: str | None):
    def graphql(query: str, variables: dict):
        if not token:
            raise GateError(
                "review-thread resolution lives in GitHub's GraphQL API, which "
                "needs a token; set GITHUB_TOKEN or judge a snapshot with --inputs"
            )
        body = json.dumps({"query": query, "variables": variables}).encode("utf-8")
        req = urllib.request.Request(
            API + "/graphql",
            data=body,
            headers={
                "Authorization": "Bearer " + token,
                "Content-Type": "application/json",
                "User-Agent": "blackboard-gate-card",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=60) as resp:
            payload = json.loads(resp.read().decode("utf-8", "replace"))
        if payload.get("errors"):
            raise GateError("GraphQL refused: "
                            + "; ".join(str(e.get("message")) for e in payload["errors"]))
        return payload

    return graphql


def is_conflicted(read: dict | None) -> bool:
    """Whether this PR read says the branch has a merge conflict.

    GitHub's `mergeable_state` is computed asynchronously and flickers
    between "unknown" and a settled value, which is why the raw string
    is kept out of the fingerprint. `dirty` is different: it is a real
    conflict, it does not clear by itself, and a handoff card cannot
    stand over one.
    """
    return str((read or {}).get("mergeable_state") or "").lower() == "dirty"


def page_has_more(info, where: str) -> bool:
    """Whether another page follows, refusing to GUESS that none does.

    A page that does not state `hasNextPage` as a boolean has not told
    us it is the last one. Treating that silence as "no more pages"
    ended a read early and left comments - and any blocker on them -
    out of the judgement entirely.
    """
    if not isinstance(info, dict) or not isinstance(info.get("hasNextPage"), bool):
        raise GateError(
            f"{where}: the page does not say whether more follow; refusing to "
            "judge a partly read list"
        )
    if info["hasNextPage"] and not info.get("endCursor"):
        raise GateError(
            f"{where}: more pages follow but no cursor advances to them; "
            "refusing to judge a partly read list"
        )
    return info["hasNextPage"]


def comment_fields(c: dict) -> dict:
    return {
        "discussion_id": c.get("databaseId"),
        "author": str((c.get("author") or {}).get("login") or ""),
        "body": str(c.get("body") or ""),
        "created_at": str(c.get("createdAt") or ""),
        "html_url": str(c.get("url") or ""),
    }


def _paged(rest, path: str, params: dict) -> list[dict]:
    items: list[dict] = []
    page = 1
    while True:
        chunk = rest(path, {**params, "per_page": 100, "page": page})
        if not isinstance(chunk, list):
            raise GateError(f"unexpected payload from {path}")
        items.extend(chunk)
        if len(chunk) < 100:
            return items
        page += 1


def gate_fingerprint(inputs: dict) -> str:
    """Everything the judgement depends on, as one comparable string.

    A point-in-time read is not enough for a gate: a same-SHA Cursor
    NO-GO, a Copilot blocker review, a thread being reopened or a CI
    re-run can all land while the OTHER resources are being fetched,
    and the head never moves. So the whole read is repeated and
    compared on this, and only a snapshot seen twice unchanged is
    judged.

    This fingerprints the COMPLETE records rather than fields picked
    by hand. The first version listed what it thought mattered and
    left out each thread comment's author and body and each check
    run's URL, so an existing Copilot comment edited from BLOCKER to
    benign text compared equal and a state seen once was called
    settled. Whatever judge() reads is in here by construction.

    The one documented exception is the `mergeable_state` STRING,
    which GitHub computes asynchronously and can report as "unknown"
    on one read and "clean" on the next; including it would risk a
    read that can never settle, which is its own kind of failure. The
    one thing it means to the judgement is not excluded: `dirty` is a
    merge conflict and refuses, so the derived `conflicted` flag is
    fingerprinted in its place.
    """
    # `mergeable_state` itself is excluded (see above), but what it
    # MEANS for the gate is not: a flip to `dirty` is a merge
    # conflict, and that must restart settling. So the raw string is
    # replaced by the one derived fact the judgement uses, which is
    # stable under the unknown/clean flicker.
    def pr_part(read: dict) -> dict:
        part = {k: v for k, v in (read or {}).items() if k != "mergeable_state"}
        part["conflicted"] = is_conflicted(read)
        return part

    gate = {
        "pr": pr_part(inputs.get("pr")),
        "pr_after": pr_part(inputs.get("pr_after")),
        "issue_comments": inputs.get("issue_comments") or [],
        "reviews": inputs.get("reviews") or [],
        "threads": inputs.get("threads") or [],
        "check_runs": inputs.get("check_runs") or [],
        "combined_status": inputs.get("combined_status") or {},
    }
    return hash_text(json.dumps(gate, sort_keys=True, default=str))


def hash_text(text) -> str:
    return hashlib.sha256(str(text or "").encode("utf-8", "replace")).hexdigest()[:16]


def gather(owner: str, repo: str, number: int, sha: str | None,
           rest, graphql, attempts: int = 3) -> dict:
    """Read the PR until the same gate state is seen twice running."""
    previous: dict | None = None
    for attempt in range(1, max(2, attempts) + 1):
        current = gather_once(owner, repo, number, sha, rest, graphql)
        if previous is not None and gate_fingerprint(previous) == gate_fingerprint(current):
            return current
        previous = current
    raise GateError(
        f"the PR kept changing while it was being read ({attempts} attempts): a "
        "verdict, review, thread or check moved under the read. Try again once it "
        "settles - a card from a snapshot that never held is worth nothing"
    )


def pr_fields(payload: dict) -> dict:
    return {
        "state": str(payload.get("state") or ""),
        "draft": bool(payload.get("draft")),
        "merged": bool(payload.get("merged")),
        "head_sha": str((payload.get("head") or {}).get("sha") or "").lower(),
        "mergeable_state": str(payload.get("mergeable_state") or ""),
        "html_url": str(payload.get("html_url") or ""),
    }


def read_gate_lists(owner: str, repo: str, number: int, named_sha: str,
                    rest, graphql) -> dict:
    """One pass over every list the judgement reads."""
    comments = [
        {
            "id": c.get("id"),
            "author": str((c.get("user") or {}).get("login") or ""),
            "body": str(c.get("body") or ""),
            "created_at": str(c.get("created_at") or ""),
            "html_url": str(c.get("html_url") or ""),
        }
        for c in _paged(rest, f"/repos/{owner}/{repo}/issues/{number}/comments",
                        {"sort": "created", "direction": "asc"})
    ]
    reviews = [
        {
            "id": r.get("id"),
            "author": str((r.get("user") or {}).get("login") or ""),
            "state": str(r.get("state") or ""),
            "commit_id": str(r.get("commit_id") or ""),
            "body": str(r.get("body") or ""),
            "submitted_at": str(r.get("submitted_at") or ""),
            "html_url": str(r.get("html_url") or ""),
        }
        for r in _paged(rest, f"/repos/{owner}/{repo}/pulls/{number}/reviews", {})
    ]

    # Page until a SHORT page, never on total_count: a missing count
    # would otherwise end the read after page 1 and hide a failing run
    # on page 2. The count, when the API gives one, is then a
    # cross-check - a mismatch means the read was not whole, so it is
    # an error rather than a judgement on partial CI.
    check_runs: list[dict] = []
    page = 1
    total: int | None = None
    while True:
        payload = rest(f"/repos/{owner}/{repo}/commits/{named_sha}/check-runs",
                       {"per_page": 100, "page": page})
        chunk = payload.get("check_runs")
        if not isinstance(chunk, list):
            raise GateError("unexpected check-runs payload: no check_runs list")
        if page == 1 and isinstance(payload.get("total_count"), int):
            total = int(payload["total_count"])
        check_runs.extend(
            {
                "name": str(r.get("name") or ""),
                "status": str(r.get("status") or ""),
                "conclusion": str(r.get("conclusion") or ""),
                "started_at": str(r.get("started_at") or ""),
                "html_url": str(r.get("html_url") or ""),
                # `details_url` is recorded as a second place to read
                # the Actions workflow-run id from, since html_url is
                # the one a card links and could be anything.
                "details_url": str(r.get("details_url") or ""),
                # Lineage: same app + same WORKFLOW RUN + same name =
                # attempts of one check. Another app, or another
                # workflow run, is another check. See
                # current_check_runs() for why the workflow run is in
                # that key and the check suite is not.
                "app_id": str(((r.get("app") or {}).get("id")) or ""),
                "check_suite_id": str(((r.get("check_suite") or {}).get("id")) or ""),
            }
            for r in chunk
        )
        if len(chunk) < 100:
            break
        page += 1
        if page > 50:
            raise GateError("check-runs paging did not end; refusing to judge a partial read")
    if total is not None and total != len(check_runs):
        raise GateError(
            f"check-runs read is not whole: the API counted {total}, this read "
            f"has {len(check_runs)} (runs were created mid-read?); try again"
        )

    # The combined-status endpoint PAGINATES its contexts (30 by
    # default). One unparameterized request plus "trust the contexts
    # over the rollup word" meant a failing context on page 2 was
    # simply absent, and the word that would have refused was being
    # ignored. Page it, then prove the read is whole against the
    # count the API itself reports.
    first_status = rest(f"/repos/{owner}/{repo}/commits/{named_sha}/status",
                        {"per_page": 100, "page": 1}) or {}
    statuses = list(first_status.get("statuses") or [])
    # The rollup word and the count are taken from PAGE 1 and kept, as
    # they already were for check runs. Reassigning `combined` each
    # page read them from the LAST body, so a final empty page could
    # erase a `failure` rollup and its count - and the card printed.
    status_word = first_status.get("state")
    status_total = first_status.get("total_count")
    page = 1
    chunk = statuses
    while len(chunk) == 100:
        page += 1
        if page > 50:
            raise GateError(
                "legacy status paging did not end; refusing to judge a partial read"
            )
        payload = rest(f"/repos/{owner}/{repo}/commits/{named_sha}/status",
                       {"per_page": 100, "page": page}) or {}
        chunk = list(payload.get("statuses") or [])
        statuses.extend(chunk)
    if isinstance(status_total, int) and status_total != len(statuses):
        raise GateError(
            f"legacy status read is not whole: the API counted {status_total}, "
            f"this read has {len(statuses)}; try again"
        )

    threads: list[dict] = []
    cursor = None
    while True:
        payload = graphql(THREADS_QUERY,
                          {"owner": owner, "repo": repo, "number": number,
                           "cursor": cursor})
        # A null repository or pullRequest with no `errors` must not
        # read as "this PR has no review threads".
        repository = (payload.get("data") or {}).get("repository")
        pull = (repository or {}).get("pullRequest")
        conn = (pull or {}).get("reviewThreads")
        if not isinstance(conn, dict) or not isinstance(conn.get("nodes"), list):
            raise GateError(
                "GraphQL returned no reviewThreads for this PR (null repository, "
                "pull request or connection); refusing to treat that as 'no threads'"
            )
        for node in conn.get("nodes") or []:
            inner = node.get("comments") or {}
            thread_id = str(node.get("id") or "")
            comments_out = [comment_fields(c) for c in inner.get("nodes") or []]
            info = inner.get("pageInfo") or {}
            # Read the rest of a long thread rather than refusing it:
            # `truncated` is for a thread that could NOT be read whole,
            # not for one that simply runs past the first page.
            pages = 1
            more_follows = page_has_more(info, f"thread {thread_id} page 1")
            while more_follows:
                if pages >= MAX_THREAD_PAGES:
                    break
                more = graphql(THREAD_COMMENTS_QUERY,
                               {"id": thread_id, "cursor": info.get("endCursor")})
                nxt = (((more.get("data") or {}).get("node") or {})
                       .get("comments"))
                if not isinstance(nxt, dict) or not isinstance(nxt.get("nodes"), list):
                    raise GateError(
                        f"GraphQL returned no further comments for thread {thread_id}; "
                        "refusing to judge a partly read thread"
                    )
                comments_out.extend(comment_fields(c) for c in nxt["nodes"])
                info = nxt.get("pageInfo")
                pages += 1
                more_follows = page_has_more(
                    info, f"thread {thread_id} page {pages}"
                )
            threads.append(
                {
                    "id": thread_id,
                    "is_resolved": bool(node.get("isResolved")),
                    "is_outdated": bool(node.get("isOutdated")),
                    "truncated": more_follows,
                    "comments": comments_out,
                }
            )
        info = conn.get("pageInfo")
        if not page_has_more(info, "review threads page"):
            break
        cursor = info.get("endCursor")

    return {
        "issue_comments": comments,
        "reviews": reviews,
        "threads": threads,
        "check_runs": check_runs,
        # The COUNT matters as much as the word: see ci_state().
        # The CONTEXTS are what gets judged; a missing count is left
        # missing rather than invented as zero, which would have read
        # as "no legacy statuses" while a failing context sat in the
        # list beside it.
        "combined_status": {
            "state": str(status_word or "unknown"),
            "total_count": status_total if isinstance(status_total, int) else None,
            "contexts": [
                {"context": str(s.get("context") or ""),
                 "state": str(s.get("state") or "")}
                for s in statuses
            ],
        },
    }


def gather_once(owner: str, repo: str, number: int, sha: str | None,
                rest, graphql) -> dict:
    """One read of the PR, whose lists are kept from a CLOSING pass.

    The lists are sampled one after another, so a change that lands
    after a list was copied and PERSISTS is absent from that read.
    With one pass per read, a change landing after the last comment
    sample of the second read sat outside both reads, they agreed on
    the stale copy, and the card printed over something already live.
    No revert was needed. Walking the lists twice and keeping the
    SECOND copy moves each sample to the end of the read, so the next
    read sees the change and settling restarts.

    The history is worth keeping. This mechanism was written, could
    not be shown working, and was reverted: two mutants that removed
    it left the suite green, because the fakes keyed their change on
    the number of COMMENT reads, which moves with the number of
    samples. Copilot supplied the harness that distinguishes them -
    trigger the change from a LATER endpoint's fetch, so it lands
    inside one read's own stagger - and the mechanism is back with a
    regression that kills the single-pass mutant. The lesson was
    mine: "I cannot prove it" meant my test design was inadequate,
    not that the fix was worthless.

    What is still open, and cannot be read away: a change that reverts
    before the next read, a change inside the closing pass's own
    stagger, and anything landing after gather() returns. A card is
    evidence about the moment it was read, not a lock.
    """
    pr = rest(f"/repos/{owner}/{repo}/pulls/{number}")
    head = str((pr.get("head") or {}).get("sha") or "").lower()
    named_sha = (sha or head).lower()

    read_gate_lists(owner, repo, number, named_sha, rest, graphql)  # opening pass
    lists = read_gate_lists(owner, repo, number, named_sha, rest, graphql)  # kept
    pr_after = rest(f"/repos/{owner}/{repo}/pulls/{number}")

    return {
        "schema": SCHEMA,
        "owner": owner,
        "repo": repo,
        "number": number,
        "named_sha": named_sha,
        "gathered_at": utc_now(),
        "pr": pr_fields(pr),
        # The whole second read, not just its head: a merge or a
        # conversion to draft during the reads has to be visible.
        "pr_after": pr_fields(pr_after),
        **lists,
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description="Print the same-SHA gate card, or refuse while anything is open."
    )
    ap.add_argument("--owner", default=DEFAULT_OWNER)
    ap.add_argument("--repo", default=DEFAULT_REPO)
    ap.add_argument("--pr", type=int, help="pull request number (live mode)")
    ap.add_argument("--sha", help="the ONE full 40-hex SHA; defaults to the PR head")
    ap.add_argument("--codex-optional", action="store_true",
                    help="waive a MISSING Codex verdict where Codex does not own "
                         "the scope (docs/EXPRESS.md); a present NO-GO still refuses")
    ap.add_argument("--codex-relay", action="append", default=None,
                    metavar="LOGIN",
                    help="a login trusted to relay Codex verdicts (repeatable; "
                         f"default: {', '.join(sorted(CODEX_RELAY_LOGINS))}). "
                         "Passing it replaces the default list")
    ap.add_argument("--required-check", action="append", default=None,
                    metavar="NAME",
                    help="a check that must exist and pass on the SHA "
                         f"(repeatable; default: {', '.join(REQUIRED_CHECKS)}). "
                         "Passing it replaces the default list")
    ap.add_argument("--accept-superseded", action="append", default=None,
                    metavar="REVIEW_URL",
                    help="record YOUR judgement that this earlier Copilot review's "
                         "summary blocker, which anchored no finding, is superseded "
                         "by the clean review on the named SHA (repeatable). The card "
                         "names it as a person's call")
    ap.add_argument("--snapshot", help="write the gathered inputs JSON here")
    ap.add_argument("--inputs", help="judge this recorded inputs JSON; no network")
    ap.add_argument("--token-env", default="GITHUB_TOKEN")
    return ap.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        if args.inputs:
            with open(args.inputs, "r", encoding="utf-8") as fh:
                inputs = json.load(fh)
        elif args.pr:
            token = (os.environ.get(args.token_env) or "").strip() or None
            inputs = gather(args.owner, args.repo, args.pr, args.sha,
                            make_rest(token), make_graphql(token))
            if args.snapshot:
                with open(args.snapshot, "w", encoding="utf-8") as fh:
                    json.dump(inputs, fh, indent=1)
        else:
            print("one of --pr or --inputs is required", file=sys.stderr)
            return 2
        judgement = judge(
            inputs,
            require_codex=not args.codex_optional,
            codex_relays=frozenset(args.codex_relay) if args.codex_relay else CODEX_RELAY_LOGINS,
            required_checks=tuple(args.required_check) if args.required_check else REQUIRED_CHECKS,
            accept_superseded=frozenset(args.accept_superseded or ()),
        )
    except GateError as exc:
        print(f"cannot judge: {exc}", file=sys.stderr)
        return 2
    except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
        print(f"cannot read: {exc}", file=sys.stderr)
        return 2
    print(render(inputs, judgement))
    return 0 if judgement.passed else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
