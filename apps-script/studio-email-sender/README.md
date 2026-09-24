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
