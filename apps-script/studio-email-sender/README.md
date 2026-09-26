# SFDC24 Studio email sender

This standalone Apps Script web app accepts only signed server-to-server OTP
requests from the Studio controller. Configure these Script Properties before
deployment:

- `STUDIO_EMAIL_SENDER_SECRET`: the same secret as the controller setting,
  at least 32 characters.
- `STUDIO_OPERATOR_EMAILS`: comma-separated, exact lowercase addresses,
  matching the controller allowlist.
- `STUDIO_CLIENT_EMAILS` (optional): comma-separated, exact lowercase addresses
  of client workspaces - the addresses in the controller's `studio_clients`
  registry. They receive the sign-in code and their working-session summary
  like an operator. Unset or empty adds nobody; one malformed entry refuses
  every request, the same as the operator list.

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
