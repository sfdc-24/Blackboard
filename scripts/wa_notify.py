#!/usr/bin/env python3
"""Send him a WhatsApp message, in Python, with the sender's name on it.

WHY THIS EXISTS RATHER THAN scripts/wa_send.py
    There was already a Python sender. It is not a replacement for
    wa_notify.ps1 and swapping to it would have broken a standing instruction:

      - wa_send.py does NOT write the "[KIND - tag]" prefix. On 2026-09-07 he
        asked, by name, that every message identify which instance sent it,
        because several surfaces write to him and a stateless gateway answers
        within two seconds under the same channel. A message he cannot attribute
        is worth very little.
      - it reads WA_TOKEN; wa_notify.ps1 reads META_TOKEN. Both exist in .env.
        Two senders reading two different credentials, with nothing saying which
        is authoritative, is a trap waiting for whoever rotates one of them.
      - it needs `requests`. Nothing else on the fleet path does, and an import
        that is absent in a cloud sandbox is a dependency this migration exists
        to remove.

    So this is the PowerShell script's contract, in stdlib Python: same prefix,
    same caps, same Graph version, same Bearer gotcha.

WHY IT IS A MIGRATION STEP
    Dependency 4 of docs/OPENAI-CLOUD-MIGRATION.md, "Windows-only actions":
    "WhatsApp receipt delivery still invokes powershell.exe and
    wa_notify.ps1; fleet_agent.py still shells out to bus.ps1". A cloud
    runtime has no PowerShell, so every caller that shells out is a caller
    that cannot move. #183 closed the fleet_agent half; this is the other.

    That doc is not on main yet - it arrives with codex's PR #172, branch
    codex/openai-cloud-migration. Named here so the citation is findable
    before it merges rather than reading as a broken path.

WHAT IT DOES NOT DO
    Buttons, list menus and reply-to quoting. wa_notify.ps1 keeps those and
    stays the tool for a one-tap decision card. This covers the plain-text
    receipt path, which is what the fleet actually shells out for.

    python scripts/wa_notify.py --kind STATUS --tag wa-poller --text "..."
    python scripts/wa_notify.py --text-file msg.txt --dry-run
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Matches the deployed Pipedream reply step. DOCTRINE V2 says v22.0 and the
# V3.1 bus doc says v24.0; the deployed thing wins until that is ruled on.
GRAPH_VERSION = "v22.0"

# Meta's text body limit is 4096. Leave room for the prefix line rather than
# discovering the edge as a 400 with the message never delivered.
MAX_CHARS = 3800

KINDS = ("BLOCKED", "ANDON", "STATUS", "DONE", "ASK")

# The .ps1 refuses a tag outside this shape, and it is not cosmetic. The tag
# is written into the identity line, so a tag carrying a newline and a
# bracket composes a SECOND prefix and the message he reads is attributed to
# an instance that never sent it. argparse constrains the CLI's --kind;
# nothing constrained either field for a caller using notify(), which is now
# every caller that used to shell out.
TAG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


ENV_KEYS = ("META_TOKEN", "WA_TOKEN", "WA_PHONE_NUMBER_ID", "WA_TO",
            "WA_GRAPH_VERSION")


def load_env(path: Path | None = None) -> dict:
    explicit = path is not None
    path = path or (ROOT / ".env")
    if not path.is_file():
        # A cloud runtime injects these from Secret Manager and has no .env.
        # Only when no file was asked for by name: a named file that is
        # missing is a mistake, not a hint to look elsewhere.
        injected = {k: os.environ[k] for k in ENV_KEYS if os.environ.get(k)}
        if not explicit and injected:
            return injected
        raise SystemExit("env file not found: %s" % path)
    env = {}
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        m = re.match(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$", line)
        if m:
            env[m.group(1)] = m.group(2).strip('"').strip("'")
    return env


def credentials(env: dict) -> tuple[str, str, str, str]:
    """Returns (token, phone_number_id, recipient, which_var).

    META_TOKEN first, because that is what wa_notify.ps1 uses and it is the one
    that has been kept working. WA_TOKEN is the fallback so a box that only has
    the other name still sends, and the caller is told which was used - a silent
    fallback to a stale second credential is how a 190 gets misread as expiry.
    """
    which = "META_TOKEN"
    token = env.get("META_TOKEN") or ""
    if not token.strip():
        token, which = env.get("WA_TOKEN") or "", "WA_TOKEN"

    # GOTCHA, cost 20 minutes on 2026-09-06: the stored value may already carry
    # the scheme. Sending "Bearer Bearer ..." returns HTTP 401 code 190
    # Authentication Error, which reads exactly like an expired token and is not.
    token = re.sub(r"^\s*[Bb]earer\s+", "", token).strip()
    if not token:
        raise SystemExit(
            "no WhatsApp token: set META_TOKEN (preferred) or WA_TOKEN in .env")

    pnid = str(env.get("WA_PHONE_NUMBER_ID", "")).strip()
    recipient = re.sub(r"[^\d]", "", str(env.get("WA_TO", "")))
    if not re.fullmatch(r"\d{5,30}", pnid):
        raise SystemExit("WA_PHONE_NUMBER_ID must contain digits only")
    if not re.fullmatch(r"\d{6,20}", recipient):
        raise SystemExit("WA_TO must contain one 6-20 digit operator number")
    return token, pnid, recipient, which


def compose(text: str, kind: str, tag: str, raw: bool = False) -> str:
    """The prefix is the point of this function.

    ASCII separator deliberately: Windows PowerShell 5.1 reads its own copy of
    this rule as ANSI, and a UTF-8 middle dot there left the machine name as
    mojibake in his chat. Keeping the two identical matters more than the glyph.
    """
    body = text.rstrip("\r\n")
    if not raw:
        if kind not in KINDS:
            raise SystemExit("kind must be one of %s, not %r"
                             % (", ".join(KINDS), kind))
        if not TAG_RE.match(tag or ""):
            raise SystemExit("tag %r is not one safe token; it would forge "
                             "the identity line" % (tag,))
        body = "[%s - %s]\n%s" % (kind, tag, body)
    if len(body) > MAX_CHARS:
        body = body[: MAX_CHARS - 3] + "..."
    return body


def send(body: str, token: str, pnid: str, recipient: str, timeout: int = 60) -> dict:
    url = "https://graph.facebook.com/%s/%s/messages" % (GRAPH_VERSION, pnid)
    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": recipient,
        "type": "text",
        "text": {"preview_url": False, "body": body},
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={"Authorization": "Bearer " + token,
                 "Content-Type": "application/json"},
    )
    # NOT retried. A send that fails after Meta accepted it would arrive twice,
    # and he reads this channel. One attempt, and the caller is told the truth.
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return {"ok": True, "status": r.getcode(),
                    "body": r.read().decode("utf-8", "replace")}
    except urllib.error.HTTPError as e:
        return {"ok": False, "status": e.code,
                "body": (e.read().decode("utf-8", "replace") if e.fp else "")}
    except Exception as exc:  # noqa: BLE001 - a transport failure is not a refusal
        return {"ok": False, "status": 0, "body": "%s: %s" % (type(exc).__name__, exc)}


def notify(text: str, kind: str = "BLOCKED", tag: str = "claude-code-cli",
           raw: bool = False, dry_run: bool = False,
           env_file: str | None = None) -> tuple[bool, str]:
    """One call for other Python to use instead of shelling out."""
    env = load_env(Path(env_file) if env_file else None)
    token, pnid, recipient, which = credentials(env)
    body = compose(text, kind, tag, raw)
    if dry_run:
        return True, "DRY RUN (%s, %d chars to %s):\n%s" % (which, len(body), recipient, body)
    res = send(body, token, pnid, recipient)
    return bool(res["ok"]), "HTTP %s %s" % (res["status"], res["body"][:300])


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--text")
    ap.add_argument("--text-file")
    ap.add_argument("--tag", default="claude-code-cli")
    ap.add_argument("--kind", default="BLOCKED", choices=KINDS)
    ap.add_argument("--raw", action="store_true",
                    help="send without the identity prefix. Do not use for "
                         "anything he reads.")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--env-file")
    args = ap.parse_args()

    if args.text_file:
        text = Path(args.text_file).read_text(encoding="utf-8", errors="replace")
    elif args.text:
        text = args.text
    else:
        text = sys.stdin.read()
    if not text.strip():
        print("nothing to send", file=sys.stderr)
        return 2

    ok, detail = notify(text, args.kind, args.tag, args.raw, args.dry_run,
                        args.env_file)
    print(detail)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
