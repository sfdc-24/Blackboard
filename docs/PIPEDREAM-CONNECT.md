# Pipedream Connect (SFDC24)

Added for grok-bot / fleet server use against project **proj_5DsGpGe** in **production**.
Quickstart reference: https://pipedream.com/docs/connect/quickstart

## Env (D-18 — local `.env` only, never commit)

```
PIPEDREAM_CLIENT_ID=...
PIPEDREAM_CLIENT_SECRET=...
PIPEDREAM_PROJECT_ID=proj_5DsGpGe
PIPEDREAM_PROJECT_ENVIRONMENT=production
```

## CLI

```powershell
python scripts\pipedream_connect.py check
python scripts\pipedream_connect.py token --external-user-id grok-bot
python scripts\pipedream_connect.py apps --q whatsapp
```

`check` must print `ok: true` before any Connect feature is claimed TESTED.

## What this is / is not

| | |
|---|---|
| **Connect** | OAuth client → Connect API (accounts, actions, proxy, MCP-style tools for end users / agents) |
| **Existing WA path** | Meta webhook → Pipedream workflow (`scripts/pipedream_wa_inbound.js`) → Alpha DB; outbound via `wa_notify.ps1` on the laptop |

Connect does **not** replace the WA inbound workflow. It is the way agents (including grok-bot) authenticate to Pipedream's Connect surface for *new* app integrations under this project.

## Addressing on WhatsApp

Mr. Salam prefixes `Grok`. grok-bot reads `whatsapp` rows on Alpha DB and may send via `wa_notify.ps1` with tag `grok-bot`. Do not treat Pipedream gateway auto-lanes tagged `claude` / `gemini` as fleet peers.

## Security

- Never print client secret or full connect tokens into chat, board rows, or git.
- Use `--print-token` only on a trusted local console.
- Rotate via Pipedream workspace API settings if a secret leaks.

## Free Grok WhatsApp inbox (no model spend)

Inbound still lands via the existing Pipedream WA workflow → Alpha DB (`whatsapp` tag).

```powershell
python scripts\grok_wa_inbox.py list --last 10
python scripts\grok_wa_inbox.py once          # detect new Grok-prefixed rows
python scripts\grok_wa_inbox.py once --ack    # + STATUS ACK via wa_notify.ps1
```

Schedule `scripts\grok_wa_inbox_once.ps1` on the laptop if you want polling without waking Grok Bot (saves plan dollars).
Address messages with a leading `Grok`.

