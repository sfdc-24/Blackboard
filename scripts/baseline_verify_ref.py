#!/usr/bin/env python3
"""Check any git ref's Governor baseline against deployed source.

  python scripts/baseline_verify_ref.py [ref]        # default HEAD

READ ONLY - uses `git show`, never touches the working tree, needs no clasp
and no credentials. Digests are of deployed v31, published in board row
GAS-V31-DIGEST-001, sha256 after normalising CRLF to LF, compared by stem.

Two refinements over a plain digest compare, both learned the hard way:

1. JSON IS COMPARED SEMANTICALLY. appsscript.json reported MISMATCH on
   origin/session/vm-cicd while carrying the identical six oauthScopes - the
   bytes differ only in key order. A byte compare calls that drift, and a
   team that learns to wave through this mismatch will wave through a real one.

2. A MISMATCH DOES NOT MEAN STALE. This directory is both a mirror of
   deployed source and the place reviewed changes land, so a difference can
   mean the repo is AHEAD of production (a reviewed fix not yet deployed -
   correct and expected) or BEHIND it (an out-of-band deploy - the landmine).
   A digest cannot tell those apart, so this prints a line-level diffstat and
   says plainly that the direction is a human call.
"""
import hashlib
import json
import subprocess
import sys

DIGESTS_V31 = {
    "appsscript": "46aac4dd90894a0e15f16a027878e978fc21e2fe75b3302e5e525579eb44e5ba",
    "Auth": "1b7fa67196c3c904d3b389138ef8af4efec4e5612982d5134a88b3d82183dfb8",
    "Code": "ff43da1193980e7b43a657df22754f3a64784c49283456c1c00228d83aaaaf1f",
    "Index": "9e98b85129b127bc1aa6414b2e5242d917c7bc122baceaadd23d77016ef9aea7",
    "Monitor": "cc50d7a91c95638701ae9b146de40395b39013201261857411d423ad49674922",
    "PublicInbox": "09d2301c595d54096ff819eb996c0196f0e8317ded14ec4e417ae678efb8fb6a",
    "Reception": "2dce046fc9faacb0d06d0e81a24c86bcaea74fa7737b3f0d06abb6c1e6883181",
}
EXT = {"appsscript": "json", "Index": "html", "Reception": "html"}
DEPLOYED_DIR = None  # optional: dir of files fetched by gas_get_version.py


def norm(b):
    return b.replace(b"\r\n", b"\n")


def canon_json(b):
    """Canonical form, so key order and indentation are not drift."""
    return json.dumps(json.loads(b.decode("utf-8")), sort_keys=True,
                      separators=(",", ":")).encode()


def main():
    ref = sys.argv[1] if len(sys.argv) > 1 else "HEAD"
    deployed_dir = sys.argv[2] if len(sys.argv) > 2 else DEPLOYED_DIR

    clean = differs = 0
    for stem in sorted(DIGESTS_V31):
        path = f"apps-script/governor-page-api/{stem}.{EXT.get(stem, 'gs')}"
        try:
            blob = subprocess.run(["git", "show", f"{ref}:{path}"],
                                  capture_output=True, check=True).stdout
        except subprocess.CalledProcessError:
            print(f"{stem:14} ABSENT at {ref}")
            differs += 1
            continue

        body = norm(blob)
        if hashlib.sha256(body).hexdigest() == DIGESTS_V31[stem]:
            clean += 1
            print(f"{stem:14} MATCH")
            continue

        # Byte mismatch. For JSON, decide whether it is only formatting.
        if EXT.get(stem) == "json" and deployed_dir:
            try:
                live = open(f"{deployed_dir}/{stem}.json", "rb").read()
                if canon_json(body) == canon_json(live):
                    clean += 1
                    print(f"{stem:14} MATCH (semantically; byte digest differs "
                          f"on key order/formatting only)")
                    continue
            except (OSError, ValueError):
                pass

        differs += 1
        print(f"{stem:14} DIFFERS from deployed v31")
        if deployed_dir:
            try:
                live = norm(open(f"{deployed_dir}/{stem}."
                                 f"{EXT.get(stem, 'gs')}", "rb").read())
                a = body.decode("utf-8", "replace").splitlines()
                b = live.decode("utf-8", "replace").splitlines()
                import difflib
                added = sum(1 for l in difflib.unified_diff(b, a, n=0)
                            if l.startswith("+") and not l.startswith("+++"))
                removed = sum(1 for l in difflib.unified_diff(b, a, n=0)
                              if l.startswith("-") and not l.startswith("---"))
                print(f"{'':14}   repo vs deployed: +{added} / -{removed} lines")
            except OSError:
                pass

    print(f"\n{clean}/7 stems at {ref} agree with deployed v31; {differs} differ.")
    if differs:
        print("DIRECTION IS NOT DECIDED HERE. A difference means the repo is\n"
              "either AHEAD of production (a reviewed change not yet deployed)\n"
              "or BEHIND it (an out-of-band deploy). Deploying resolves the\n"
              "first and destroys the second. Read the diffstat and decide.")


if __name__ == "__main__":
    main()
