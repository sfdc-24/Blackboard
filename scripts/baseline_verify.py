#!/usr/bin/env python3
"""Compare apps-script/governor-page-api/ against the published v31 digests.

READ ONLY. Source files are compared as bytes with every CRLF replaced by LF,
by filename STEM, so the repo's .gs against Apps Script's .js is not drift.

appsscript.json is NOT compared as bytes. Its six oauthScopes can be identical
while the bytes differ purely in key order, and a team that learns to wave
through a mismatch it knows is cosmetic will eventually wave through a real
one. It is parsed and canonicalised first, per GAS-V31-DIGEST-002.

Neither the canonical form NOR the parse is redefined here. A digest holds only
if both sides treat the text identically, and "the JSON" is not self-defining:
sort order, separators and ensure_ascii change the canonical bytes, and a
duplicate key or a NaN changes what "parsed" even means. So this imports the
same parse_json and canonical the build-identity helper uses. Importing one and
not the other was the P2 defect codex-site-resume found in the first version of
this fix: json.load silently keeps the LAST duplicate key, so a manifest that
the deployment-identity path REFUSES could still be reported MATCH here. A
checker that accepts what the deploy path rejects is worse than no checker.
"""
import hashlib
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from gas_build_identity import canonical, parse_json  # noqa: E402  (path set above)

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

# Resolved from this file, not from the caller's cwd. When it was relative,
# running from scripts/ found nothing and printed "0 of 7 stems match" - a
# confidently wrong answer that reads exactly like total drift. A checker that
# can report catastrophe because of your working directory is worse than none.
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_DIR = os.path.join(REPO, "apps-script", "governor-page-api")


def digest_of(path, stem):
    """Return (hexdigest, how). Raises ValueError if a JSON stem is not clean."""
    if stem in JSON_STEMS:
        # utf-8-sig: Apps Script may hand back a BOM, which is not drift either.
        with open(path, encoding="utf-8-sig") as fh:
            text = fh.read()
        # Strict: rejects duplicate keys and NaN/Infinity, exactly as the
        # deployment identity path does. Do not relax this to json.loads.
        parsed = parse_json(text)
        return hashlib.sha256(canonical(parsed).encode("utf-8")).hexdigest(), "canonical JSON"
    with open(path, "rb") as fh:
        data = fh.read().replace(b"\r\n", b"\n")
    return hashlib.sha256(data).hexdigest(), "raw bytes, CRLF normalised"


def verify(dirpath):
    """Compare dirpath against DIGESTS_V31. Returns (rows, mismatches)."""
    present = {}
    for fn in sorted(os.listdir(dirpath)):
        if os.path.isfile(os.path.join(dirpath, fn)):
            present[os.path.splitext(fn)[0]] = fn

    rows, mismatches = [], 0
    for stem in sorted(DIGESTS_V31):
        fn = present.get(stem)
        if fn is None:
            rows.append((stem, None, "ABSENT", None, None, None))
            mismatches += 1
            continue
        try:
            got, how = digest_of(os.path.join(dirpath, fn), stem)
        except ValueError as exc:
            rows.append((stem, fn, "UNPARSEABLE", None, None, str(exc)))
            mismatches += 1
            continue
        want = DIGESTS_V31[stem]
        status = "MATCH" if got == want else "MISMATCH"
        if status == "MISMATCH":
            mismatches += 1
        rows.append((stem, fn, status, want, got, how))
    return rows, mismatches


def main(argv):
    dirpath = argv[1] if len(argv) > 1 else DEFAULT_DIR
    if not os.path.isdir(dirpath):
        print(f"baseline dir not found: {dirpath} - refusing to report a count")
        return 2

    rows, mismatches = verify(dirpath)
    for stem, fn, status, want, got, how in rows:
        if status == "ABSENT":
            print(f"{stem:14} ABSENT from {dirpath} - reporting explicitly, not omitting")
        elif status == "UNPARSEABLE":
            print(f"{stem:14} UNPARSEABLE ({fn}) - {how}")
            print(f"{'':14}   the deploy path rejects this text too; treat as NOT verified")
        elif status == "MATCH":
            print(f"{stem:14} MATCH    ({fn}, {how})")
        else:
            print(f"{stem:14} MISMATCH ({fn}, {how})")
            print(f"{'':14}   deployed v31 {want}")
            print(f"{'':14}   repo baseline {got}")

    present = {os.path.splitext(f)[0] for f in os.listdir(dirpath)
               if os.path.isfile(os.path.join(dirpath, f))}
    extra = sorted(present - set(DIGESTS_V31))
    if extra:
        print(f"\nfiles in the dir not covered by the v31 fingerprint: {extra}")

    print(f"\n{len(DIGESTS_V31) - mismatches} of {len(DIGESTS_V31)} stems match deployed v31")
    return 0 if mismatches == 0 else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
