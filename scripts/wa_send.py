#!/usr/bin/env python3
"""Send a WhatsApp text from the SFDC24 business number to Mr. Salam (Meta Cloud API).

The outbound path the DOCTRINE (D-27) and ARCHITECTURE.md describe: a plain POST to
  https://graph.facebook.com/<ver>/<PHONE_NUMBER_ID>/messages
with {messaging_product:"whatsapp", to, type:"text", text:{body}}.

Until now the ONLY holder of that call was the Pipedream step send_whatsapp_reply,
which fires only in reply to an inbound WhatsApp message. Nothing polls the board,
so agent-authored rows never reach WhatsApp (chat-mobile, PIPEDREAM-LANE-001).
This script is the missing agent-side sender. It is deliberately dumb: no model,
no board read, one message, one receipt.

D-18: the token comes from .env (gitignored), never from source or the board.
  WA_TOKEN            permanent System User token (whatsapp_business_messaging)
  WA_PHONE_NUMBER_ID  business phone number id (default: the SFDC24 number, D-27)
  WA_TO               recipient digits (default: Mr. Salam, from the inbound wamids)
  WA_GRAPH_VERSION    default v22.0 (D-27)

Free-form text is only accepted by Meta inside the 24h window after the
recipient's last inbound message; outside it you need an approved template.

Usage:
  python scripts/wa_send.py --check                # read-only: does the token work?
  python scripts/wa_send.py "hi from vm-cli"       # send, print the wamid receipt
  python scripts/wa_send.py --dry-run "text"       # show the request, send nothing
  python scripts/wa_send.py --file body.txt        # body from a file (multi-line)
"""
import argparse, json, os, sys
import requests

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load_env(path=os.path.join(ROOT, ".env")):
    env = {}
    try:
        with open(path, encoding="utf-8-sig") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
    except FileNotFoundError:
        pass
    return env


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("body", nargs="?", help="message text")
    ap.add_argument("--file", help="read the body from this file")
    ap.add_argument("--to", help="recipient digits (overrides WA_TO)")
    ap.add_argument("--check", action="store_true", help="read-only token/number check, no send")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    env = load_env()
    token = env.get("WA_TOKEN") or os.environ.get("WA_TOKEN", "")
    # The stored value may already carry the scheme. Sending "Bearer Bearer ..."
    # returns HTTP 401 code 190 Authentication Error, which reads exactly like an
    # expired token and is not (claude-code-cli, ESCALATION-CHANNEL-001).
    if token.lower().startswith("bearer "):
        token = token[7:].strip()
    pnid = env.get("WA_PHONE_NUMBER_ID") or "1273365472529201"
    to = args.to or env.get("WA_TO") or "16472183217"
    ver = env.get("WA_GRAPH_VERSION") or "v22.0"
    base = f"https://graph.facebook.com/{ver}/{pnid}"

    if not token and not args.dry_run:
        print("WA_TOKEN is not set in .env — nothing sent. Add the line WA_TOKEN=<system user token> "
              "(the value Pipedream's send_whatsapp_reply step uses for its Graph API call).", file=sys.stderr)
        return 2
    headers = {"Authorization": f"Bearer {token}"}

    if args.check:
        r = requests.get(base, headers=headers, params={"fields": "display_phone_number,verified_name,quality_rating"}, timeout=30)
        print("check:", r.status_code, r.text[:400])
        return 0 if r.ok else 1

    body = open(args.file, encoding="utf-8").read() if args.file else (args.body or "")
    body = body.strip()
    if not body:
        print("empty body", file=sys.stderr)
        return 2
    if len(body) > 4096:
        print(f"body is {len(body)} chars; WhatsApp text limit is 4096", file=sys.stderr)
        return 2

    payload = {"messaging_product": "whatsapp", "recipient_type": "individual", "to": to,
               "type": "text", "text": {"preview_url": False, "body": body}}
    if args.dry_run:
        print("POST", base + "/messages")
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return 0

    r = requests.post(base + "/messages", headers=headers, json=payload, timeout=30)
    try:
        data = r.json()
    except ValueError:
        data = {"raw": r.text[:400]}
    if r.ok and data.get("messages"):
        # The wamid is the receipt. "accepted" means Meta queued it; delivery is a
        # separate status webhook (ISSUE 020 shape: accepted is not delivered).
        print("SENT wamid=" + data["messages"][0].get("id", "?"),
              "status=" + str(data["messages"][0].get("message_status", "accepted")), "to=" + to)
        return 0
    err = data.get("error", data)
    print("SEND FAILED", r.status_code, json.dumps(err)[:600], file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
