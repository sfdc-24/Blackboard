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
