#!/usr/bin/env python3
"""Build identity for the bus deployment: stamp it before a push, read it back after a redeploy (case 00001051).

GTM V4 Risk 1: web-editor deploys republished old versions twice (about 8 hours of recovery), and nothing could tell,
because the live ping carried no version. The bus ping now returns `build`, the commit stamped into gas-bus/Build.js.

    python scripts/gas_build_readback.py --stamp <40-hex commit>     before `clasp push`: writes gas-bus/Build.js
    python scripts/gas_build_readback.py --expect <40-hex commit>    after `clasp redeploy`: the live ping must report it

--expect exits 0 only when the live ping reports exactly that commit; anything else (an old commit, 'unset', no build
field because the deployment predates this change, an unreachable bus) exits 1 and says which, so a deploy script can
roll back on it. Nothing secret is sent or printed: ping needs no secret.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

HEX40 = re.compile(r"^[0-9a-f]{40}$")
BUILD_FILE = Path(__file__).resolve().parents[1] / "gas-bus" / "Build.js"
HEADER = ("// Build identity for the live read-back (case 00001051). scripts/gas_build_readback.py --stamp writes the\n"
          "// deploying commit here before clasp push; the no-secret ping returns it, so a stale republish is detectable.\n")


def stamp(commit: str, path: Path = BUILD_FILE) -> None:
    if not HEX40.match(commit or ""):
        raise SystemExit("--stamp needs the full 40-hex commit")
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(HEADER + "var BUILD_SHA = '%s';\n" % commit)


def live_build(fetch_ping) -> tuple[str | None, str]:
    """(build, why): the live ping's build field, or None with the reason it could not be read."""
    try:
        raw = fetch_ping()
    except Exception as error:                                           # noqa: BLE001 - reported, never raised
        return None, "bus unreachable (%s)" % type(error).__name__
    try:
        body = json.loads(raw) if isinstance(raw, (str, bytes)) else dict(raw)
    except (ValueError, TypeError):
        return None, "ping did not return JSON"
    if not body.get("ok"):
        return None, "ping not ok"
    if "build" not in body:
        return None, "the live deployment reports no build (it predates the build stamp)"
    return str(body["build"]), "live build %s" % str(body["build"])[:12]


def check(expect: str, fetch_ping) -> tuple[bool, str]:
    if not HEX40.match(expect or ""):
        return False, "--expect needs the full 40-hex commit"
    build, why = live_build(fetch_ping)
    if build is None:
        return False, why
    if build != expect:
        return False, "the live bus runs %s, not %s - stale or wrong deployment" % (build[:12], expect[:12])
    return True, "the live bus runs %s" % expect[:12]


def _default_fetch():
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from bus import fetch, load_env  # noqa: PLC0415
    env = load_env()
    url = env.get("bus_url") or env.get("BUS_URL")
    return fetch(url + ("&" if "?" in url else "?") + "action=ping")


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) == 2 and argv[0] == "--stamp":
        stamp(argv[1])
        print("stamped gas-bus/Build.js with %s" % argv[1][:12])
        return 0
    if len(argv) == 2 and argv[0] == "--expect":
        ok, why = check(argv[1], _default_fetch)
        print(("PASS: " if ok else "FAIL: ") + why)
        return 0 if ok else 1
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main())
