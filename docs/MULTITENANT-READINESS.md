# MULTITENANT-READINESS-001

Status: source repair only. Do not describe this as deployed or as general
multi-tenant readiness.

## Independent ruling

On 2026-09-07, the production Governor deployment was read through the Apps
Script API at immutable version 31. Its `Code.gs` SHA-256 was
`FF43DA1193980E7B43A657DF22754F3A64784C49283456C1C00228D83AAAAF1F`.
That source accepted public `vid`, used it in `vh_<vid>` and `rc_<vid>` cache
keys, checked the daily counter before the provider call, and incremented the
counter only after a successful response. Current `origin/main` retained the
same chat/history pattern. Public `?view=home` and `?auth=start` probes both
returned HTTP 200; the former offered Google sign-in and the latter rendered a
configured Google OAuth redirect.

The issue is therefore real:

- Google sign-in identified who sent a quarantined message, but it did not bind
  conversation history or spend state to that identity.
- Rotating `vid` reset the per-session counter. Choosing somebody else's `vid`
  selected the same cached voice history.
- Two concurrent calls could both pass the session/daily prechecks before
  either increment ran.

Google sign-in was working, but sign-in alone was not a tenant boundary.

## Source repair

The repaired contract is:

1. The server issues a purpose-labelled, domain-separated HMAC conversation
   token. It cannot be replayed as the separately shaped Google auth-session
   token or vice versa. For a signed-in visitor, its stable key is derived from
   Google's immutable `sub`, never the email or a browser id. For an anonymous
   visitor it contains a random server nonce.
2. The server verifies the token on every turn and derives an opaque 128-bit
   cache/property key. Google-backed tokens must match the current signed
   session. Anonymous tokens are invalid after sign-in, and Google-backed tokens
   are invalid without the matching session.
3. Legacy `sid`/`vid` values are ignored. They cannot select history, logging or
   spend keys.
4. A single script lock covers the check and durable reservation of both the
   per-conversation and whole-site chat budgets. Reservation completes before
   the one Anthropic request. A failed provider attempt remains counted.
5. The numeric ceilings are unchanged: 12 attempts per conversation and 150
   per UTC day by default. The current `CHAT_DAILY_CAP` override is still used.
   The legacy `CHAT_COUNT_YYYYMMDD` value is read and updated so a rollback
   cannot forget spend already reserved by this version.
6. Active session state is bounded to 96 entries in one property. Expired
   entries are purged; active entries are never evicted to admit more spend.
   An absent state property is accepted only for migration from v31. A present
   malformed state, malformed legacy counter or malformed cap fails closed;
   counters are never silently reset. Capacity, parse, property or lock failure
   returns an offline response before any provider request.
7. Visitor rows still land only in `PUBLIC_INBOX` as
   `EXTERNAL_UNTRUSTED / instruction_authority=NONE / PUBLIC_RECEPTION /
   UNREVIEWED`. The signed token identifies a conversation; it confers no
   Governor authority and causes no Governor read.

The web client contract is `ct=<server token>`. The Apps Script reception page
and the top-level voice page persist the returned `ct` and send it on the next
turn. The separate `sfdc24-site` repository is therefore a coupled release; an
old voice page would discard the returned token and start a new anonymous
conversation on each turn.

## Acceptance evidence required before production

- Two Google subjects produce distinct history/budget keys; a new token for the
  same subject produces the same key.
- Email changes and legacy `sid`/`vid` rotation do not reset the signed-in
  identity or the 12-attempt ceiling.
- The same legacy `vid` used by two identities cannot mix cached history.
- A forged token cannot select a key, and a Google token cannot be replayed with
  another Google session.
- A concurrent request at the final daily/session slot results in exactly one
  provider call. The reservation is visible before that call begins.
- Provider failure consumes the reserved attempt; retry cannot overspend.
- Each allowed chat turn makes exactly one model request.
- Quarantine rows retain the four fixed trust/provenance values, and provider
  payloads contain neither Google subject/email nor Governor data.
- The current public voice client persists returned `ct`; tests also exercise an
  old `vid`-only caller and prove the backend ignores it while the atomic global
  daily ceiling remains effective.

## Coupled staging and rollback plan

The version 31 evidence above is historical. On 2026-09-08 at 16:05 UTC,
immutable production read-back verified **version 33**, with file-set SHA-256
`03bf71ee87f1ee2fae6efec5adb1e387fa9bea682691969088bc6b43ad1ad3d7`.
Its `Code.gs` and the other five non-Reception files are unchanged from v31;
its Reception adds the link to the top-level `/voice/` page. Returning to v31
would remove that visitor fix. PR33 ports it into the canonical source; include
the accepted port in the next candidate instead of copying the older live
Reception over the signed-token client.

Before each cutover, record the then-current production deployment binding,
immutable source hashes and site commit as the rollback pair. Refresh this
evidence if either target moves. The v33 read-back here is a reference, not
permission to overwrite a later production release.

1. Merge neither repository until both PRs are green and reviewed together.
2. Deploy the Apps Script source to the production-distinct Governor staging
   script as a new immutable version. Do not repoint production.
3. Publish the companion site commit to an isolated Pages preview or temporary
   path that targets only the staging Apps Script deployment.
4. Read back the exact Apps Script immutable source/file-set hash and the exact
   Pages commit/bytes. Run anonymous, two-Google-identity, legacy-client,
   concurrency, one-provider-call and quarantine checks with non-production
   counters and provider stubs.
5. Re-read both stable production endpoints and prove they were unchanged.
6. For a production release, publish the backend first and the `ct`-aware site
   client immediately after. The backend remains spend-bounded for old clients,
   but old clients do not retain anonymous history.
7. Roll back the site to the recorded pre-cutover immutable commit first, then
   repoint Apps Script to the recorded pre-cutover immutable version (v33 at
   the read-back above). Its unchanged v31 `Code.gs` reads the legacy daily
   counter maintained by the repaired version. Revalidate that compatibility
   if the recorded baseline has changed. Read back both targets and verify the
   public reception, its `/voice/` fallback and the voice page in a browser.

## Residual boundary

An anonymous token is a bearer session, not proof of a person. A determined
anonymous caller can discard it and request another server nonce; Apps Script
has no stable first-party cookie or trustworthy person identifier for that
caller. The atomic whole-site daily cap is therefore the hard anonymous spend
ceiling. Strong per-person isolation requires Google sign-in or a future edge
session service. This repair must not be represented as full multi-tenant data
isolation for arbitrary product workloads.
