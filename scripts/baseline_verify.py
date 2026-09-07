#!/usr/bin/env python3
"""Compare apps-script/governor-page-api/ against the published v31 digests.

READ ONLY. Source files are compared as bytes with every CRLF replaced by LF,
by filename STEM, so the repo's .gs against Apps Script's .js is not drift.

appsscript.json is NOT compared as bytes. Its six oauthScopes can be identical
while the bytes differ purely in key order, and a team that learns to wave
through a mismatch it knows is cosmetic will eventually wave through a real
one. It is parsed and canonicalised first, per GAS-V31-DIGEST-002.

The canonical form is deliberately NOT redefined here. A digest comparison
only holds if both sides canonicalise identically, and canonical JSON is not
self-defining: sort order, separators, ensure_ascii and trailing newline all
change the bytes. So this imports the same canonical() the build-identity
helper uses, rather than opening a second definition of the word.
"""
import hashlib
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from gas_build_identity import canonical  # noqa: E402  (path set above)

# Stems whose digest is taken over canonical JSON rather than raw bytes.
JSON_STEMS = {"appsscript"}

DIGESTS_V31 = {
    # canonical, per GAS-V31-DIGEST-002; supersedes the raw-byte 46aac4dd...
    "appsscript": "7f5f66e4683a5bfe1b43e9df89be7bd3dbbe0a21f469f99eefb5d324930dc19d",
    "Auth": "1b7fa67196c3c904d3b389138ef8af4efec4e5612982d5134a88b3d82183dfb8",
    "Code": "ff43da1193980e7b43a657df22754f3a64784c49283456c1c00228d83aaaaf1f",
    "Index": "9e98b85129b127bc1aa6414b2e5242d917c7bc122baceaadd23d77016ef9aea7",
    "Monitor": "cc50d7a91c95638701ae9b146de40395b39013201261857411d423ad49674922",
    "PublicInbox": "09d2301c595d54096ff819eb996c0196f0e8317ded14ec4e417ae678efb8fb6a",
    "Reception": "2dce046fc9faacb0d06d0e81a24c86bcaea74fa7737b3f0d06abb6c1e6883181",
}

# Resolved from this file, not from the caller's cwd. When DIR was relative,
# running from scripts/ found nothing and printed "0 of 7 stems match" - a
# confidently wrong answer that reads exactly like total drift. A checker that
# can report catastrophe because of your working directory is worse than none.
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DIR = os.path.join(REPO, "apps-script", "governor-page-api")

if not os.path.isdir(DIR):
    raise SystemExit(f"baseline dir not found: {DIR} - refusing to report a count")

present = {}
if os.path.isdir(DIR):
    for fn in sorted(os.listdir(DIR)):
        path = os.path.join(DIR, fn)
        if os.path.isfile(path):
            present[os.path.splitext(fn)[0]] = fn

mismatches = 0
for stem in sorted(DIGESTS_V31):
    fn = present.get(stem)
    if fn is None:
        print(f"{stem:14} ABSENT from {DIR} - reporting explicitly, not omitting")
        mismatches += 1
        continue
    path = os.path.join(DIR, fn)
    if stem in JSON_STEMS:
        # utf-8-sig: Apps Script may hand back a BOM, which is not drift either.
        with open(path, encoding="utf-8-sig") as fh:
            try:
                parsed = json.load(fh)
            except ValueError as exc:
                mismatches += 1
                print(f"{stem:14} UNPARSEABLE ({fn}) - {exc}")
                continue
        got = hashlib.sha256(canonical(parsed).encode("utf-8")).hexdigest()
        how = "canonical JSON"
    else:
        with open(path, "rb") as fh:
            data = fh.read().replace(b"\r\n", b"\n")
        got = hashlib.sha256(data).hexdigest()
        how = "raw bytes, CRLF normalised"
    want = DIGESTS_V31[stem]
    if got == want:
        print(f"{stem:14} MATCH    ({fn}, {how})")
    else:
        mismatches += 1
        print(f"{stem:14} MISMATCH ({fn}, {how})")
        print(f"{'':14}   deployed v31 {want}")
        print(f"{'':14}   repo baseline {got}")

extra = sorted(set(present) - set(DIGESTS_V31))
if extra:
    print(f"\nfiles in the repo dir not covered by the v31 fingerprint: {extra}")

print(f"\n{len(DIGESTS_V31) - mismatches} of {len(DIGESTS_V31)} stems match deployed v31")
