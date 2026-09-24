# Governor Studio email adapter

This is a source contract, not proof of a deployment or an OAuth grant. The
Governor Apps Script manifest declares `script.send_mail`, but the deploying
account must still have granted that scope. A source merge alone must never be
reported as working email delivery.

The web-app route is:

```text
POST <governor /exec URL>?action=studio-email
```

The Studio controller keeps its existing JSON body and signs
`timestamp + "\n" + nonce + "\n" + email + "\n" + code`. The route is
separate from the Governor passphrase path: malformed or refused Studio
requests return `{"ok":false}` and cannot append a board row. A GET to the
same action is a read-only negative probe and also returns only
`{"ok":false}`.

## Script Properties

The adapter is off unless all of these dedicated values are present:

- `STUDIO_GOVERNOR_EMAIL_ENABLED=on`
- `STUDIO_GOVERNOR_EMAIL_SECRET`: a new 64-character lowercase hexadecimal
  signing key; do not reuse `GOVERNOR_PASS` or the standalone sender key
- `STUDIO_GOVERNOR_OPERATOR_EMAILS`: the exact comma-separated lowercase
  address allowlist; there is no owner or Governor-identity fallback

The adapter writes only `STUDIO_GOVERNOR_EMAIL_STATE_V1`. That bounded record
contains nonces, keyed subject hashes, acceptance times, and a non-sensitive
quota fence (the last observed remaining count and pending reservation times)—
never a raw email address or verification code. It rejects replays, more than
three accepted attempts for one address in 15 minutes, more than 20 accepted
attempts globally in a rolling 24 hours, and Studio sends that would leave
fewer than 12 recipients in the deploying account's current MailApp quota.
The quota fence prevents overlapping Studio executions from spending the same
snapshot; unrelated scripts can still consume the shared account quota. A
reservation is written before mail delivery and survives a failed or ambiguous
provider call.

## Release sequence

1. Reconcile the complete deployed Governor source with the reviewed commit.
   A blind `clasp push` from a partial or stale checkout is not permitted.
2. Create an immutable Apps Script version and keep the previous deployment ID
   and version as the rollback target.
3. Add the dedicated properties while the adapter remains disabled.
4. Deploy, then prove an unsigned GET and POST both return JSON `ok:false` and
   do not write the board or send mail.
5. Enable the adapter and run one controlled signed request to an allowlisted
   inbox. MailApp acceptance is not inbox receipt; verify the message arrives.
6. Replay the same signed request and prove it is refused without a second
   message.
7. In a separate Cloud Run revision, update `STUDIO_EMAIL_SENDER_URL` to the
   action URL and point `STUDIO_EMAIL_SENDER_SECRET` at the matching dedicated
   key. Keep `STUDIO_WORKER=synthetic` and voice disabled.
8. Complete one real `/v1/auth/start` and one-use `/v1/auth/verify` flow before
   configuring the public Studio page with the controller URL.

Immediate rollback is to set `STUDIO_GOVERNOR_EMAIL_ENABLED` to anything other
than `on`, restore the previous controller revision, and—if needed—restore the
previous Apps Script deployment version. Never print or place the signing key,
email code, or raw recipient address in logs, board rows, commits, or PR text.
