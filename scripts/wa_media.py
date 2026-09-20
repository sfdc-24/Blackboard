#!/usr/bin/env python3
"""Fetch the WhatsApp media the board only ever recorded as an id.

WHY IT EXISTS
-------------
On 2026-09-19 between 22:18 and 23:06 Mr Salam sent 47 media messages. Every
one of them landed on the board as this, and nothing more:

    WA-MEDIA|type=image|media=2203134563581011

Forty-six were photographs of the ASQ Toronto conference he was sitting in.
The forty-seventh, at 23:06, was an eighteen-second VOICE NOTE addressed to
claude-code-cli by name, asking a direct question and offering to open
Pipedream. It was read seventeen hours later, by hand, only because someone
went looking for why the board rows were empty.

The inbound pipeline records the id and stops. No agent could see the content,
so a message from him looked identical to no message at all - the same failure
as the twelve unread PUBLIC_INBOX messages and the WhatsApp poller that matched
only one prefix. **A channel that cannot be read is not a channel.**

WHAT IT DOES
------------
    python scripts/wa_media.py list --hours 36
    python scripts/wa_media.py fetch --hours 36 --out media/
    python scripts/wa_media.py fetch --id 2394932544364034 --out media/

`fetch` resolves each id through Meta's Graph media endpoint, downloads the
bytes, names the file by the media type it ACTUALLY is rather than the type the
board row claims, and transcribes audio through Groq when GROQ_API_KEY is
present. The board said `type=image` for the voice note; the bytes began
`OggS`. Trust the bytes.

THE EXPIRY THAT MAKES THIS URGENT
---------------------------------
Meta media URLs expire. A voice note nobody fetches is not merely unread, it
becomes unreadable. Run this on a schedule, not when someone remembers.

D-18: WA_TOKEN / META_TOKEN and GROQ_API_KEY are read from .env at run time.
They never reach argv, a log, or a board row.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

GRAPH_VERSIONS = ("v23.0", "v21.0", "v19.0")
GROQ_URL = "https://api.groq.com/openai/v1/audio/transcriptions"
GROQ_MODEL = "whisper-large-v3"
UA = "sfdc24-fleet/1.0"

# The board row's `type=` is a claim, not a measurement. These are measurements.
MAGIC = [
    (b"OggS", ".ogg", "audio"),
    (b"\xff\xd8\xff", ".jpg", "image"),
    (b"\x89PNG", ".png", "image"),
    (b"RIFF", ".wav", "audio"),
    (b"%PDF", ".pdf", "document"),
    (b"\x1aE\xdf\xa3", ".webm", "audio"),
]


def load_env(path: Path | None = None) -> dict:
    path = path or Path(os.environ.get("BLACKBOARD_ENV") or (REPO / ".env"))
    out = {}
    if not path.is_file():
        return out
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def sniff(blob: bytes, mime: str = "") -> tuple[str, str]:
    """(extension, kind) from the BYTES, falling back to the declared mime."""
    for sig, ext, kind in MAGIC:
        if blob.startswith(sig):
            return ext, kind
    mime = (mime or "").lower()
    if mime.startswith("audio/"):
        return ".ogg", "audio"
    if mime.startswith("image/"):
        return ".jpg", "image"
    return ".bin", "unknown"


def graph_get(url: str, token: str, binary: bool = False):
    req = urllib.request.Request(url, headers={
        "Authorization": "Bearer " + token, "User-Agent": UA})
    with urllib.request.urlopen(req, timeout=120) as r:
        return r.read() if binary else r.read().decode("utf-8", "replace")


def media_meta(media_id: str, token: str):
    """Meta bumps its Graph version; try the newest first and report the last
    error rather than a bare False, because 'not found' and 'wrong version'
    need different fixes."""
    last = ""
    for ver in GRAPH_VERSIONS:
        try:
            return json.loads(graph_get(
                "https://graph.facebook.com/%s/%s" % (ver, media_id), token))
        except urllib.error.HTTPError as ex:
            last = "HTTP %s on %s: %s" % (
                ex.code, ver, ex.read().decode("utf-8", "replace")[:200])
    raise SystemExit("could not resolve media %s - %s" % (media_id, last))


def transcribe(path: Path, key: str, vocab: str) -> str:
    audio = path.read_bytes()
    boundary = "----%s" % uuid.uuid4().hex
    parts = []

    def field(name, value):
        parts.append(("--%s\r\n" % boundary).encode())
        parts.append(('Content-Disposition: form-data; name="%s"\r\n\r\n' % name).encode())
        parts.append(("%s\r\n" % value).encode())

    field("model", GROQ_MODEL)
    field("language", "en")
    field("response_format", "json")
    field("temperature", "0")
    field("prompt", vocab)
    parts.append(("--%s\r\n" % boundary).encode())
    parts.append(('Content-Disposition: form-data; name="file"; filename="%s"\r\n'
                  % path.name).encode())
    parts.append(b"Content-Type: audio/ogg\r\n\r\n")
    parts.append(audio)
    parts.append(b"\r\n")
    parts.append(("--%s--\r\n" % boundary).encode())
    req = urllib.request.Request(GROQ_URL, data=b"".join(parts), method="POST", headers={
        "Authorization": "Bearer " + key,
        "Content-Type": "multipart/form-data; boundary=" + boundary,
        # Groq has rejected header-less calls from this box before.
        "User-Agent": UA,
    })
    with urllib.request.urlopen(req, timeout=300) as r:
        return json.loads(r.read().decode("utf-8", "replace")).get("text", "").strip()


VOCAB = (
    "A voice message about the SFDC24 project: sfdc24.com, Salesforce, "
    "Blackboard, the board, agents, Claude Code CLI, Grok, Grok Bot, Gemini, "
    "Foundry, Codex, Cursor, Copilot, Pipedream, WhatsApp, Zoom, leads, "
    "Web-to-Lead, pull requests, the fleet, the VM, Azure, and the laptop."
)


def media_rows(hours: float):
    import bus  # imported late: `list`/`fetch --id` must work without a bus
    env = bus.load_env()
    since = (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime(
        "%Y-%m-%dT%H:%M:%SZ")
    obj = bus.read_rows(env, since=since, limit=600)
    out = []
    for row in obj["rows"]:
        c = list(row) + [""] * 6
        payload = str(c[5])
        if str(c[2]).strip().lower() != "whatsapp" or not payload.startswith("WA-MEDIA"):
            continue
        mid = re.search(r"media=([0-9]+)", payload)
        kind = re.search(r"type=([a-z]+)", payload)
        out.append({"row_id": str(c[0]), "ts": str(c[1]),
                    "claimed": kind.group(1) if kind else "?",
                    "id": mid.group(1) if mid else None})
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("list", "fetch"):
        p = sub.add_parser(name)
        p.add_argument("--hours", type=float, default=36.0)
        p.add_argument("--id", action="append", dest="ids")
        if name == "fetch":
            p.add_argument("--out", default=str(REPO / "media"))
            p.add_argument("--no-transcribe", action="store_true")
    args = ap.parse_args(argv)

    env = load_env()
    token = env.get("WA_TOKEN") or env.get("META_TOKEN")
    if not token:
        raise SystemExit("WA_TOKEN / META_TOKEN missing from .env")

    if args.ids:
        items = [{"row_id": "-", "ts": "-", "claimed": "?", "id": i} for i in args.ids]
    else:
        items = media_rows(args.hours)

    if args.cmd == "list":
        print("%d media rows" % len(items))
        for it in items:
            print("  %-14s %s  claimed=%-11s id=%s"
                  % (it["row_id"][:14], it["ts"][:19], it["claimed"], it["id"] or "NONE"))
        return 0

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    groq = env.get("GROQ_API_KEY", "")
    saved = skipped = 0
    for it in items:
        if not it["id"]:
            print("  %s %s  NO MEDIA ID in the row - nothing to fetch"
                  % (it["row_id"][:14], it["ts"][:19]))
            skipped += 1
            continue
        meta = media_meta(it["id"], token)
        blob = graph_get(meta["url"], token, binary=True)
        ext, kind = sniff(blob, meta.get("mime_type", ""))
        path = out / (it["id"] + ext)
        path.write_bytes(blob)
        saved += 1
        note = ""
        if kind != it["claimed"] and it["claimed"] != "?":
            note = "  (row claimed %s, bytes say %s)" % (it["claimed"], kind)
        print("  saved %s  %d bytes%s" % (path.name, len(blob), note))
        if kind == "audio" and groq and not args.no_transcribe:
            text = transcribe(path, groq, VOCAB)
            path.with_suffix(".txt").write_text(text, encoding="utf-8")
            print("    TRANSCRIPT: %s" % text)
    print("saved=%d skipped=%d into %s" % (saved, skipped, out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
