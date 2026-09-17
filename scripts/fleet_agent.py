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
import json
import os
import re
import subprocess
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BUS = os.path.join(REPO, "scripts", "bus.ps1")
ENV = os.path.join(REPO, ".env")
BOARD = "Blackboard - Alpha DB"
BACKUPS = os.path.join(os.path.dirname(REPO), "blackboard-backups")

# A real board read is megabytes and carries a "rows" array. Anything smaller is
# the service payload wearing an ok:true.
MIN_BOARD_BYTES = 100_000


def read_board(attempts=4, quiet=False):
    """Read the board, validating the SHAPE. Returns parsed JSON or None."""
    out = os.path.join(tempfile.gettempdir(), "fleet_%d.json" % os.getpid())
    for i in range(1, attempts + 1):
        subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", BUS,
             "-Action", "read", "-Title", BOARD, "-OutFile", out],
            cwd=REPO, capture_output=True, text=True, timeout=300,
        )
        if not os.path.exists(out):
            continue
        size = os.path.getsize(out)
        if size < MIN_BOARD_BYTES:
            if not quiet:
                print("   attempt %d rejected: %d bytes — the ping-payload failure" % (i, size))
            continue
        try:
            with open(out, encoding="utf-8") as fh:
                data = json.load(fh)
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
    ("gemini",                  "fable", None,                "82 rows to 2026-09-08 then silent — key withdrawn"),
    ("whatsapp",                None,    "WA_TOKEN",          "inbound lane via Pipedream; writes rows, reads none"),
    ("glasses-uploader",        None,    "GLASSES_URL",       "capture pipeline; last wrote 2026-09-05"),
    ("foundry",                 None,    "FOUNDRY_API_KEY",   "3 rows; key present but 401 on every probe"),
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
    print("  FOUNDRY_API_KEY is present but returned 401 on all six probes")
    print("  (2026-09-17) — present is not the same as working.")
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


def cmd_post(args):
    """Write a CORRECTLY SHAPED row. Hand-assembling the array is how three
    rows got mangled on 2026-09-17.

    The column contract, read off a well-formed vm-claude-code-cli row:

        Row_ID          a real identifier, e.g. VMCCC-PR73-REBASE-START-20260917T0640Z
        Timestamp       ISO-8601 with Z
        Source_Tag      the writing instance's tag
        Target_Surface  semicolon-delimited addressing, e.g. "claude-code-cli;ALL"
        Action_Type     phase: WIP | OPEN | DONE | ASK | BLOCKED
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
    now = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    rid = args.id or ("%s-%s" % (args.prefix, dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%MZ")))
    to = args.to
    payload = "BCB|v=1|id=%s|phase=%s|class=%s|from=%s|to=%s|%s" % (
        rid, args.phase, args.klass, tag, to.replace(";", ","), args.text)
    row = [rid, now, tag, to, args.phase, payload, args.category, args.project, args.gist or args.text[:180], ""]

    out = subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", BUS,
         "-Action", "append", "-Title", BOARD, "-SheetRowJson", json.dumps(row, ensure_ascii=False)],
        cwd=REPO, capture_output=True, text=True, timeout=300,
    )
    print("  append said:", (out.stdout or out.stderr).strip()[:200])

    # D-4: the response is not the proof. Read the row back by its own id.
    data = read_board(quiet=True)
    if data is None:
        print("  COULD NOT VERIFY — no valid board read. Do NOT retry blindly.")
        return 1
    _, recs = rows_frame(data)
    hit = [r for r in recs if str(r.get("Row_ID", "")) == rid]
    if hit:
        print("  VERIFIED on the board: Row_ID=%s Source_Tag=%s Target_Surface=%s"
              % (rid, hit[-1].get("Source_Tag"), hit[-1].get("Target_Surface")))
        return 0
    print("  NOT FOUND after append — the ok:true was a silent no-op.")
    return 1


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


def main():
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
    po.add_argument("text")
    po.add_argument("--tag", default="claude-code-cli")
    po.add_argument("--to", default="ALL")
    po.add_argument("--phase", default="OPEN", choices=["WIP", "OPEN", "DONE", "ASK", "BLOCKED"])
    po.add_argument("--klass", default="NOTE")
    po.add_argument("--category", default="OPEN")
    po.add_argument("--project", default="SITE")
    po.add_argument("--gist", default=None)
    po.add_argument("--id", default=None)
    po.add_argument("--prefix", default="CCC-NOTE")
    po.set_defaults(fn=cmd_post)

    args = p.parse_args()
    sys.exit(args.fn(args))


if __name__ == "__main__":
    main()
