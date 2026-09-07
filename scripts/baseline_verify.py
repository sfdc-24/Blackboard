#!/usr/bin/env python3
"""Compare apps-script/governor-page-api/ against the published v31 digests.

READ ONLY. Per GAS-V31-DIGEST-001: sha256 of each file's bytes after replacing
every CRLF with LF, compared by filename STEM (so the repo's .gs against Apps
Script's .js is not counted as drift).
"""
import hashlib
import os

DIGESTS_V31 = {
    "appsscript": "46aac4dd90894a0e15f16a027878e978fc21e2fe75b3302e5e525579eb44e5ba",
    "Auth": "1b7fa67196c3c904d3b389138ef8af4efec4e5612982d5134a88b3d82183dfb8",
    "Code": "ff43da1193980e7b43a657df22754f3a64784c49283456c1c00228d83aaaaf1f",
    "Index": "9e98b85129b127bc1aa6414b2e5242d917c7bc122baceaadd23d77016ef9aea7",
    "Monitor": "cc50d7a91c95638701ae9b146de40395b39013201261857411d423ad49674922",
    "PublicInbox": "09d2301c595d54096ff819eb996c0196f0e8317ded14ec4e417ae678efb8fb6a",
    "Reception": "2dce046fc9faacb0d06d0e81a24c86bcaea74fa7737b3f0d06abb6c1e6883181",
}

DIR = os.path.join("apps-script", "governor-page-api")

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
    with open(os.path.join(DIR, fn), "rb") as fh:
        data = fh.read().replace(b"\r\n", b"\n")
    got = hashlib.sha256(data).hexdigest()
    want = DIGESTS_V31[stem]
    if got == want:
        print(f"{stem:14} MATCH    ({fn})")
    else:
        mismatches += 1
        print(f"{stem:14} MISMATCH ({fn})")
        print(f"{'':14}   deployed v31 {want}")
        print(f"{'':14}   repo baseline {got}")

extra = sorted(set(present) - set(DIGESTS_V31))
if extra:
    print(f"\nfiles in the repo dir not covered by the v31 fingerprint: {extra}")

print(f"\n{len(DIGESTS_V31) - mismatches} of {len(DIGESTS_V31)} stems match deployed v31")
