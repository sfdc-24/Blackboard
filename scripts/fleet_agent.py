#!/usr/bin/env python3
"""The fleet agent. Does the repetitive legwork so a model does not have to.

WHY THIS EXISTS
  Mr. Salam, 2026-09-17: "you have a lot of resources, don't burn tokens doing
  obvious work that is repetitive... web search, consolidating notes, data
  should be offloaded to a python agent that you build / run / install."

  He is right. Reading a 3.9 MB board into a context window to answer "who is
  active" is the definition of expensive obvious work.

WHAT IT DOES
  backup   snapshot the board, gzipped, with the shape VALIDATED
  roster   every tag that has ever written, with counts and last-seen
  keys     which agents have a working credential and which need one
  map      roster + keys together: who can join, who needs a bridge
  triage   TF-IDF topics over recent rows, so nobody reads history by hand
  dupes    near-duplicate rows (the board repeats itself a lot)
  archive  row count against the roll-over threshold

THE ONE RULE THIS FILE EXISTS TO ENFORCE
  The bus read is FLAKY. It intermittently returns a 116-byte service payload
  with {"ok":true} instead of the board:

      {"ok":true,"service":"sfdc24-blackboard-bus","time":"...","_httpStatus":200}

  Measured on 2026-09-17: one read returned 3.9 MB, the retry 116 bytes, both
  ok:true. So EVERY read here is validated on SHAPE, never on status, and a
  backup is never written from an unvalidated read. Writing a 116-byte file
  named "backup" would be worse than having no backup, because it looks like one.

USAGE
  python scripts/fleet_agent.py backup
  python scripts/fleet_agent.py map
  python scripts/fleet_agent.py triage --days 2
"""
import argparse
import datetime as dt
import gzip
import hashlib
import json
import os
import re
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# THE POWERSHELL IS GONE, AND THAT IS THE POINT.
#
# This file used to shell out to scripts/bus.ps1 twice - once to read the
# board and once to append a row - which made a pure-Python tool depend on a
# Windows shell. It is dependency 4 in docs/OPENAI-CLOUD-MIGRATION.md, and
# scripts/bus.py already does both in Python, so it was a dependency to
# delete rather than one to migrate.
#
# bus.py is also the better transport here for two reasons that have both
# cost us a day: it retries a health-blob response rather than accepting it
# as an empty board, and it REFUSES to retry an append, because a redirect
# failure raised client-side can arrive after the row has already landed.
sys.path.insert(0, os.path.join(REPO, "scripts"))
from bus import fetch as _bus_fetch, load_env as _bus_load_env  # noqa: E402
import board_row  # noqa: E402  - the one write rule: never trim a row silently
ENV = os.path.join(REPO, ".env")
BOARD = "Blackboard - Alpha DB"
BACKUPS = os.path.join(os.path.dirname(REPO), "blackboard-backups")

# A real board read is megabytes and carries a "rows" array. Anything smaller is
# the service payload wearing an ok:true.
MIN_BOARD_BYTES = 100_000


def read_board(attempts=4, quiet=False):
    """Read the board, validating the SHAPE. Returns parsed JSON or None.

    The size check below survives the move off PowerShell on purpose. bus.py
    already rejects a response with no `rows`, but a SMALL response that happens
    to carry rows is the other half of the same failure and only this caller
    knows how big a real board is.
    """
    env = _bus_load_env()
    for i in range(1, attempts + 1):
        try:
            body = _bus_fetch(env["BUS_URL"],
                              {"action": "read", "secret": env["BUS_SECRET"],
                               "title": BOARD})
        except Exception as exc:  # noqa: BLE001 - a transport failure is not an empty board
            if not quiet:
                print("   attempt %d rejected: transport (%s)" % (i, exc))
            continue
        size = len(body.encode("utf-8", "replace"))
        if size < MIN_BOARD_BYTES:
            if not quiet:
                print("   attempt %d rejected: %d bytes — the ping-payload failure" % (i, size))
            continue
        try:
            data = json.loads(body)
        except Exception as exc:
            if not quiet:
                print("   attempt %d rejected: unparseable (%s)" % (i, exc))
            continue
        if not isinstance(data.get("rows"), list) or len(data["rows"]) < 50:
            if not quiet:
                print("   attempt %d rejected: no usable rows array" % i)
            continue
        if not quiet:
            print("   read OK on attempt %d: %d rows, %.2f MB" % (i, len(data["rows"]), size / 1e6))
        return data
    return None


def rows_frame(data):
    """Board rows as a list of dicts keyed by the header row."""
    rows = data["rows"]
    head = [str(h).strip() for h in rows[0]]
    out = []
    for r in rows[1:]:
        rec = {}
        for i, h in enumerate(head):
            rec[h] = r[i] if i < len(r) else ""
        out.append(rec)
    return head, out


def parse_ts(value):
    try:
        return dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:
        return None


# ---------------------------------------------------------------- commands ---

def cmd_backup(args):
    os.makedirs(BACKUPS, exist_ok=True)
    data = read_board()
    if data is None:
        print("  NO VALID READ — refusing to write a backup that is not one.")
        return 1
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    path = os.path.join(BACKUPS, "alpha-db-%s.json.gz" % stamp)
    blob = json.dumps(data, ensure_ascii=False).encode("utf-8")
    with gzip.open(path, "wb") as fh:
        fh.write(blob)
    print("  snapshot : %s" % path)
    print("  rows     : %d" % (len(data["rows"]) - 1))
    print("  size     : %.1f KB compressed" % (os.path.getsize(path) / 1024))

    keep = args.keep
    snaps = sorted(f for f in os.listdir(BACKUPS) if f.startswith("alpha-db-"))
    print("  kept     : %d snapshot(s)" % len(snaps))
    if keep and len(snaps) > keep:
        # Oldest first; never touch the newest, and say what went.
        for old in snaps[:-keep]:
            os.remove(os.path.join(BACKUPS, old))
            print("  pruned   : %s" % old)
    return 0


def cmd_roster(args):
    data = read_board()
    if data is None:
        return 1
    _, recs = rows_frame(data)
    seen = {}
    for r in recs:
        tag = str(r.get("Source_Tag", "")).strip()
        if not tag:
            continue
        ts = parse_ts(r.get("Timestamp"))
        cur = seen.setdefault(tag, {"n": 0, "last": None})
        cur["n"] += 1
        if ts and (cur["last"] is None or ts > cur["last"]):
            cur["last"] = ts
    print("  %-26s %7s  %s" % ("tag", "rows", "last seen"))
    for tag, v in sorted(seen.items(), key=lambda kv: -kv[1]["n"]):
        last = v["last"].strftime("%Y-%m-%d %H:%M") if v["last"] else "-"
        print("  %-26s %7d  %s" % (tag, v["n"], last))
    return 0


def load_env():
    kv = {}
    if not os.path.exists(ENV):
        return kv
    for line in open(ENV, encoding="utf-8", errors="replace"):
        m = re.match(r"^\s*([A-Za-z0-9_]+)\s*=\s*(.+?)\s*$", line)
        if m:
            kv[m.group(1)] = m.group(2)
    return kv


# Who is who. Voice from scripts/voices.json, credential from .env, and the row
# counts from the board itself on 2026-09-17 — NOT from memory. The first
# version of this table was written from recollection and was wrong in three
# ways: it invented "waker" (no such agent has ever written a row), it missed
# "whatsapp" (305 rows, the busiest non-me writer), and it missed the whole
# chatgpt-codex-desktop-* family (~600 rows across session-suffixed tags,
# active the same day). Run `roster` before trusting anything here.
AGENTS = [
    ("claude-code-cli",         "nova",  "ANTHROPIC_API_KEY", "laptop; the only surface that can send WhatsApp"),
    ("vm-claude-code-cli",      "alloy", "ANTHROPIC_API_KEY", "the Azure VM worker — active, writes the board"),
    ("vm-cli",                  "alloy", "ANTHROPIC_API_KEY", "VM worker tag used by claude_board_worker.ps1"),
    ("chatgpt-codex-desktop-*", "echo",  "OPENAI_API_KEY",    "codex desktop, one tag per session"),
    ("codex",                   "echo",  "OPENAI_API_KEY",    "codex, short tag"),
    ("gemini",                  "fable", "GEMINI_API_KEY",    "82 rows to 2026-09-08, dark nine days, key restored 2026-09-17 — answering"),
    ("whatsapp",                None,    "WA_TOKEN",          "inbound lane via Pipedream; writes rows, reads none"),
    ("glasses-uploader",        None,    "GLASSES_URL",       "capture pipeline; last wrote 2026-09-05"),
    ("foundry",                 None,    "FOUNDRY_API_KEY",   "WORKING 2026-09-17: claude-opus-5 and gpt-4o, two routes, token usage"),
    ("vm-order-worker",         None,    None,                "board-only worker"),
    ("vm-chrome",               None,    None,                "board-only, browser"),
    ("chat-mobile",             None,    None,                "board-only, phone"),
    ("reception",               None,    None,                "board-only"),
    ("governor-page",           None,    None,                "the governor console writes rows"),
    ("ba",                      "coral", "OPENAI_API_KEY",    "persona: discovery in the meeting"),
    ("sa",                      "onyx",  "OPENAI_API_KEY",    "persona: technical design"),
]


def cmd_keys(args):
    kv = load_env()
    gem = [k for k in kv if re.search(r"GEMINI|GOOGLE_AI|VERTEX", k)]
    print("  %-20s %-8s %-26s %s" % ("agent", "voice", "credential", "status"))
    for name, voice, key, _note in AGENTS:
        if name == "gemini":
            status = "CAN JOIN" if gem else "NEEDS A KEY — none present"
            cred = (",".join(gem) if gem else "GEMINI_API_KEY")
        elif key is None:
            status = "board only (no credential needed)"
            cred = "-"
        else:
            status = "CAN JOIN" if kv.get(key) else "NEEDS A KEY"
            cred = key
        print("  %-20s %-8s %-26s %s" % (name, voice or "-", cred, status))
    print()
    # THIS USED TO PRINT "FOUNDRY_API_KEY is present but returned 401 on all
    # six probes — present is not the same as working." It was false, and a
    # status command stating a stale falsehood as current fact is worse than one
    # that says nothing: anyone reading it stops looking.
    #
    # The probes really did 401. The cause was the credential, not the service:
    # the value in .env was 32 characters and belonged to a DIFFERENT resource,
    # while abdus-2123-resource issues 84-character keys. With the right key the
    # first call returned 200 against three models that had been deployed the
    # whole time. Corrected 2026-09-17 after four green runs.
    print("  foundry answers on BOTH routes: claude-opus-5 on /anthropic/v1/messages")
    print("  with x-api-key, gpt-4o on /models/chat/completions with api-key.")
    print("  Cross the two and it is 401 or 404, which is what made the key look")
    print("  like a service outage. Ask which resource a key is from, first.")
    print("  Still needing a credential: grok (no agent behind it), meta (6 rows).")
    return 0


def cmd_map(args):
    print("=== who can join today ===")
    cmd_keys(args)
    print()
    print("=== and who has actually been writing ===")
    return cmd_roster(args)


def cmd_triage(args):
    data = read_board()
    if data is None:
        return 1
    _, recs = rows_frame(data)
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=args.days)
    texts, labels = [], []
    for r in recs:
        ts = parse_ts(r.get("Timestamp"))
        if ts is None or ts < cutoff:
            continue
        body = " ".join(str(r.get(k, "")) for k in ("Payload", "Action_Type", "Category"))
        body = re.sub(r"https?://\S+", " ", body)
        if len(body.strip()) > 40:
            texts.append(body)
            labels.append(str(r.get("Source_Tag", "?")))
    print("  rows in the last %d day(s): %d" % (args.days, len(texts)))
    if len(texts) < 8:
        print("  too few to cluster — printing them instead")
        for t, l in zip(texts, labels):
            print("    [%s] %s" % (l, t[:110].replace("\n", " ")))
        return 0
    try:
        from sklearn.feature_extraction.text import TfidfVectorizer
        from sklearn.cluster import KMeans
    except ImportError:
        print("  scikit-learn not installed: python -m pip install scikit-learn")
        return 1
    vec = TfidfVectorizer(max_features=4000, stop_words="english", ngram_range=(1, 2))
    X = vec.fit_transform(texts)
    k = max(2, min(args.clusters, len(texts) // 4))
    km = KMeans(n_clusters=k, n_init=8, random_state=0).fit(X)
    terms = vec.get_feature_names_out()
    for c in range(k):
        idx = km.cluster_centers_[c].argsort()[::-1][:7]
        members = [i for i, lab in enumerate(km.labels_) if lab == c]
        who = sorted({labels[i] for i in members})
        print("\n  topic %d  (%d rows)  %s" % (c + 1, len(members), ", ".join(who[:4])))
        print("    terms: %s" % ", ".join(terms[i] for i in idx))
        print("    e.g.   %s" % texts[members[0]][:120].replace("\n", " "))
    return 0


def cmd_dupes(args):
    data = read_board()
    if data is None:
        return 1
    _, recs = rows_frame(data)
    recent = recs[-args.window:]
    try:
        from rapidfuzz import fuzz
    except ImportError:
        print("  rapidfuzz not installed: python -m pip install rapidfuzz")
        return 1
    bodies = [(i, str(r.get("Payload", ""))[:400]) for i, r in enumerate(recent)]
    bodies = [(i, b) for i, b in bodies if len(b) > 60]
    hits = 0
    for a in range(len(bodies)):
        for b in range(a + 1, len(bodies)):
            s = fuzz.ratio(bodies[a][1], bodies[b][1])
            if s >= args.threshold:
                hits += 1
                if hits <= 12:
                    print("  %d%%  %s" % (s, bodies[a][1][:90].replace("\n", " ")))
                break
    print("  %d near-duplicate row(s) in the last %d" % (hits, len(recent)))
    return 0


def default_post_id(prefix: str, tag: str, phase: str, text: str, moment=None) -> str:
    """The id of a post that named none: the prefix, the second, and six hex of what is posted.

    Row ids are global and verified as exactly one row (d53079b), so the old minute-resolution
    default made a second post in the same minute a refusal (the fourth adversarial check on #367).
    The digest makes two different posts differ and a retry of the same post repeat its id."""
    stamp = (moment or dt.datetime.now(dt.timezone.utc)).strftime("%Y%m%dT%H%M%SZ")
    digest = hashlib.sha256(("%s\n%s\n%s" % (tag, phase, text or "")).encode("utf-8")).hexdigest()[:6]
    return "%s-%s-%s" % (prefix, stamp, digest)


def cmd_post(args):
    """Write a CORRECTLY SHAPED row. Hand-assembling the array is how three
    rows got mangled on 2026-09-17.

    The column contract, read off a well-formed vm-claude-code-cli row:

        Row_ID          a real identifier, e.g. VMCCC-PR73-REBASE-START-20260917T0640Z
        Timestamp       ISO-8601 with Z
        Source_Tag      the writing instance's tag
        Target_Surface  semicolon-delimited addressing, e.g. "claude-code-cli;ALL"
        Action_Type     phase: WIP | OPEN | DONE | ASK | BLOCKED | RESULT
        Payload         BCB|v=1|id=...|phase=...|from=...|to=...|<text>
        Category        OPEN | DONE
        Project Tag     ORDER, SITE, etc.
        Gist            the human-readable summary

    My three rows put the tag in Row_ID and the SUBJECT in Source_Tag, which
    shifted every column. The consequence was not cosmetic: the vote ballot's
    addressing never landed in Target_Surface, so no reader filtering on
    addressing could ever have seen it. That is why the vote drew zero replies
    from instances that were demonstrably reading the board.
    """
    tag = args.tag
    moment = dt.datetime.now(dt.timezone.utc)
    now = moment.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    to = args.to
    text = args.text
    if getattr(args, "text_file", None):
        with open(args.text_file, encoding="utf-8") as fh:
            text = fh.read()
    if not (text or "").strip():
        print("  refusing an empty post: pass text or --text-file with content")
        return 2
    rid = args.id or default_post_id(args.prefix, tag, args.phase, text, moment)
    header = "BCB|v=1|id=%s|phase=%s|class=%s|from=%s|to=%s|" % (
        rid, args.phase, args.klass, tag, to.replace(";", ","))
    # NEVER TRIM SILENTLY (board_row, 2026-10-09). Every agent's post comes through here, so this is
    # the one place the 1,500-char limit is applied. fit(write=False) only NAMES the spill object.
    # The bytes are written by commit() after the append, so a slow store is not on the post.
    spill = board_row.store_spill()
    try:
        payload, fitted = board_row.fit(header, text, key=rid, spill=spill, write=False)
    except board_row.RowRejected as exc:
        print("  %s" % exc)
        return 2
    if fitted["truncated"]:
        print("  WARNING: %d chars could not be stored (%s); the row is marked truncated=1"
              % (fitted["chars"], fitted["error"] or "no spill store"))
    gist = args.gist or board_row.split_fields(text)[1][:180]
    row = [rid, now, tag, to, args.phase, payload, args.category, args.project, gist[:180], ""]
    # exact=False: a reply retried with other text under the same id is the ask already answered.
    return append_and_verify(rid, row, fitted, spill, exact=False)


# The row list's cells, by index; the board's headers for them are Row_ID, Source_Tag, Action_Type, Payload.
ROW_ID_COL, SOURCE_COL, ACTION_COL, PAYLOAD_COL = 0, 2, 4, 5

# Exit codes of post and request. Four outcomes, four numbers, because a caller that cannot tell
# "nothing was sent" from "something may have landed" is a caller that posts twice (Grok on #367).
EXIT_VERIFIED, EXIT_NOT_SENT, EXIT_REFUSED, EXIT_UNCONFIRMED = 0, 1, 2, 3

# WHAT A REQUEST MAY NAME THROUGH THIS TOOL, on the waker's path and on the command line alike:
# scripts/redis_keys.py, one rule in one file (Grok on #367, item 1). Re-exported here for callers.
from redis_keys import key_allowed_for, SHARED_PREFIXES, OWN_INBOX_PREFIX, SHARED_READ_KEYS  # noqa: E402,F401


def _same_payload(a, b) -> bool:
    """Two payload cells that say the same thing. Whitespace-normalised, because the sheet is not
    byte-faithful about runs of spaces and the comparison must not fail every post over one."""
    return " ".join(str(a or "").split()) == " ".join(str(b or "").split())


BOARD_HEAD = ("Row_ID", "Timestamp", "Source_Tag", "Target_Surface", "Action_Type", "Payload",
              "Category", "Project Tag", "Gist", "Sub-Gist")     # the A:J layout, by position
BOARD_WIDTH = 10        # A:J, the ten named cells every row is written with


def _ten_cells(cells):
    """A row as it was written: its first ten cells as strings, a shorter row padded with "".

    None when any cell past the tenth is not blank. THE LIVE BOARD IS TWELVE WIDE: one historical
    malformed append widened Alpha DB from A:J to A:L, and getDataRange() returns every row at the
    sheet's full width, K and L blank (docs/COMMS-PROTOCOL.md rule 5, scripts/OrderSupervisor.psm1,
    src/bus_server.py). A pin on exactly ten cells (d53079b) made every post NOT SENT on that board;
    the fourth adversarial check on #367 caught it before anything was deployed. Blank padding is
    the shape; a non-blank cell past J is not this row."""
    if not isinstance(cells, (list, tuple)):
        return None
    if any(str(c if c is not None else "").strip() for c in cells[BOARD_WIDTH:]):
        return None
    head = [str(c) if c is not None else "" for c in cells[:BOARD_WIDTH]]
    return head + [""] * (BOARD_WIDTH - len(head))


def _matching_rows(rid, tries=1):
    """The board's rows that mention `rid`, by a FILTERED read, or None when the board could not be read.

    A filtered read, not the whole board (the third adversarial check on #367): the unfiltered read
    is megabytes, four attempts of two 120 s hops each, and it ran BEFORE the append inside the
    waker's 400 s budget, so a slow gateway could spend the whole budget before anything was sent,
    while the waker said "it may have landed" about a row that never was. `match` is a substring
    over the whole row (a later row quoting the id matches too), so the caller still compares
    Row_ID exactly. One try before the append: a flapping gateway is told NOT SENT at once and the
    budget is kept for the append and the read-back. No header comes back from a filtered read, so
    the cells are compared by position, the A:J layout every writer here uses."""
    env = _bus_load_env()
    try:
        body = _bus_fetch(env["BUS_URL"], {"action": "read", "secret": env["BUS_SECRET"], "title": BOARD,
                                           "match": str(rid), "limit": 200}, tries=tries)
        rows = json.loads(body).get("rows")
    except Exception:  # noqa: BLE001 - a transport failure is not an empty board
        return None
    if not isinstance(rows, list):
        return None
    # A filtered read carries no header; a gateway that ignores the filter returns the whole board
    # with one. Either way the caller compares Row_ID exactly, so the header is simply dropped.
    if rows and isinstance(rows[0], (list, tuple)) and rows[0] and str(rows[0][0]).strip() == "Row_ID":
        rows = rows[1:]
    return rows


FREE_TEXT_COLS = (PAYLOAD_COL, 8, 9)      # Payload, Gist, Sub-Gist: runs of spaces are the sheet's


def _same_complete_row(hit, row) -> bool:
    """Compare every semantic cell of the row's ten: the Timestamp must be non-empty but is not
    compared (the sheet reformats it); the free-text cells are compared with runs of spaces
    collapsed; the rest byte-exact; blank padding past the tenth cell is ignored."""
    got, want = _ten_cells(hit), _ten_cells(row)
    return (got is not None and want is not None and bool(got[1].strip())
            and all((_same_payload(got[i], want[i]) if i in FREE_TEXT_COLS else got[i] == want[i])
                    for i in range(BOARD_WIDTH) if i != 1))


def _board_rows_like(rid, row, tries=1):
    """(hit, identical, why): every row on the board under this Row_ID, those that are this row, and
    "" - or (None, None, why) when the board could not be read."""
    recs = _matching_rows(rid, tries=tries)
    if recs is None:
        return None, None, "the board could not be read (filtered read, %d tr%s)" % (tries, "y" if tries == 1 else "ies")
    hit = [r for r in recs if isinstance(r, (list, tuple)) and r and str(r[0] if r[0] is not None else "") == rid]
    return hit, [r for r in hit if _same_complete_row(r, row)], ""


def append_and_verify(rid, row, fitted=None, spill=None, exact=True):
    """Read the board, append the row if it is not there, commit its spill, and prove it landed by
    reading it back. EXIT_VERIFIED requires exactly one complete row under this global Row_ID,
    matching every semantic cell. The board may reformat Timestamp, so a nonempty timestamp is
    required but its bytes are not compared. The one append path serves post and request.

    THE BOARD IS READ FIRST. The ids are stable on purpose (reply_row_id, the waker's request_id), so
    a retry carries the id of an earlier attempt: the identical row already there is not posted
    again and counts as verified; the same id under different content is refused before posting,
    including on a reply retry. A board that cannot
    be read before posting means NOT SENT: a blind append is the double write this fleet keeps
    paying for. After the append, the read-back is the proof; "not found" is UNCONFIRMED, never
    "absent" (a 404 on the redirect hop has been raised client-side after the row landed)."""
    fitted = fitted or {}
    hit, identical, why = _board_rows_like(rid, row, tries=1)
    if hit is None:
        # The id cannot be checked, and a blind append is the double write this fleet keeps paying
        # for. Nothing was appended: the caller may try again on its next pass.
        print("  NOT SENT: %s; the id could not be checked, nothing was appended" % why)
        return EXIT_NOT_SENT
    if len(hit) == len(identical) == 1:
        print("  already on the board: Row_ID=%s carries this complete row; not posted again" % rid)
        print("  VERIFIED on the board: Row_ID=%s Source_Tag=%s Target_Surface=%s"
              % (rid, row[SOURCE_COL], row[3]))
        return EXIT_VERIFIED
    if len(hit) > 1:
        # Something WAS sent, earlier, more than once (a race): not "refused", which means nothing
        # was ever sent. The worker answers the id once; the board carries the duplicates.
        print("  UNCONFIRMED: Row_ID=%s is on the board %d times already (an earlier race); not posted "
              "again; the worker answers the id once" % (rid, len(hit)))
        return EXIT_UNCONFIRMED
    if hit:
        print("  refused before posting: %s id %s is already used by different or incomplete content; "
              "not posted again" % ("request" if exact else "reply", rid))
        return EXIT_REFUSED
    # tries=1 is not a tuning choice. The v1 bus does not dedup, and a
    # googleusercontent 404 on the redirect hop can be raised client-side AFTER
    # the row has landed - that replayed a row onto the live board once already.
    # The read-back below is what closes the loop, not this response.
    # THE MARKER BEFORE THE APPEND. A caller that kills this process on its budget reads the partial
    # output: no marker means nothing was appended (NOT SENT, try again later); the marker means the
    # append may have landed (UNCONFIRMED, never post again blindly). Flushed, so it is there.
    print("  appending Row_ID=%s" % rid, flush=True)
    try:
        body = _bus_fetch(
            _bus_load_env()["BUS_URL"],
            {"action": "append", "secret": _bus_load_env()["BUS_SECRET"],
             "title": BOARD, "sheetRow": row},
            tries=1,
        )
        print("  append said:", body.strip()[:200])
    except Exception as exc:  # noqa: BLE001
        print("  append raised %s: %s" % (type(exc).__name__, exc))
        print("  This does NOT mean the row is absent. Reading back to find out.")

    # The spill is off the post: the append above did not wait on the store. Commit now, so a
    # reader that resolves the pointer after this process returns finds the bytes. A failure is
    # said; it does not pretend the append failed, and it does not overwrite an object that landed.
    if fitted.get("deferred"):
        try:
            board_row.commit(fitted, spill)
            print("  long text (%d chars) stored whole at %s; the row carries the pointer"
                  % (fitted["chars"], fitted["pointer"]))
        except Exception as exc:  # noqa: BLE001
            print("  SPILL_UNCONFIRMED: %s: %s" % (type(exc).__name__, exc))
            print("  The row is already posted. The object name is the row id plus the sha256, so "
                  "a later commit of the same text reuses it and cannot overwrite a different one.")

    # D-4: the response is not the proof. Read the row back: exactly one identical row.
    hit, identical, why = _board_rows_like(rid, row, tries=2)
    if hit is None:
        print("  UNCONFIRMED: no usable board read after the append (%s). It may have landed. Do NOT retry blindly." % why)
        return EXIT_UNCONFIRMED
    if len(hit) == len(identical) == 1:
        print("  VERIFIED on the board: Row_ID=%s Source_Tag=%s Target_Surface=%s"
              % (rid, row[SOURCE_COL], row[3]))
        return EXIT_VERIFIED
    if len(hit) > 1:
        print("  UNCONFIRMED: %d rows carry Row_ID=%s (duplicate or conflicting appends); "
              "not confirmed as ONE post" % (len(hit), rid))
        return EXIT_UNCONFIRMED
    if hit:
        print("  UNCONFIRMED: Row_ID=%s has incomplete or changed content; do not reuse the id." % rid)
        # The two heads, so a board that is not byte-faithful shows itself here rather than as a
        # row that is never confirmed. Sixty chars is the BCB header, before any value.
        print("  expected: %r" % " ".join(str(row[PAYLOAD_COL]).split())[:60])
        found_payload = hit[0][PAYLOAD_COL] if len(hit[0]) > PAYLOAD_COL else ""
        print("  found   : %r" % " ".join(str(found_payload).split())[:60])
        return EXIT_UNCONFIRMED
    print("  UNCONFIRMED: not found after the append. The ok:true may have been a no-op, or the read "
          "raced the write. It may yet land; do NOT post again blindly.")
    return EXIT_UNCONFIRMED


# ---------------------------------------------------------------- redis-op requests
#
# Every fleet agent has governed read/write on Redis (Mr. Salam, 2026-10-08: "Everyone gets read
# write access, you control the Redis access rights and make actions auditable"). The instance has a
# private address, so the path is a BOARD ROW: the agent appends a request, the bus-requests job
# (cloud/bus-reconciler/serve_requests.py) judges it under scripts/redis_acl.json via
# scripts/redis_gov.py, does it, and answers with ONE result row addressed back to the agent's tag.
# Every operation, allowed or refused, is in gov:audit. docs/REDIS-ACCESS.md is the agents' page.
#
# WHY THIS IS ITS OWN SUBCOMMAND. `post` writes Action_Type = a phase (WIP/OPEN/DONE/ASK/BLOCKED/
# RESULT), and bus_request.parse_request() answers ONLY a row whose Action_Type is AYA_REQ. So an
# agent whose only tool is `post` - every cloud waker, Gemini's included - could never ask, however
# the access list read. Measured 2026-10-11: the ACL had named gemini and grok since 2026-10-08 and
# neither had a tool that could produce the row. This is that tool.
REQUEST_ACTION = "AYA_REQ"
REQUEST_WORKER = "bus-reconciler"
REQUEST_OPS = ("get", "hgetall", "xrange", "set", "del", "hset", "hput", "xadd")
# scripts/bus_request._REQ_ID, restated: the id is the one thing the worker takes from the row.
_REQUEST_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{3,63}$")


def request_row(tag, op, key, val=None, field=None, count=None, project="FLEET", rid=None, now=None):
    """(rid, row) for one governed Redis request, in the exact shape bus_request.parse_request reads.

    The value always travels base64 (enc=b64): the payload has no escaping, so a pipe or a newline in
    a value would otherwise end the field - and a value an agent wrote is not ours to trust with the
    grammar. The worker decodes it and judges it; this builds the row, nothing more. A request is
    never spilled: a pointer row would carry no op= for the worker to read, so one that does not fit
    is refused here with the reason, not stored elsewhere."""
    import base64
    tag = (tag or "").strip().lower()
    op = (op or "").strip().lower()
    if op not in REQUEST_OPS:
        raise ValueError("op must be one of " + ", ".join(REQUEST_OPS))
    key = (key or "").strip()
    if not key or any(ch in key for ch in "|\r\n\t "):
        raise ValueError("key is required and may not contain a pipe or whitespace")
    if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,63}", tag):
        raise ValueError("tag must be the agent's board tag (letters, digits, . _ -)")
    denied = key_allowed_for(tag, op, key)
    if denied:
        raise ValueError(denied)
    stamp = now or dt.datetime.now(dt.timezone.utc)
    # THE CALLER NAMES THE REQUEST (Grok on #367: this used to make one up from the clock and three
    # random bytes). A retry is the same id and a new request is a new id; a tool that guesses which
    # one you meant is a tool that can write twice, and the worker answers an id once.
    if not rid:
        raise ValueError("a request id is required (--id): a retry is the same id, a new request is a "
                         "new id, and this tool does not guess which one you meant")
    if not _REQUEST_ID.match(rid):
        raise ValueError("request id %r is outside the worker's id grammar" % rid)
    parts = ["BCB", "v=1", "id=%s" % rid, "req=%s" % rid, "do=redis-op", "op=%s" % op, "key=%s" % key]
    if field:
        if any(ch in field for ch in "|\r\n\t "):
            raise ValueError("field may not contain a pipe or whitespace")
        parts.append("field=%s" % field.strip())
    if val is not None:
        parts.append("val=%s" % base64.b64encode(str(val).encode("utf-8")).decode("ascii"))
        parts.append("enc=b64")
    if count is not None:
        parts.append("count=%d" % int(count))
    parts += ["from=%s" % tag, "to=%s" % REQUEST_WORKER]
    payload = "|".join(parts)
    if len(payload) > board_row.ROW_LIMIT:
        raise ValueError("the request is %d chars; a row holds %d, and a request cannot be spilled "
                         "(the value is %d bytes before base64)"
                         % (len(payload), board_row.ROW_LIMIT, len(str(val or "").encode("utf-8"))))
    payload = board_row.guard(payload)
    gist = "redis-op %s %s" % (op, key)
    row = [rid, stamp.strftime("%Y-%m-%dT%H:%M:%S.%fZ"), tag, REQUEST_WORKER, REQUEST_ACTION,
           payload, "OPEN", project, gist[:180], ""]
    return rid, row


def cmd_request(args):
    """Post one governed Redis request and read the row back. The answer arrives later, as an
    AYA_RESULT row addressed to --tag; this exit code says only whether the ASK landed.

    Exit codes (EXIT_*): 0 VERIFIED (exactly one identical row read back, or already there), 1 NOT
    SENT (the board could not be read first), 2 refused before posting (the tool's gates, or the id
    already used by a different request), 3 UNCONFIRMED (appended, no clean read-back: it may have
    landed). The waker's post_request reads the same words: VERIFIED is posted, "refused before
    posting" and NOT SENT are refused, UNCONFIRMED is unconfirmed."""
    try:
        rid, row = request_row(args.tag, args.op, args.key, val=args.val, field=args.field,
                               count=args.count, project=args.project, rid=args.id)
    except ValueError as why:
        print("  refused before posting: %s" % why)
        return EXIT_REFUSED
    print("  request %s: %s %s" % (rid, args.op, args.key))
    return append_and_verify(rid, row, exact=True)


def cmd_archive(args):
    data = read_board()
    if data is None:
        return 1
    n = len(data["rows"]) - 1
    print("  rows on the board : %d" % n)
    print("  roll-over target  : %d" % args.threshold)
    if n > args.threshold:
        print("  => OVERDUE. Start a new sheet; keep this one as history.")
        print("     The gateway has NO create action, so the new sheet has to be")
        print("     made by hand in Drive first — see the 2026-09-17 finding.")
    else:
        print("  => not yet needed (%d rows to go)" % (args.threshold - n))
    return 0


def build_parser():
    p = argparse.ArgumentParser(description="fleet legwork, so a model does not do it by hand")
    sub = p.add_subparsers(dest="cmd", required=True)

    b = sub.add_parser("backup"); b.add_argument("--keep", type=int, default=30); b.set_defaults(fn=cmd_backup)
    r = sub.add_parser("roster"); r.set_defaults(fn=cmd_roster)
    k = sub.add_parser("keys"); k.set_defaults(fn=cmd_keys)
    m = sub.add_parser("map"); m.set_defaults(fn=cmd_map)
    t = sub.add_parser("triage"); t.add_argument("--days", type=int, default=2)
    t.add_argument("--clusters", type=int, default=6); t.set_defaults(fn=cmd_triage)
    d = sub.add_parser("dupes"); d.add_argument("--window", type=int, default=400)
    d.add_argument("--threshold", type=int, default=92); d.set_defaults(fn=cmd_dupes)
    a = sub.add_parser("archive"); a.add_argument("--threshold", type=int, default=2000)
    a.set_defaults(fn=cmd_archive)

    po = sub.add_parser("post", help="write a correctly shaped BCB row and read it back")
    po.add_argument("text", nargs="?", default="")
    po.add_argument("--text-file", dest="text_file", default=None,
                    help="read the text from this path (no argv length limit)")
    po.add_argument("--tag", default="claude-code-cli")
    po.add_argument("--to", default="ALL")
    po.add_argument("--phase", default="OPEN", choices=["WIP", "OPEN", "DONE", "ASK", "BLOCKED", "RESULT"])
    po.add_argument("--klass", default="NOTE")
    po.add_argument("--category", default="OPEN")
    po.add_argument("--project", default="SITE")
    po.add_argument("--gist", default=None)
    po.add_argument("--id", default=None)
    po.add_argument("--prefix", default="CCC-NOTE")
    po.set_defaults(fn=cmd_post)

    rq = sub.add_parser("request", help="ask the governed Redis worker for one op; the answer is a later row")
    rq.add_argument("op", choices=list(REQUEST_OPS))
    rq.add_argument("key")
    rq.add_argument("--val", default=None, help="the value (set, hset, xadd) or a JSON object (hput)")
    rq.add_argument("--field", default=None, help="the hash field, for hset")
    rq.add_argument("--count", type=int, default=None, help="entries to return, for xrange")
    rq.add_argument("--tag", default="claude-code-cli")
    rq.add_argument("--project", default="FLEET")
    rq.add_argument("--id", required=True,
                    help="your request id (letters, digits, . _ -; 4-64 chars): the same id for a retry, "
                         "a new id for a new request; the tool does not make one up")
    rq.set_defaults(fn=cmd_request)
    return p


def main():
    args = build_parser().parse_args()
    sys.exit(args.fn(args))


if __name__ == "__main__":
    main()
