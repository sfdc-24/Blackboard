#!/usr/bin/env python3
"""Grok WhatsApp inbox peek (stdlib). once = read without acknowledging.
Does NOT send WhatsApp. Does NOT --ack. Prints counts + safe previews only.
Never prints tokens.
"""
from __future__ import annotations
import argparse, json, os, sys, urllib.error, urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import bus as busmod  # noqa: E402

def load_env():
    return busmod.load_env()

def graph_check(env):
    token = env.get("WA_TOKEN") or env.get("META_TOKEN") or ""
    if token.lower().startswith("bearer "):
        token = token[7:].strip()
    pnid = env.get("WA_PHONE_NUMBER_ID") or ""
    ver = env.get("WA_GRAPH_VERSION") or "v22.0"
    if not token or not pnid:
        return {"ok": False, "reason": "no_wa_creds"}
    url = f"https://graph.facebook.com/{ver}/{pnid}?fields=display_phone_number,verified_name,quality_rating"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            body = json.loads(r.read().decode())
        return {
            "ok": True,
            "verified_name": body.get("verified_name"),
            "quality_rating": body.get("quality_rating"),
            "display_phone_number": body.get("display_phone_number"),
        }
    except urllib.error.HTTPError as e:
        return {"ok": False, "reason": f"http_{e.code}"}
    except Exception as e:
        return {"ok": False, "reason": type(e).__name__}

def board_peek(env, last=30):
    obj = busmod.read_board(env)
    rows = obj.get("rows") or []
    data = rows[1:] if rows and isinstance(rows[0], list) else rows
    needles = ("whatsapp", "wamid", "grok", "public_inbox", "pipedream", "wa ")
    hits = []
    for r in data[-400:]:
        if not isinstance(r, list) or len(r) < 6:
            continue
        blob = " ".join(str(c) for c in r[:10]).lower()
        if any(n in blob for n in needles):
            hits.append(r)
    out = []
    for r in hits[-last:]:
        payload = str(r[5])[:160].replace("\n", " ")
        out.append({
            "ts": str(r[1])[:19] if len(r) > 1 else "",
            "from": str(r[2]) if len(r) > 2 else "",
            "to": str(r[3]) if len(r) > 3 else "",
            "gist": str(r[8])[:80] if len(r) > 8 else "",
            "payload_preview": payload,
        })
    return {"total_board_rows": max(0, len(data)), "wa_related": len(hits), "tail": out}

def cmd_once(args):
    env = load_env()
    g = graph_check(env)
    print("GRAPH", "ok" if g.get("ok") else "skip",
          f"name={g.get('verified_name','')}" if g.get("ok") else f"reason={g.get('reason')}",
          f"quality={g.get('quality_rating','')}" if g.get("ok") else "")
    try:
        peek = board_peek(env, last=args.last)
        print(f"BOARD rows={peek['total_board_rows']} wa_related={peek['wa_related']} showing={len(peek['tail'])}")
        for row in peek["tail"][-args.last:]:
            print(f"  {row['ts']} | {row['from']} -> {row['to']} | {row['gist'] or row['payload_preview'][:100]}")
        print("ACK skipped (once without --ack)")
        return 0
    except SystemExit as e:
        print("BOARD fail", e)
        return 1
    except Exception as e:
        print("BOARD err", type(e).__name__)
        return 1

def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    o = sub.add_parser("once")
    o.add_argument("--last", type=int, default=8)
    args = ap.parse_args()
    if args.cmd == "once":
        raise SystemExit(cmd_once(args))

if __name__ == "__main__":
    main()
