# Voice client pending-reply repair

The September 7 resume acceptance reproduced an overlapping-turn defect in
the voice client. During a pending reply the send button was disabled, but
Enter still cleared the draft and submitted a second request. Both requests
could leave before the server issued the first conversation token. The
microphone's stop control also reset the visual mode without cancelling the
pending request, so visual mode alone was not a sufficient guard.

The client now tracks the outstanding reply independently of visual mode.
Typed and recognised speech share that guard, the microphone is disabled while
the reply is pending, and the retained typed draft becomes sendable when the
callback completes, including its error path. Existing conversation-token and
production endpoint contracts remain intact.

The canonical browser acceptance lives in the companion
[site PR #6](https://github.com/sfdc-24/sfdc24-site/pull/6), at commit
`c409095538556f5c002430304339f23216e6beb9`, in
`tests/voice-conversation.spec.cjs`. Four tests execute the real page with all
external network requests intercepted: token retention across turns/reloads,
replacement after an auth-session change, blocked browser storage, and delayed
reply plus repeated Enter. The last case failed on the old client by clearing
the draft and dispatching a second request. All four pass after the repair.
These are browser contract tests; their synthetic auth and conversation tokens
do not establish live Google authentication or provider behavior.

The site page and this repository's `site/voice/index.html` have identical Git
blob `d269437cd400ae6bec4f4fcf6d6b3911bae4e453`. The 12 backend tenant-boundary
tests pass against this mirror. The combined site candidate also passes four
homepage outage/readiness fixtures and two existing client contract tests.

Read-only Apps Script verification at 2026-09-07T17:00:34Z independently found
production v31 and staging v5. The staging immutable source and fresh-nonce
build response match commit `740aec0287d6fb72505f8a8e850394df379edd1e` and
file-set SHA-256
`873c64863b58530c8cbec67728a26475e8ed594b1c8c83061ae025acaadd20eb`.
This patch changes the mirrored static client only. The coupled production
cutover and rollback gates in `MULTITENANT-READINESS.md` remain open.
