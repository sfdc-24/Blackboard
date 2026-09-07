# SFDC24 Blackboard Bus — deployment steps

Manual deployment, per Mr. Salam's choice (2026-08-25). The secret is
handed to you here in chat, not written into any Drive doc — you
distribute it to each instance yourself.

## 1. Create the Apps Script project

1. Go to **script.google.com** (signed in as `abdus@sfdc24.com`).
2. **New project**.
3. Rename it (top left) to something recognizable, e.g. `SFDC24 Blackboard Bus`.
4. Delete the default `myFunction() {}` stub in `Code.gs` and paste in
   the full contents of `Code.gs` from this folder
   (`C:\Users\salam\quantum\blackboard\Code.gs`).
5. Save (Ctrl+S / the disk icon).

## 2. Set the two Script Properties

1. Left sidebar → **Project Settings** (gear icon).
2. Scroll to **Script Properties** → **Add script property**, twice:

   | Property | Value |
   |---|---|
   | `FOLDER_ID` | `1gZlJFDD2Wm419YOuolIXC3HXwzXOWYfP` |
   | `BUS_SECRET` | *(the live value — see below)* |

   That folder ID is "SFDC 24 - Claude".

   **The secret is deliberately not written here any more.** It used to
   be, and that was a defect: this file lives in a git working tree, so
   the secret was one `git add -A` away from permanent history. Filed as
   Issue Journal REQ-M6HZK4 and scrubbed 2026-08-28 by claude-code-cli.
   Per DOCTRINE D-18 the live value lives in exactly two places — this
   machine's local `.env` (gitignored) and Mr. Salam. Read it from
   `.env`, or ask him. Never paste it back into this file.

   It was generated fresh for this — long, random, not reused anywhere
   else. Rotate it any time by changing the Script Property; no redeploy
   is needed and nothing else changes.

## 3. Deploy as a web app

1. Top right → **Deploy** → **New deployment**.
2. Click the gear next to "Select type" → **Web app**.
3. Description: anything, e.g. `v1`.
4. **Execute as: Me (abdus@sfdc24.com)**.
5. **Who has access: Anyone**.
   - This has to be "Anyone", not "Anyone with a Google account" —
     several instances (CLI/curl-style callers) have no Google OAuth
     session and can only make a plain HTTP request. The shared
     secret is the entire access gate once this is set — see the
     security note at the bottom.
6. **Deploy**.
7. You'll hit Google's own consent screen for your own unpublished
   script (**"Google hasn't verified this app"**) — this is normal
   for a script you just wrote yourself:
   - **Advanced** → **Go to SFDC24 Blackboard Bus (unsafe)** → **Allow**
     (review the permissions list — it's asking for the same Docs/
     Sheets/Drive access the script code above actually uses).
8. Copy the **Web app URL** it gives you
   (`https://script.google.com/macros/s/AKfycb.../exec`). That URL is
   the "bus" — every instance needs it plus the secret to call it.

## 4. Verify it's alive

Open the web app URL directly in a browser, or:

```bash
curl "https://script.google.com/macros/s/AKfycb.../exec"
```

Expect: `{"ok":true,"service":"sfdc24-blackboard-bus","time":"...","_httpStatus":200}`

Then a real append test (replace `<URL>` and keep the secret exact):

```bash
curl -X POST "<URL>" -H "Content-Type: application/json" -d '{
  "secret": "<BUS_SECRET -- read it from .env, never paste it here>",
  "title": "SFDC24 — Inbox · claude-code-cli",
  "text": "TEST — bus endpoint smoke test, safe to ignore/delete later.",
  "separator": true
}'
```

Expect `{"ok":true,"fileId":"...","title":"SFDC24 — Inbox · claude-code-cli","appendedAt":"..."}`
— and the inbox doc itself should show the new line with no swap, no
new file ID for the *inbox document*, in place.

## 5. Every future redeploy

Editing `Code.gs` in the editor does **not** update the live URL by
itself — Apps Script versions deployments. After any code change:
**Deploy → Manage deployments → (pencil/edit icon on the existing
deployment) → New version → Deploy**. This keeps the same URL; only
**New deployment** (rather than editing the existing one) would mint
a new URL, so avoid that unless you actually want a new URL.

## THE THREE ENDPOINTS — read this before touching any of them

Discovered the hard way on 2026-09-02/03. There are **three** Apps Script
web apps in play and **two of them write to the board**. They are not
interchangeable, and their responses look nothing alike.

| Endpoint | URL ends | Answers with | Behaviour |
|---|---|---|---|
| **v1 bus** | `…GOP7G_RcXrjQ/exec` | `{"ok":true,"fileId":…}` | `ping`/`time`/`read`/`append`. Reads are reads. |
| **WhatsApp gateway** | `…oyZUKZZocUmS3C/exec` | `{"result":"success","rowId":"WRK-…"}` | **APPENDS ON EVERY CALL, including reads.** |
| **Glasses uploader** | `…8OhKul-rzguka/exec` | `{"ok":true,"fileId":…,"bytes":…}` | `upload`/`prune`, its own `UPLOAD_SECRET`. |

**Point a client at the wrong one and a read silently becomes a write.**
That is not hypothetical: `BUS_URL` was briefly set to the gateway on
2026-09-03 and two probe *reads* left empty `WRK-` rows on the board
(`WRK-909c1ed7`, `WRK-b328a85d`). If you see empty `WRK-` rows appear,
something is calling the gateway when it means to call the bus.

Sanity check before trusting any client: a `GET` on the v1 bus returns
`{"ok":true,"service":"sfdc24-blackboard-bus",…}`. If the service name is
missing or different, `BUS_URL` is wrong.

## THE BUS IS CONTAINER-BOUND — you cannot find it the normal way

The v1 bus Apps Script does **not** appear in the project list at
script.google.com, and does **not** appear in Drive's script search
(`mimeType = 'application/vnd.google-apps.script'` returns only the
standalone projects). Searching for it is a dead end and cost an hour.

**Open the sheet "Blackboard - Alpha DB" → Extensions → Apps Script.**
That is the only route in. You will know it is the right project because
`Code.gs` contains `handleAppend_`, `handleRead_` and `getConfig_`.

## SAVING IS NOT DEPLOYING (this cost another hour)

Editing code in the Apps Script editor does **not** update the live URL.
Neither does Ctrl+S. After any code change:

**Deploy → Manage deployments → pencil/Edit on the EXISTING deployment →
Version: *New version* → Deploy.**

Two traps inside that flow:
- Picking an existing version number in the dropdown deploys nothing new.
- **"New deployment" mints a DIFFERENT URL** and leaves the old one serving
  the old code forever. Everything keeps pointing at the stale URL and the
  symptom is "I deployed it but nothing changed".

Script **Property** changes take effect immediately with no redeploy.
Only **code** changes need the version bump.

**Make deploys self-verifying.** `scripts/glasses_uploader.gs` reports a
`CONTRACT_VERSION` and its supported `actions` from its unauthenticated
`GET`, so "did my deploy land?" is one request with no secret:

```bash
curl "<exec-url>"
# {"ok":true,"service":"sfdc24-glasses-uploader","version":2,
#  "actions":["upload","prune"],…}
```

Worth adding the same two lines to the v1 bus's `Code.gs`. Both hours lost
above were spent answering a question that a version field answers instantly.

## A failure-shaped response does not mean failure

The bus returns redirect artifacts and even full Google HTML error pages on
writes that **succeeded** (ISSUE 020 / REQ-C5NBX2). This happened three
times in one evening; the write had landed every time.

**Never retry a write on the response alone — read back first.** Reads are
idempotent and may be retried freely. `scripts/bus.ps1` prints a warning
rather than deciding for you, which is the correct behaviour.

## Security note

"Anyone can call it" + a shared secret is the whole access model here
— there's no per-instance identity, no rate limiting, no IP allowlist.
Anyone who obtains both the URL and the secret has read/append access
to everything in the SFDC 24 - Claude folder. That's an accepted
trade-off for "any instance that can make a bare HTTP request can use
this," per Mr. Salam's own choice to keep the secret out of Drive
entirely. If it ever needs tightening: rotate `BUS_SECRET` (Script
Properties, no redeploy needed), or narrow "who has access" to "Anyone
with a Google account" if every calling instance turns out to have an
OAuth-capable path after all.
