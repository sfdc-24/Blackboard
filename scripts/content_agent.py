#!/usr/bin/env python3
"""The content agent. Keeps sfdc24.com from going stale without a human topping it up.

WHY THIS EXISTS
  Mr. Salam, 2026-09-17: "create some scripts some notes and a list of things
  that can be referenced by the agents or you so the fun never stops ... keep
  things interesting and dynamic so it doesn't have to get stale in time."

  And earlier the same morning: "don't burn tokens doing obvious work that is
  repetitive ... web search ... should be offloaded to a python agent."

  A character bible written once goes stale in a month. A bank that refills
  itself does not.

TWO FILES, ON PURPOSE
  content/bank.json    CURATED. Hand-written, git-tracked, small, valuable.
                       Ads, roasts, learning nuggets, callbacks.
  content/hooks.json   FETCHED. Disposable, gitignored, regenerable.
                       Raw headlines pulled from feeds.

  The first version put both in one file, and a single `fetch` added 90 rows to
  a git-tracked file — turning every refresh into a churned diff and burying 22
  hand-written lines under machine output. Curated material and disposable
  input do not belong in the same file.

  RAW HEADLINES ARE NOT JOKES. A hook is INPUT for a persona to riff on;
  ads/roasts/learning are FINISHED copy. `pick --kind news_hook` hands you
  something to write from, never something to publish.

HOW IT STAYS FRESH — three mechanisms, because one is not enough
  1. fetch  pulls live headlines from eight feeds proven reachable 2026-09-17.
  2. age    decays by kind. A hook dies in ~21 days; a lesson never does.
  3. retire counts uses. Used `retire_after` times and it leaves rotation for
            good. THIS is what actually stops staleness — not freshness,
            repetition. Nobody hears the same line twice.

  `add` lets an AGENT write new material into the bank, which is what keeps the
  ad slot alive rather than a fixed list somebody gets bored of.

USAGE
  python scripts/content_agent.py fetch
  python scripts/content_agent.py pick --kind ad --persona grok
  python scripts/content_agent.py add ad "Einstein. Named after a genius. Suggests you email a lead." --product Einstein --by grok
  python scripts/content_agent.py age --prune
  python scripts/content_agent.py stats
"""
import argparse
import datetime as dt
import json
import os
import random
import re
import sys
import urllib.request

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BANK = os.path.join(REPO, "content", "bank.json")     # curated, committed
HOOKS = os.path.join(REPO, "content", "hooks.json")   # fetched, gitignored

UA = {"User-Agent": "Mozilla/5.0 (sfdc24 content agent)"}

# Proven reachable 2026-09-17: each returned HTTP 200 with a parseable body.
FEEDS = [
    ("salesforce-news", "https://www.salesforce.com/news/feed/", "feed"),
    ("salesforce-blog", "https://www.salesforce.com/blog/feed/", "feed"),
    ("hn-front", "https://hn.algolia.com/api/v1/search?tags=front_page", "hn"),
    ("hn-sfdc", "https://hn.algolia.com/api/v1/search_by_date?query=salesforce&tags=story", "hn"),
    ("register", "https://www.theregister.com/software/headlines.atom", "feed"),
    ("infoq", "https://feed.infoq.com/", "feed"),
    ("techcrunch", "https://techcrunch.com/feed/", "feed"),
    ("devto-sfdc", "https://dev.to/feed/tag/salesforce", "feed"),
]

# Days until worthless. None = never expires.
HALF_LIFE = {"news_hook": 21, "ad": 120, "roast": 90, "callback": None, "learning": None}

KINDS = ["news_hook", "ad", "roast", "callback", "learning"]


def _load(path, seed):
    if not os.path.exists(path):
        return seed
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception as exc:
        print("  could not read %s (%s)" % (os.path.basename(path), exc))
        return seed


def _save(path, doc):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    doc.setdefault("_meta", {})["updated"] = dt.datetime.now().isoformat(timespec="seconds")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, ensure_ascii=False, indent=1)


def load_bank():
    return _load(BANK, {"_meta": {"created": dt.date.today().isoformat()}, "items": []})


def load_hooks():
    return _load(HOOKS, {"_meta": {"what": "fetched headlines, disposable"}, "items": []})


def all_items():
    """Every item, tagged with which file owns it so a write goes back correctly."""
    out = []
    for it in load_bank().get("items", []):
        it["_file"] = "bank"
        out.append(it)
    for it in load_hooks().get("items", []):
        it["_file"] = "hooks"
        out.append(it)
    return out


def days_old(item):
    try:
        return (dt.date.today() - dt.date.fromisoformat(str(item.get("added", ""))[:10])).days
    except Exception:
        return 0


def freshness(item):
    hl = HALF_LIFE.get(item.get("kind"), 60)
    if hl is None:
        return 1.0
    return max(0.0, 1.0 - (days_old(item) / float(hl)))


def spent(item):
    return item.get("used", 0) >= item.get("retire_after", 3)


def strip_tags(s):
    return re.sub(r"<[^>]+>", " ", s or "").replace("&amp;", "&").strip()


def cmd_fetch(args):
    hooks = load_hooks()
    seen = {i.get("text", "")[:90] for i in hooks["items"]}
    seen |= {i.get("text", "")[:90] for i in load_bank().get("items", [])}
    added = 0
    for name, url, kind in FEEDS:
        try:
            raw = urllib.request.urlopen(
                urllib.request.Request(url, headers=UA), timeout=20).read().decode("utf-8", "replace")
        except Exception as exc:
            print("   %-16s unreachable (%s)" % (name, str(exc)[:46]))
            continue
        titles = []
        if kind == "hn":
            try:
                for h in json.loads(raw).get("hits", [])[:12]:
                    t = h.get("title") or h.get("story_title")
                    if t:
                        titles.append((t, h.get("url") or ""))
            except Exception:
                pass
        else:
            for m in re.finditer(r"<title[^>]*>(.*?)</title>", raw, re.S | re.I):
                t = strip_tags(re.sub(r"<!\[CDATA\[|\]\]>", "", m.group(1)))
                if t and len(t) > 18:
                    titles.append((t, ""))
            titles = titles[1:13]  # the first title is the feed's own name
        new = 0
        for t, link in titles:
            if t[:90] in seen:
                continue
            seen.add(t[:90])
            hooks["items"].append({
                "id": "hook-%s-%d" % (dt.date.today().isoformat(), len(hooks["items"])),
                "kind": "news_hook", "source": name, "text": t, "url": link,
                "added": dt.date.today().isoformat(), "used": 0, "retire_after": 2,
                "personas": ["meta", "gemini", "narrator"],
            })
            new += 1
            added += 1
        print("   %-16s %2d headline(s), %2d new" % (name, len(titles), new))
    _save(HOOKS, hooks)
    print("\n  hooks file holds %d; %d added. Curated bank untouched." % (len(hooks["items"]), added))
    return 0


def cmd_pick(args):
    pool = []
    for i in all_items():
        if args.kind and i.get("kind") != args.kind:
            continue
        if args.persona and i.get("personas") and args.persona not in i["personas"]:
            continue
        if spent(i):
            continue
        f = freshness(i)
        if f <= 0:
            continue
        pool.append((f, i))
    if not pool:
        print("  nothing left in that slice. Run `fetch`, widen the filter, or `add` something.")
        print("  (this is the system working — everything available is used up or expired)")
        return 1
    pool.sort(key=lambda p: -p[0])
    f, chosen = random.choice(pool[: max(3, len(pool) // 3)])

    if chosen.get("kind") == "news_hook":
        print("  >> RAW HOOK — this is INPUT to write from, not copy to publish.\n")
    print("  kind      : %s" % chosen.get("kind"))
    print("  freshness : %.2f  (%d days old, used %d/%d)"
          % (f, days_old(chosen), chosen.get("used", 0), chosen.get("retire_after", 3)))
    if chosen.get("teaches"):
        print("  teaches   : %s" % chosen["teaches"])
    if chosen.get("by"):
        print("  written by: %s" % chosen["by"])
    print("\n  %s\n" % chosen.get("text"))
    if chosen.get("source"):
        print("  source    : %s %s" % (chosen["source"], chosen.get("url", "")))

    if not args.dry:
        which = chosen.pop("_file", "bank")
        doc = load_bank() if which == "bank" else load_hooks()
        for it in doc["items"]:
            if it.get("id") == chosen.get("id"):
                it["used"] = it.get("used", 0) + 1
                it["last_used"] = dt.date.today().isoformat()
        _save(BANK if which == "bank" else HOOKS, doc)
    return 0


def cmd_add(args):
    """An agent writes new material. This is what keeps the ad slot alive."""
    bank = load_bank()
    item = {
        "id": args.id or ("%s-%s-%d" % (args.kind, dt.date.today().isoformat(), len(bank["items"]))),
        "kind": args.kind,
        "text": args.text,
        "added": dt.date.today().isoformat(),
        "used": 0,
        "retire_after": args.retire_after,
        "personas": [p.strip() for p in args.personas.split(",")] if args.personas else [],
    }
    if args.product:
        item["product"] = args.product
    if args.hype:
        item["hype"] = args.hype
    if args.teaches:
        item["teaches"] = args.teaches
    if args.by:
        item["by"] = args.by

    # House rule, enforced rather than hoped for.
    if args.kind == "ad" and not args.product:
        print("  REFUSED: an ad must name a REAL product. The joke is the tone,")
        print("  never an invented feature. Pass --product.")
        return 1

    bank["items"].append(item)
    _save(BANK, bank)
    print("  added %s (%s)%s" % (item["id"], args.kind, ("  by " + args.by) if args.by else ""))
    print("  bank now holds %d curated item(s)" % len(bank["items"]))
    return 0


def cmd_age(args):
    rows = sorted(((freshness(i), days_old(i), i) for i in all_items()), key=lambda r: r[0])
    dead = [r for r in rows if r[0] <= 0]
    used_up = [r for r in rows if spent(r[2])]
    print("  %d item(s): %d expired by age, %d retired by use"
          % (len(rows), len(dead), len(used_up)))
    print("\n  closest to expiry:")
    for f, age, i in [r for r in rows if r[0] > 0][:8]:
        print("   %.2f  %3dd  %-10s %s" % (f, age, i.get("kind"), (i.get("text") or "")[:62]))
    if args.prune and (dead or used_up):
        for which, path in (("bank", BANK), ("hooks", HOOKS)):
            doc = load_bank() if which == "bank" else load_hooks()
            before = len(doc["items"])
            doc["items"] = [i for i in doc["items"] if freshness(i) > 0 and not spent(i)]
            if len(doc["items"]) != before:
                _save(path, doc)
                print("  pruned %d from %s" % (before - len(doc["items"]), which))
    return 0


def cmd_stats(args):
    from collections import Counter
    items = all_items()
    kinds = Counter(i.get("kind") for i in items)
    print("  curated : %s" % BANK)
    print("  fetched : %s" % HOOKS)
    print()
    for k, n in kinds.most_common():
        live = sum(1 for i in items if i.get("kind") == k and freshness(i) > 0 and not spent(i))
        cur = sum(1 for i in items if i.get("kind") == k and i.get("_file") == "bank")
        print("   %-10s %3d total  %3d usable  %3d curated" % (k, n, live, cur))
    authored = [i for i in items if i.get("by")]
    if authored:
        print("\n   %d item(s) written by agents:" % len(authored))
        for i in authored[:6]:
            print("     %-8s %s" % (i.get("by"), (i.get("text") or "")[:58]))
    return 0


def main():
    p = argparse.ArgumentParser(description="keeps the material fresh so nobody has to")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("fetch").set_defaults(fn=cmd_fetch)

    k = sub.add_parser("pick")
    k.add_argument("--persona", default=None)
    k.add_argument("--kind", default=None, choices=KINDS)
    k.add_argument("--dry", action="store_true", help="do not count it as used")
    k.set_defaults(fn=cmd_pick)

    a = sub.add_parser("add", help="an agent writes new material into the bank")
    a.add_argument("kind", choices=KINDS)
    a.add_argument("text")
    a.add_argument("--product", default=None, help="required for an ad: the REAL feature")
    a.add_argument("--hype", default=None, choices=["none", "some", "deflated", "absurd"])
    a.add_argument("--teaches", default=None)
    a.add_argument("--by", default=None, help="which agent wrote it")
    a.add_argument("--personas", default=None, help="comma separated")
    a.add_argument("--retire-after", type=int, default=4, dest="retire_after")
    a.add_argument("--id", default=None)
    a.set_defaults(fn=cmd_add)

    g = sub.add_parser("age"); g.add_argument("--prune", action="store_true"); g.set_defaults(fn=cmd_age)
    sub.add_parser("stats").set_defaults(fn=cmd_stats)

    args = p.parse_args()
    sys.exit(args.fn(args))


if __name__ == "__main__":
    main()
