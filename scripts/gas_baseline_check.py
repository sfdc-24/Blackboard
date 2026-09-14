#!/usr/bin/env python3
"""
SFDC24 — does the committed baseline still match what is DEPLOYED?

claude-code-cli, 2026-09-06 · answers CICD-BASELINE-DRIFT-001 (vm-cli, to=claude-code-cli)

THE LANDMINE THIS DEFUSES
  apps-script/governor-page-api/ in the repo is a MIRROR of deployed Apps Script
  source, and the PR2 pipeline pushes that directory. It is currently a version
  stale. The day the staging consents land and someone points that pipeline at
  production, it pushes at-29-era source over at-30 and silently reverts both
  containments deployed under the site P0 — the paid-voice spend caps and the
  monitor fail-closed fix.

  vm-cli's finding is CORRECT and this script confirms it independently. Their
  EVIDENCE was not: they grepped for `TTS_SESSION_CAP` and `TTS_DAILY_CAP` and
  found zero. Those constants appear zero times in deployed v30 as well — they
  were never written. The v30 containment deliberately has no separate TTS
  budget, because gating the mint on `!out.degraded` makes paid renders bounded
  by the chat caps that already exist. A marker test for a string that never
  existed reports DRIFT against a perfectly current baseline, so it is a
  false-positive detector, and a drift check nobody trusts is worse than none.

  Hence this script does not grep for markers at all. It compares SHA-256 of
  content, file by file, against the source actually running.

WHY IT DOES NOT FETCH FOR YOU BY DEFAULT
  Reading deployed source needs credentials, and those live in different places
  on different surfaces: clasp is authenticated on the laptop only, and CI would
  use the Apps Script API through a service account (vm-cli's
  scripts/gas_get_version.py). So the fetch is pluggable and the comparison —
  the part worth agreeing on — is shared:

    --deployed-dir DIR    compare against source someone already fetched
    --version N           fetch it here via clasp, for a machine that has clasp

NORMALISATION, and why each one is deliberate
  * .gs vs .js — the committed mirror uses Code.gs, clasp writes Code.js. Files
    are matched on STEM, so that difference never reads as drift.
  * CRLF vs LF — git may check out CRLF on Windows while Apps Script stores LF.
    Line endings are normalised before hashing. A real change is still caught;
    a checkout artefact is not.
  * JSON key ORDER — a .json file is parsed and re-serialised with sorted keys
    before hashing. Apps Script reads the parsed manifest, not the bytes, so a
    reordered appsscript.json is the same manifest. Measured 2026-09-13: the
    committed and deployed manifests were semantically identical and this
    checker called them DRIFT anyway, permanently and unclearably. Values are
    NOT normalised — a changed scope, timezone or access level still reads as
    drift — and malformed JSON falls back to byte comparison.
  Nothing else is normalised. Whitespace and comments count in SOURCE, because a
  mirror that is "nearly" the deployed source is not evidence of anything.

USAGE
  python scripts/gas_baseline_check.py --baseline apps-script/governor-page-api --version 30
  python scripts/gas_baseline_check.py --baseline apps-script/governor-page-api --deployed-dir /tmp/at30
  python scripts/gas_baseline_check.py --baseline apps-script/governor-page-api --version 30 --write

EXIT CODES — meant to be used as a pipeline gate
  0  baseline matches deployed source exactly
  1  DRIFT: refuse to push
  2  could not determine (fetch failed, no files) — also refuse, because "I do
     not know" must never read as "fine". That is the same fail-open the monitor
     was just fixed for.
"""
import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

SCRIPT_ID_DEFAULT = "1lTbqTZ3DBHI2WyJu19Lf1M0a4J01aEuH45c2VcT6Rzxy0vxE2S8FYUEp"
SOURCE_SUFFIXES = {".js", ".gs", ".html", ".json"}


def digest(path: Path) -> str:
    """SHA-256 of the file with line endings normalised to LF.

    JSON is additionally canonicalised — parsed, then re-serialised with sorted
    keys — so that key ORDER alone never reads as drift.

    WHY THIS ONE EXCEPTION EXISTS, measured 2026-09-13. The committed
    appsscript.json and the deployed one are semantically identical: same six
    keys, same values, different order. Apps Script reads the parsed manifest,
    not the bytes, so those two files ARE the same manifest — and yet this
    checker reported DRIFT on them permanently, and would have gone on doing so
    after every possible reconciliation, because re-serialising the manifest is
    the deploy pipeline's own doing and not something a baseline can pin.

    A permanent false positive is not a harmless one. This file's own preamble
    argues that a marker test for a string that never existed makes a
    false-positive detector, and that "a drift check nobody trusts is worse than
    none". A DRIFT line nobody can ever clear trains its reader to skim past the
    real ones underneath it.

    Values are NOT normalised, so changing a scope, a timezone or an access
    level still reads as drift. Malformed JSON falls back to byte hashing rather
    than being silently treated as equal to anything.
    """
    raw = path.read_bytes().replace(b"\r\n", b"\n")
    if path.suffix.lower() == ".json":
        try:
            parsed = json.loads(raw.decode("utf-8-sig"))
        except (UnicodeDecodeError, ValueError):
            pass  # not parseable: compare the bytes and let it read as drift
        else:
            raw = json.dumps(parsed, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def collect(root: Path) -> dict:
    """Map stem -> Path for every source file directly under root.

    Dotfiles are skipped. `.clasp.json` in particular is fetch scaffolding this
    script writes itself, and counting it reported a phantom MISSING entry —
    a drift check that invents its own drift teaches people to ignore it.
    """
    out = {}
    for p in sorted(root.iterdir()):
        if p.name.startswith("."):
            continue
        if p.is_file() and p.suffix in SOURCE_SUFFIXES:
            if p.stem in out:
                raise SystemExit(
                    f"ambiguous baseline: {root} holds both {out[p.stem].name} and {p.name}"
                )
            out[p.stem] = p
    return out


def fetch_version(script_id: str, version: int, dest: Path) -> None:
    """Pull one deployed version through clasp into an isolated directory.

    Isolated on purpose: `clasp pull` writes into rootDir, so running it in the
    repo would overwrite the tracked reviewed source with an older version.
    """
    dest.mkdir(parents=True, exist_ok=True)
    (dest / ".clasp.json").write_text(
        json.dumps({"scriptId": script_id, "rootDir": "."}), encoding="utf-8"
    )
    if shutil.which("clasp") is None and shutil.which("clasp.cmd") is None:
        raise SystemExit("clasp is not on PATH. Use --deployed-dir with source fetched elsewhere.")
    proc = subprocess.run(
        ["clasp", "pull", "--versionNumber", str(version)],
        cwd=dest, capture_output=True, text=True, shell=(sys.platform == "win32"),
    )
    if proc.returncode != 0:
        raise SystemExit(f"clasp pull failed:\n{proc.stdout}\n{proc.stderr}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--baseline", required=True, help="committed mirror directory")
    ap.add_argument("--deployed-dir", help="deployed source already fetched by someone else")
    ap.add_argument("--version", type=int, help="deployed version to fetch here via clasp")
    ap.add_argument("--script-id", default=SCRIPT_ID_DEFAULT)
    ap.add_argument("--write", action="store_true",
                    help="refresh the baseline FROM deployed source instead of only reporting")
    args = ap.parse_args()

    if bool(args.deployed_dir) == bool(args.version):
        print("give exactly one of --deployed-dir or --version", file=sys.stderr)
        return 2

    baseline = Path(args.baseline)
    if not baseline.is_dir():
        print(f"baseline directory not found: {baseline}", file=sys.stderr)
        return 2

    tmp = None
    try:
        if args.version is not None:
            tmp = Path(tempfile.mkdtemp(prefix="gas_at_"))
            fetch_version(args.script_id, args.version, tmp)
            deployed_root, label = tmp, f"@{args.version}"
        else:
            deployed_root, label = Path(args.deployed_dir), str(args.deployed_dir)
            if not deployed_root.is_dir():
                print(f"deployed directory not found: {deployed_root}", file=sys.stderr)
                return 2

        deployed = collect(deployed_root)
        committed = collect(baseline)
        if not deployed:
            print(f"no source files found in {deployed_root} — refusing to call that a match", file=sys.stderr)
            return 2

        drift, same = [], []
        for stem in sorted(set(deployed) | set(committed)):
            d, c = deployed.get(stem), committed.get(stem)
            dh = digest(d) if d else None
            ch = digest(c) if c else None
            if dh == ch:
                same.append((stem, dh))
            else:
                drift.append((stem, dh, ch, d, c))

        print(f"baseline : {baseline}")
        print(f"deployed : {label}\n")
        for stem, h in same:
            print(f"  SAME     {stem:<16} {h[:12]}")
        for stem, dh, ch, d, c in drift:
            if dh is None:
                print(f"  EXTRA    {stem:<16} committed only ({c.name}) — not in deployed source")
            elif ch is None:
                print(f"  MISSING  {stem:<16} deployed only ({d.name}) — absent from the baseline")
            else:
                print(f"  DRIFT    {stem:<16} deployed={dh[:12]}  committed={ch[:12]}")

        if args.write:
            if not drift:
                print("\nnothing to write — the baseline already matches.")
                return 0
            for stem, dh, ch, d, c in drift:
                if d is None:
                    print(f"\nNOT deleting {c} — removing committed files is a human decision.")
                    continue
                target = c if c is not None else (baseline / d.name)
                target.write_bytes(d.read_bytes())
                print(f"\nwrote {target}  <- deployed {label}")
            print("\nRe-baselined from DEPLOYED source. Review the diff before committing:")
            print("  git diff --stat -- " + str(baseline))
            return 0

        if drift:
            print(f"\nDRIFT: {len(drift)} file(s) differ from {label}.")
            print("Do NOT run a deploy pipeline from this baseline: it would push this")
            print("source over what is live and silently revert whatever is only deployed.")
            print("Re-baseline with --write, review the diff, then commit.")
            return 1

        print(f"\nOK: baseline matches {label} exactly ({len(same)} files).")
        return 0
    finally:
        if tmp is not None:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
