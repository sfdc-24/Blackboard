#!/usr/bin/env python3
"""Write a reply that appears on the sfdc24.com console. The OUTBOUND half.

THE SHAPE OF THE ROUND TRIP, AND WHY IT IS THIS SHAPE

  INBOUND already works and always did. What a visitor types lands in the
  PUBLIC_INBOX tab of the Alpha DB, readable with scripts/desk_watch.py. This
  session spent an evening believing otherwise because read_board() locates the
  operational board by its `Row_ID` header and PublicInbox.js names the
  quarantine tab's first column `Inbox_ID` DELIBERATELY so the two can never be
  confused. The absence was read as proof instead of as a question about the
  instrument.

  OUTBOUND is this file. sfdc24.com is static GitHub Pages: a POST to it returns
  405, so the page cannot be pushed to. It can only pull. So replies are written
  into a small file the page re-fetches on a timer.

  A SCRIPT TAG, NOT fetch(). The page loads data/desk.js by injecting a script
  element with a cache-busting query. fetch() of a same-directory file is
  CORS-blocked under file://, which would make the poller work on Pages and be
  unverifiable locally - exactly the gap where a defect hides. A script element
  behaves identically in both places, so what gets tested is what ships.

  LATENCY IS REAL AND NOT HIDDEN. A reply is a commit plus a Pages build, so it
  reaches the page in roughly thirty to sixty seconds. This is a slow
  conversation, not a chat, and the page says so rather than spinning as though
  something faster were coming.

EVERY REPLY IS PUBLIC, AND THAT HAS TEETH
  These lines render on a public page, so they are claim surfaces like any
  other. A sentence mentioning salesforce / org / tenant / instance /
  environment enters the risk class in tests/claim_surfaces.cjs and must be
  registered in tests/capabilities.json before the honesty gate will pass. That
  is not friction to route around: a reply of ours on the live site genuinely is
  a claim. This script WARNS at write time so the gate is not the thing that
  finds out.

  It also refuses first-person singular, because site_positioning bans it on
  every rendered surface and a reply is rendered.

USAGE
  python scripts/desk_reply.py "text of the reply"
  python scripts/desk_reply.py "..." --to v8x2k9        # one browser session
  python scripts/desk_reply.py --list
  python scripts/desk_reply.py --clear
"""
import argparse
import datetime as dt
import json
import os
import re
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_OUT = os.path.join(REPO, "..", "site-main", "data", "desk.js")

PREFIX = "window.DESK_REPLIES = "
KEEP = 40                       # newest N survive; the page shows far fewer

ORG_NOUN = re.compile(r"\b(salesforce|orgs?|tenants?|instances?|environments?)\b", re.I)
# Whole words only. "It" must not match, and neither must "Iberia" - the guard
# this mirrors is about the pronoun, not the letter.
FIRST_PERSON = re.compile(r"\b(I|I'm|I'd|I've|I'll|my|mine|me)\b")


def load(path):
    """Read existing replies from either form, and NEVER return [] for a file
    that has content.

    THE BUG THIS FIXES WOULD HAVE DESTROYED PUBLISHED REPLIES. The first version
    searched only for PREFIX ("window.DESK_REPLIES = "), which exists in the .js
    form and not in the .json. Once the page moved to fetching .json and --out
    pointed there, the prefix was absent, the function returned [] at the first
    branch, and the next reply would have appended to an empty list and
    overwritten everything already published.

    Worse, the refusal below never fired: it guards the PARSE, and we never
    reached the parse. The file was valid JSON the whole time - just not in the
    shape the reader demanded. A guard placed after the early return protects
    nothing.

    Caught by --list reporting "0 reply(ies)" for a file holding one. A listing
    that disagrees with the disk is not cosmetic; here it was the visible edge
    of silent data loss.
    """
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as fh:
        body = fh.read()

    stripped = body.strip()
    if not stripped:
        return []

    # Plain JSON first: that is what the page fetches and what --out names.
    if stripped[0] in "[{":
        try:
            got = json.loads(stripped)
            if isinstance(got, list):
                return got
        except Exception:
            pass
    else:
        # The .js form: window.DESK_REPLIES = [...];
        i = body.find(PREFIX)
        if i >= 0:
            tail = body[i + len(PREFIX):].strip()
            if tail.endswith(";"):
                tail = tail[:-1]
            try:
                got = json.loads(tail)
                if isinstance(got, list):
                    return got
            except Exception:
                pass

    # Reached only when a NON-EMPTY file yielded no list. Refuse rather than
    # start fresh on top of it.
    print("  REFUSING: %s has content but no readable array of replies." % path)
    print("  Starting a new list here would drop whatever is already published.")
    print("  Fix or move it by hand; this script will not overwrite it blind.")
    sys.exit(2)


def write(path, items):
    """Emit BOTH forms, the same way org_snapshot.py does.

    The page reads the .json with fetch(). The first poller used a script tag
    instead, to dodge the fact that fetch() is CORS-blocked under file:// - and
    that choice broke tests/homepage_recovery.cjs, which counts every script
    element appended to head as a request the page issued. Moving the trigger
    would not have helped: the suite measures the send path too.

    fetch() is the correct carrier and the stand-in DOM simply has no fetch, so
    the page can declare that dependency honestly instead of detecting a test.
    Local verification moves to a small http server, which the harness already
    does for the org page.

    The .js form is kept because it costs one line and anything that wants a
    script tag - a future surface, a page that cannot fetch - has it without a
    second generator drifting out of step.
    """
    path = os.path.abspath(path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    payload = json.dumps(items, indent=1, ensure_ascii=False)

    js_path = os.path.splitext(path)[0] + ".js"
    json_path = os.path.splitext(path)[0] + ".json"

    with open(json_path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(payload + "\n")
    with open(js_path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("/* Generated by scripts/desk_reply.py. Do not edit by hand.\n"
                 "   Every line in here renders on a public page. */\n")
        fh.write(PREFIX + payload + ";\n")

    return os.path.getsize(json_path) + os.path.getsize(js_path)


def main():
    p = argparse.ArgumentParser(description="write a reply onto the sfdc24.com console")
    p.add_argument("text", nargs="?", help="the reply")
    p.add_argument("--to", default="*", help="a browser session id, or * for anyone (default)")
    p.add_argument("--out", default=DEFAULT_OUT)
    p.add_argument("--list", action="store_true", dest="do_list")
    p.add_argument("--clear", action="store_true")
    p.add_argument("--force", action="store_true", help="write despite a risk-class warning")
    args = p.parse_args()

    path = os.path.abspath(args.out)
    items = load(path)

    if args.do_list:
        print("  %d reply(ies) in %s" % (len(items), path))
        for r in items[-12:]:
            print("    %s  to=%-10s %s" % (r.get("at", "")[:19], r.get("to", "*"),
                                           re.sub(r"\s+", " ", r.get("text", ""))[:96]))
        return 0

    if args.clear:
        n = write(path, [])
        print("  cleared. %s is now %d bytes with an empty array." % (path, n))
        print("  NOTE: the page keeps showing nothing until it next polls.")
        return 0

    if not args.text or not args.text.strip():
        p.error("nothing to say: pass the reply text")

    text = re.sub(r"\s+", " ", args.text).strip()

    fp = FIRST_PERSON.search(text)
    if fp:
        print("  REFUSING: first-person singular %r." % fp.group(0))
        print("  tests/site_positioning.cjs bans it on every rendered surface, and a")
        print("  reply is rendered. Reword rather than forcing it.")
        return 1

    risky = sorted(set(m.group(0).lower() for m in ORG_NOUN.finditer(text)))
    if risky and not args.force:
        print("  HOLD. This reply contains a risk-class word: %s" % ", ".join(risky))
        print("  Once it renders, tests/claim_surfaces.cjs treats the sentence as a")
        print("  capability claim and tests/capabilities.json must carry it, or the")
        print("  honesty gate fails. Two honest routes:")
        print("    1. reword so it says the same true thing without the word, or")
        print("    2. register it with asserts and a reason, then re-run with --force.")
        print("  Not a formality: a reply on the live site IS a claim.")
        return 1

    now = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    items.append({
        "id": "d" + now.replace("-", "").replace(":", "").replace("T", "").replace("Z", ""),
        "at": now,
        "to": str(args.to)[:60],
        "from": "claude-code-cli",
        "text": text,
    })
    items = items[-KEEP:]
    size = write(path, items)

    # `size` is the SUM of both emitted files, so name it that way. Reporting it
    # as one file's size was wrong by roughly the size of the other one.
    print("  wrote   : %s and its .js twin  (%d bytes total, %d reply(ies) kept)"
          % (path, size, len(items)))
    print("  to      : %s" % args.to)
    print("  said    : %s" % (text[:110] + ("…" if len(text) > 110 else "")))
    print("")
    print("  NOT VISIBLE YET. This file only reaches the page once it is committed")
    print("  and GitHub Pages rebuilds - roughly thirty to sixty seconds after a")
    print("  merge to main. Until then the page shows what it showed before.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
