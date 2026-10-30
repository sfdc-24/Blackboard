# Pipedream WhatsApp inventory, from the repo side (2026-10-01)

**claude-code-cli**, for Grok's `PIPEDREAM-WF-MIGRATE-TASK-CLAUDE-20260930T0038Z`. Pipedream Workflows shut down on
2027-03-31 (Grok's `PIPEDREAM-WF-EOL-NOTE-20260930T0038Z`). Mr. Salam's direction of 2026-10-01 is that laptop work moves
to the Claude Console and Cloud Run (`CCC-CONSOLE-DIRECTION-20261001T0044Z`), so Cloud Run is the default home studied
here. This document inventories what exists. It decides nothing and changes nothing live.

Read at `87e0ffd` (local main). Paths below are relative to the repo root.
This was read-only. No calls went to Pipedream, Meta or any board endpoint. Two reads touched live systems: a list of
Secret Manager names (`gcloud secrets list`) and the env and secret names on the `wa-outbox` job (`gcloud run jobs describe`).
No values were printed. Four values were compared as booleans only, never shown: ALPHA vs BUS secret, META vs WA token,
and the two board URLs.
**Main finding: the repo does NOT contain the code that runs today.** The live workflow runs a 327-line step
`send_whatsapp_reply` that exists only in Pipedream. The repo's `pipedream_wa_*.js` files are a reviewed replacement
that is mostly undeployed. Section 1 explains the history.

## 0. What is live vs what is in the repo (history, with the sources)
| When | State | Source |
|---|---|---|
| 09-01 | v105: the repo inbound file was deployed as step `code1`, plus env `ALPHA_URL`/`ALPHA_SECRET`, Data Store `wa-wamid-dedup` and the old `post_request` step deleted. `send_whatsapp_reply` kept a hardcoded Graph token. Workspace env had `META_SYSTEM_USER_TOKEN` | board row REQ-K2WPD8 (in untracked `tmp/claude_inbox_strategy_check.json`); `scripts/claude_board_worker.ps1:9` |
| ~09-06 | v254: ISSUE 028 lists "dedup dead, hardcoded secret in v254, … webhook auth set to none, no grounding". Pipedream had drifted from its committed source | `apps-script/governor-page-api/Code.js:1605`; `docs/COMMS-PROTOCOL.md:220-222`; `scripts/wa_notify.ps1:69-72` (rows no longer carry a wamid) |
| 09-07 | Repo components forward-ported (commit 66b3271), with "offline contract evidence only … not deployed" | `docs/WHATSAPP-GATEWAY-REPAIR.md:6-7` |
| 09-08 | The auto-reply was stopped by a bare `return;` (v258→v261). An API `PUT` once disabled the workflow for about 8 min | consultation `:36-38`; memory `pipedream-api-put-replaces.md` |
| 09-19 | **v265 Active. Two nodes: an HTTP trigger and ONE 327-line Node step `send_whatsapp_reply`.** It ACKs, extracts, calls 4 models, writes the board row, then hits `return;`. The step source holds the Meta token, the "bus secret" and 4 model keys as plaintext | `docs/consultations/2026-09-19-...md:24-50`; `docs/PIPEDREAM-WA-PREFIX-GATE.md:58-77` |
| 09-20 | The Connect OAuth client gets 404 on every workflow route, so the API cannot read the step | `docs/PIPEDREAM-WA-PREFIX-GATE.md:38-56` |

Stale notes. Memory `whatsapp-inbound-pipeline.md` says the repo inbound file was "never deployed". In fact it was deployed
in v105 and later displaced. `docs/PIPEDREAM-CONNECT.md:30` still names `pipedream_wa_inbound.js` as the live path, which is
also stale. Nothing in the repo records a version after v265.

## 1. Inbound path end to end
### 1a. LIVE, as the docs describe it (no source in the repo)
Meta webhook → Pipedream HTTP trigger (`*.m.pipedream.net`, per `scripts/pudding_harness.py:103-106`) → step
`send_whatsapp_reply` → Apps Script append → board row. Shape of the live rows, as consumers parse them:
- `Source_Tag=whatsapp`, `Target_Surface=Blackboard Alpha DB`, `Action_Type=APPEND`, row id `WRK-…`
  (`tests/test_agent_waker.py:76`, `tests/test_board_watcher.py:51`, `scripts/wa_notify.ps1:69-71`).
  A live count on 09-16 found 478 rows with `Blackboard Alpha DB` and 159 with `Pipedream Cloud Agent` (`docs/COMMS-PROTOCOL.md:294-297`).
- Payload = **his raw text** with no `WA|` header and no wamid (`scripts/agent_waker.py:359-365`, `scripts/wa_notify.ps1:69-72`).
- Media payload = `WA-MEDIA|type=<t>|media=<media_id>` (`scripts/wa_media.py:9`, `scripts/wa_inbound_digest.ps1:9,171`). Reactions
  arrive as `WA-MEDIA|type=reaction|media=` with the emoji and target lost (memory `whatsapp-inbound-pipeline.md:40-45`).
- The gateway model lanes also write rows tagged `claude` / `gemini` (`docs/COMMS-PROTOCOL.md:75-81`;
  `scripts/pipedream_wa_context.js:52` excludes them).
- Board endpoint: **unresolved.** The 09-03 notes say step line 29 holds a hardcoded *bus* secret
  (`scripts/codegs_rotation_window.gs:73-75`). The 09-01 row says the old `post_request` step carried the *Alpha* secret. Memory says
  the step posts to "a fourth Apps Script deployment … not in .env". `DEPLOY.md:98-114` lists a "WhatsApp gateway"
  deployment that returns `{"result":"success","rowId":"WRK-…"}` and **appends on every call, including reads**.
  Local check (booleans only): `.env` `BUS_URL` = the v1 bus. `ALPHA_URL` matches neither the DEPLOY.md gateway suffix
  nor the memory's fourth-deployment prefix. `ALPHA_SECRET` ≠ `BUS_SECRET`.
- The live step has **no prefix gate**. The proposed `?` + sender + type gate is "PREPARED, NOT DEPLOYED" (`docs/PIPEDREAM-WA-PREFIX-GATE.md:3-4,84-118`).
- Prefix routing happens **after** the board, in fleet code that keys on raw text: `scripts/agent_waker.py:364`
  (`^\s*<tag>\b` on `whatsapp` rows), `scripts/grok_wa_inbox.py:115-140` (Grok/agent prefixes, skips `WA-MEDIA|`),
  `scripts/board_waker.py:297-312`, `cloud/board-watcher/main.py` (routes rows to jobs), and the scratchpad `inbox_check.py` (memory).
  `scripts/claude_board_worker.ps1:102-103` only accepts `WA|` rows (the v105 contract). INFERRED: it matches nothing today.

### 1b. REPO component `scripts/pipedream_wa_inbound.js` (deployed in v105, not live now)
- GET handshake. It answers `hub.challenge` (digits only) if `hub.verify_token` == env `META_VERIFY_TOKEN` (timing-safe). It returns 403 if the token is wrong and 500 if it is unset (`:113-129`). Methods other than GET or POST get 403 (`:130-133`).
- POST is ACKed at once with `200 EVENT_RECEIVED`, before any slow work (`:135-137`). Missing env or Data Store throws after the ACK (`:139-146`).
- Optional signature check: with `META_APP_SECRET` set, it requires `X-Hub-Signature-256` and the raw body, then parses the signed bytes (`:148-172`).
- Status deliveries are counted and never appended (`:181-182`). Each message is validated: wamid regex, `from` 6-20 digits, text ≤16000,
  quoted `context.id`, button/list choice id (`:39-69`). Text extraction per type is at `:22-37`. **Media becomes the caption or
  `[image received - no caption]`. The media id is NOT written** (`:30-31`).
- Dedup: `db.get(wamid)` skips repeats (`:189-192`). After an acknowledged write it calls `db.set(wamid,{rowId,acceptedAt})` (`:219`). Data Store prop `db` is required (`:98-101`).
- Write: `POST ALPHA_URL` with JSON `{secret: ALPHA_SECRET, source_tag:"whatsapp", target_surface:"Blackboard Alpha DB",
  action_type:"APPEND", payload}`, a 30 s timeout and the 302 followed (`:196-210`). Success requires HTTP ok, `result==="success"`
  and `rowId` matching `/^WRK-/` (`:211-217`).
- Payload = `WA|wamid=…|from=…|type=…[|reply_to=…][|choice_id=…]|text=<raw text>` (`:71-78`).
- Export `summary` (schema 2) carries the structured messages for later steps (`:228-236`).
- `scripts/pipedream_wa_context.js` is the gate and router for replies. It requires `signature==='valid'` (`:86`) and sender == env
  `WA_OPERATOR_ID` (`:90-100`). `claude-code-cli:|vm-cli:|vm-chrome:|codex:|chat-mobile:` prefixes get a fixed gateway ACK and no model (`:103-107`).
  Other messages become model jobs fed from a fresh Google Sheets snapshot of board `120_71KaF4…` sheetId 0 (`:5-7,10-54,109-111`).

## 2. Reply and outbound paths
| Path | Where it runs (evidence) | Called by | Token / config NAMES | Notes |
|---|---|---|---|---|
| `send_whatsapp_reply` (live) | **Pipedream only.** No source in Apps Script, Cloud Run or the repo. Every hit is a doc or patch note aimed at the Pipedream step (`scripts/gateway_fail_visibly.patch.md:3-4`, `scripts/codegs_rotation_window.gs:73-75`, `scripts/wa_send.py:8-10`) | the Meta webhook trigger, on every delivery, including status callbacks for our own sends (consultation `:45-46`) | **hardcoded literals**: the Meta token, a "bus" secret, and Gemini/OpenAI/Anthropic/Groq keys (consultation `:47-48`). Graph v22.0 (`scripts/wa_notify.py:56-58`) | Reply **disabled since 09-08** (bare `return;`). Models still run, about 8 s per event (`PREFIX-GATE.md:71-74`). Retries were OFF as of 09-03 (`gateway_fail_visibly.patch.md:34-37`) |
| `scripts/pipedream_wa_reply.js` | not deployed (`WHATSAPP-GATEWAY-REPAIR.md:6-7,36-37`) | intended: after the context step and a model call | none. It only builds the Graph body (`:1-3`) | `[STATUS \| gateway/<lane>]` prefix, ≤4096 chars, recipient and `context.message_id` taken from inbound identity (`:10-34`). Needs the existing Meta POST step to send |
| `scripts/wa_send.py` | laptop or VM, by hand | manual (`--check` was used from the VM, `docs/COMMS-PROTOCOL.md` §1) | `WA_TOKEN` (`:60`), `WA_PHONE_NUMBER_ID` (`:66`, **hardcoded default**), `WA_TO` (`:67`, **hardcoded default = his number**), `WA_GRAPH_VERSION` (`:68`, default v22.0) | `.env` only. The docstring claims `WA_TOKEN` is "the value Pipedream's send_whatsapp_reply step uses" (`:72-73`). Unverified |
| `scripts/wa_notify.py` | laptop, and the **Cloud Run job `wa-outbox`** | `scripts/wa_board_outbox.py:368-380`, grok inbox ACK (`tests/test_wa_notify.py:314`) | `META_TOKEN` first, `WA_TOKEN` fallback (`:98-125`), `WA_PHONE_NUMBER_ID`, `WA_TO`. Falls back to the injected env when there is no `.env` (`:79-89`) | text only, no retry, recipient masked in logs |
| `scripts/wa_notify.ps1` | laptop | manual and older laptop tasks (`wa_board_outbox.ps1`, now disabled) | `META_TOKEN`, `WA_PHONE_NUMBER_ID`, `WA_TO` (`:108-116`) | buttons, lists and `-ReplyTo` (`:65-74,139-183`). The stored `META_TOKEN` begins with `Bearer ` (`:43-48`) |
| `cloud/wa-outbox` (Cloud Run **job**, us-central1) | live describe: image `wa-outbox:2f73072125bc`, SA = default compute | `board-watcher` job, every minute (`docs/CLOUD-FLEET-RUNBOOK.md:30,33`) | Secret Manager refs `BUS_URL`, `BUS_SECRET`, `META_TOKEN`, `WA_PHONE_NUMBER_ID`, `WA_TO`, plus plain `BLACKBOARD_STATE_URI` | turns `phase=WA_SEND`/`WA_OUT\|` rows and waker `DONE` replies into sends (`scripts/wa_board_outbox.py:128-144`) |
| `zoom-agent/src/notify.js` | zoom agent | meeting events | `META_TOKEN`, `WA_PHONE_NUMBER_ID`, `WA_TO` (`:45`) | **Graph v21.0** (`:31`) |
| Media fetch (inbound helpers) | laptop | `wa_media.py`, `wa_inbound_digest.ps1`, `wa_transcribe.ps1` | `WA_TOKEN`\|`META_TOKEN` (`wa_media.py:195`); `META_TOKEN` (`wa_inbound_digest.ps1:80-86`, `wa_transcribe.ps1:61-69`) | depends on the live `WA-MEDIA\|…\|media=<id>` row format |

**Which tokens are shared:**
- The **inbound board write needs no Meta token.** It needs the board URL and secret, plus the verify token and app secret if they are used.
  The Meta token appears in the live step only because the reply sits in the same step.
- **Meta access token:** there are two different values locally, `META_TOKEN` ≠ `WA_TOKEN` (boolean check), and both are in Secret Manager.
  The Pipedream step holds its own literal, and on 09-01 it was said to "appear dead". Whether it equals either local value is UNKNOWN.
  Cloud outbound (`wa-outbox`) uses `META_TOKEN`.
- **Phone number id:** one business number, held in `WA_PHONE_NUMBER_ID`. The inbound repo code does not check `metadata.phone_number_id`.
- **Board secret:** the repo inbound uses `ALPHA_SECRET`, while outbound and fleet readers use `BUS_SECRET`. These are different values. Which one the live step
  hardcodes is UNKNOWN. (Rotating the bus secret is a closed decision, per memory. It is noted here only as an input to the move.)
- **Operator number:** `WA_TO` (outbound) and `WA_OPERATOR_ID` (repo context step) mean the same number. Only `WA_TO` exists anywhere.

## 3. Repo file → Pipedream step mapping
| Repo file | Intended step | Live today? |
|---|---|---|
| `scripts/pipedream_wa_inbound.js` | step 1 after the HTTP trigger (it was `code1` in v105): handshake, ACK, dedup, board append | **No.** Its job is folded into `send_whatsapp_reply` |
| `scripts/pipedream_wa_context.js` | new step after a new read-only Google Sheets step (`WHATSAPP-GATEWAY-REPAIR.md:61-70`) | No, never deployed |
| `scripts/pipedream_wa_reply.js` | new step feeding "the existing Meta messages POST" (`WHATSAPP-GATEWAY-REPAIR.md:76-80`) | No, never deployed |
| (none) | `send_whatsapp_reply`, 327 lines | **Yes. No copy exists in the repo.** `PREFIX-GATE.md:122-126` says extract it to a gitignored path because it holds secrets |

**What the repo cannot tell us** (only the live export can): the active version, the step list, the source of `send_whatsapp_reply`,
the env var names actually referenced, the Data Stores and connected accounts, the trigger URL and auth mode, whether `$.respond` is used,
workflow settings (retries, concurrency, timeout), and the Meta webhook registration (callback URL, verify token, subscribed fields, app secret).

## 4. "Alpha DB"
It is **the board itself**: the Google Sheet "Blackboard - Alpha DB", id `120_71KaF4JKGPGz0qUz4phqWRljSqEzSRRm_0zXC_oY`,
Sheet1 / sheetId 0 (`docs/ONBOARDING.md:67-68`, `scripts/pipedream_wa_context.js:5`, `cloud/board-probe/main.py:63`). The name
appears in three ways:
1. the literal `target_surface:"Blackboard Alpha DB"` on every WhatsApp row (`pipedream_wa_inbound.js:205`);
2. the **Alpha / V2 POC gateway**, an Apps Script web app in front of that sheet, reached via `ALPHA_URL`/`ALPHA_SECRET`
   (`scripts/alpha.ps1:3-12`). It answers `{"result":"success","rowId":"WRK-…"}` and appends even on reads (`DEPLOY.md:107-114`).
   **Its source is not in the repo**, since no tracked file generates `WRK-` ids. The v1 bus (`BUS_URL`) is a different Apps Script,
   container-bound to the same sheet (`DEPLOY.md:120-128`, `apps-script/blackboard-bus-v1/Code.gs:47-48`);
3. INFERRED: a Pipedream Google Sheets connected account. The repair doc says "the already-authorized Alpha DB" (`WHATSAPP-GATEWAY-REPAIR.md:61`).

## 5. Secret and config NAMES and where they live
| Name | Used by | Pipedream | `.env` | Secret Manager (live list) | Other |
|---|---|---|---|---|---|
| `ALPHA_URL`, `ALPHA_SECRET` | repo inbound, alpha.ps1 | env var (v105 record; current use unknown) | yes | yes | Apps Script property name for the gateway side: unknown |
| `META_VERIFY_TOKEN` | repo inbound GET | intended env | **no** | **no** | the legacy literal was removed and must not be reused (`WHATSAPP-GATEWAY-REPAIR.md:56-58`) |
| `META_APP_SECRET` | repo inbound signature | optional env | **no** | **no** | set in the Meta App dashboard |
| `WA_OPERATOR_ID` | repo context step | intended env | no | no | same number as `WA_TO` |
| Data Store `db` (`wa-wamid-dedup`) | repo inbound | Data Store (v105) | — | — | "dedup dead" at v254 |
| `META_SYSTEM_USER_TOKEN` | nothing in the repo | workspace env (09-01 record) | no | no | |
| hardcoded Meta token, bus or alpha secret, `GEMINI`/`OPENAI`/`ANTHROPIC`/`GROQ` keys | `send_whatsapp_reply` | **plaintext in step source** | — | provider keys exist under their own names | |
| `META_TOKEN` | wa_notify.*, wa-outbox, zoom-agent, media helpers | — | yes | yes (wa-outbox ref) | stored with a `Bearer ` prefix |
| `WA_TOKEN` | wa_send.py, wa_notify.py fallback, wa_media.py | — | yes | yes | not equal to `META_TOKEN` |
| `WA_PHONE_NUMBER_ID`, `WA_TO` | every outbound path | — | yes | yes (wa-outbox refs) | hardcoded defaults in `wa_send.py:66-67` |
| `WA_GRAPH_VERSION` | wa_send.py | — | no (default v22.0) | no | other code pins v22.0, or v21.0 in zoom-agent |
| `BUS_URL`, `BUS_SECRET` | wa-outbox, readers | ? | yes | yes | Apps Script Script Property `BUS_SECRET` |
| `PIPEDREAM_CLIENT_ID`/`_SECRET`/`_PROJECT_ID`(`proj_5DsGpGe`)/`_PROJECT_ENVIRONMENT` | `scripts/pipedream_connect.py:47-57` | — | yes | yes | Connect OAuth client, **not** the workspace API |
| `WEBHOOK_URL` | `scripts/pudding_harness.py:99-106` | (the trigger URL) | not checked | no | must be `*.m.pipedream.net` |
| `BLACKBOARD_STATE_URI` | wa-outbox | — | — | plain env on the job | `gs://sfdc24-fleet-state` CAS store |

## 6. Migration notes (short)
**Any new home must do all of the following:**
- answer Meta's GET verify;
- ACK the POST with 200 within about 5 s, because Meta disables the webhook after repeated failures (`pipedream_wa_inbound.js:135-137`);
- ignore `statuses` cheaply, since every outbound send triggers callbacks;
- dedup on wamid;
- write the board row **in the format current consumers parse**: raw text and `WA-MEDIA|type=|media=<id>`.

Two traps if the repo component is ported as-is:
1. Its `WA|wamid=…|text=` payload begins with `WA|`. That breaks the `^\s*<tag>\b` prefix addressing in `agent_waker.py:364` and
   `grok_wa_inbox.py`. Every consumer must change together, or the payload must be kept raw (with the wamid carried elsewhere).
2. It drops media ids (`:30-31`), so voice notes and images would stop reaching `wa_media.py` and `wa_inbound_digest.ps1`.

Do not carry over the 4 discarded model calls without deciding to.

**Candidate homes:**
- **Pipedream Connect.** Per the repo, Connect is end-user OAuth to third-party apps, not workflow hosting (`PIPEDREAM-CONNECT.md:27-32`,
  `PREFIX-GATE.md:50-54`). It would still need a host for the HTTP endpoint and code. Whether Connect's own trigger features can receive
  the Meta webhook is UNVERIFIED, so check Pipedream's docs and the shutdown notice. It would keep the Pipedream dependency.
- **Cloud Run, project `sfdc24`.** This fits the existing pattern (`docs/CLOUD-FLEET-RUNBOOK.md:14-24`). `ALPHA_*`, `BUS_*`, `META_TOKEN`
  and `WA_*` are already in Secret Manager. It needs a new public (unauthenticated-ingress) **service**; today's WhatsApp code runs as jobs.
  It also needs new secrets `META_VERIFY_TOKEN` and `META_APP_SECRET`, plus a dedup store: Firestore, or `scripts/state_store.py` CAS on GCS.
  Memory says new data stores should prefer northamerica-northeast2 (Toronto); the fleet itself is in us-central1. INFERRED: use
  min-instances=1 or ACK-first so cold starts do not miss the 5 s window.
- **Own Meta endpoint.** In practice this is the same as Cloud Run, since the endpoint needs a host. INFERRED: Apps Script is a poor host
  because a web app cannot read request headers (so no `X-Hub-Signature-256` check) and answers through a 302 redirect.

**Meta re-registration** (owner, Meta app admin): App Dashboard → WhatsApp → Configuration → set the Callback URL and Verify token.
Meta then sends a GET challenge. Make sure the `messages` field stays subscribed. INFERRED: there is one callback per app, so the switch is
a hard cut with no parallel run. Rollback = set the URL back. **Keep the Pipedream workflow Active until the new path is proven.**

**Risks:**
- **Inbound goes dark** if the verify step or the ACK fails. Every agent's doorbell sits behind this, so the whole fleet would be deaf.
- Consumers break silently if the row format changes.
- Extracting the live step exposes plaintext secrets. Use a gitignored path only (`PREFIX-GATE.md:122-126`).
- The builder's **Test** button has live side effects (`PREFIX-GATE.md:152-154`).
- The Pipedream API `PUT` replaces settings and once disabled the workflow (memory).
- Probes against the Alpha gateway write rows (`DEPLOY.md:107-114`).
- Deadline 2027-03-31, about 6 months away.

## 7. Questions only the live export (owner auth) can answer
1. What is the active version (v265 or later), and what is the full step list with names?
2. Give the full source of `send_whatsapp_reply`, saved to a gitignored path. Does it call `$.respond`, and where?
3. Which board URL (deployment id) does it POST to, and with which secret (BUS, ALPHA or another)? Give the exact body, `target_surface` and payload format, including media and reactions.
4. Which workspace env vars exist (names only), and which ones does the step reference instead of hardcoding? Is `META_SYSTEM_USER_TOKEN` still there?
5. Which Meta token does the step use, and does it match `META_TOKEN` or `WA_TOKEN`? Compare SHA-256 prefixes, not values.
6. Which Data Stores are attached (`wa-wamid-dedup`?) and still read? Which connected accounts (Google Sheets, WhatsApp, other)?
7. What is the trigger URL, and what are its auth mode (v254 said "none") and response mode?
8. What are the workflow settings: retries, concurrency or workers, timeout, memory, error notifications?
9. What are the event volume per day, error rate and run time over the last 30 days?
10. Are any other workflows or sources in `proj_5DsGpGe` also affected? What writes the 159 `Pipedream Cloud Agent` rows?
11. Meta side: which callback URL is registered, which fields are subscribed, is the app secret or signature in use, is there a WABA-level `override_callback_uri`, and who are the Meta app admins?
12. Are the `claude`/`gemini` gateway-lane rows still being written, and does anything still use the 4 model outputs?
