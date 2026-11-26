# SFDC24 Studio email sender

This standalone Apps Script web app accepts only signed server-to-server OTP
requests from the Studio controller. Configure these Script Properties before
deployment:

- `STUDIO_EMAIL_SENDER_SECRET`: the same secret as the controller setting,
  at least 32 characters.
- `STUDIO_OPERATOR_EMAILS`: comma-separated, exact lowercase addresses,
  matching the controller allowlist.

Deploy the web app to execute as the deploying account. Its URL becomes
`STUDIO_EMAIL_SENDER_URL` in the controller. Mail authorization is required for
the deploying account. Do not put the URL or signing secret in browser code.

The endpoint accepts a JSON POST with exactly `timestamp`, `nonce`, `email`,
`code`, and `signature`, all strings. The controller signs the first four values
joined by newlines with HMAC-SHA256. A successful send returns `{"ok":true}`;
all other outcomes return `{"ok":false}` without request details. Apps Script
web apps return HTTP 200 for these JSON responses, so the controller must
inspect `ok` if it needs to distinguish delivery success from refusal.

## The working-session summary (kind `summary`)

At the end of a homepage conversation the visitor can have the session sent to
them as a PDF. The controller posts exactly `kind` (`"summary"`), `timestamp`,
`nonce`, `email`, `pdf` (standard base64), `pdf_sha256` (hex) and `signature`,
all strings. The signature is HMAC-SHA256 over `summary`, the timestamp, the
nonce, the email and the PDF digest joined by newlines, so it can never be
replayed as a sign-in code or the other way round. The script recomputes the
digest of the decoded attachment, requires `%PDF`, shares the nonce ledger with
the sign-in path, and sends one email with the PDF attached.

Recipients follow the same allowlist unless the Script Property
`STUDIO_SUMMARY_ANY_RECIPIENT` is exactly `true`, which lets a summary (never a
sign-in code) reach a public visitor's verified address. The controller only
asks for a summary to an address it verified at sign-in.

The new kind needs no new OAuth scope: `script.send_mail` covers attachments.
Deploy it as a new version of the same web app deployment so the URL is kept.

## A plan for review, and the approved plan (kinds `plan_review` and `plan`)

After a call, a server drafts a one-page plan and sends the operator a review
copy; the person it was written for gets their copy only after the operator
approves it on a review page. Two kinds carry those PDFs, each with its own
fields and its own signature, so neither can be replayed as the other, as a
summary or as a sign-in code:

| kind | fields (all strings) | signed, one per line | recipient |
|---|---|---|---|
| `plan_review` | `kind`, `timestamp`, `nonce`, `email`, `pdf`, `pdf_sha256`, `review_url`, `label`, `signature` | `plan_review`, timestamp, nonce, email, PDF digest, `review_url`, `label` | the operator allowlist, always |
| `plan` | `kind`, `timestamp`, `nonce`, `email`, `pdf`, `pdf_sha256`, `label`, `signature` | `plan`, timestamp, nonce, email, PDF digest, `label` | the allowlist, or anyone when `STUDIO_PLAN_ANY_RECIPIENT` is exactly `true` |

`label` is at most 80 characters with no control character, angle bracket or
at sign: the review email's subject ("Review: a proof of concept quote for ...")
and the approved plan's greeting ("Hi ..."). `review_url` is an `https` link
with a short plain path, put in the review email's body. The PDF checks, the
nonce ledger and the freshness window are the summary's.
`STUDIO_SUMMARY_ANY_RECIPIENT` does not open `plan`, and
`STUDIO_PLAN_ANY_RECIPIENT` does not open `summary` or the sign-in code.

### Deploying the plan kinds (the owner, in his own account)

1. Open this script at script.google.com, signed in as the account that
   deploys it, and replace `Code.gs` with this file from `main`.
2. Project Settings, Script Properties: check that `STUDIO_OPERATOR_EMAILS`
   lists the address that should receive review copies; add
   `STUDIO_PLAN_ANY_RECIPIENT` = `true` so an approved plan can reach a guest.
3. Deploy, Manage deployments, edit the existing web app deployment, choose
   Version: New version, then Deploy. The URL stays the same, and no new
   permission is asked for (`script.send_mail` already covers attachments).
