# Governor v31 reconciliation — September 6, 2026

After reading the day's Blackboard messages, a fresh read-only Apps Script API
listing confirmed production deployment
`AKfycbx0D-5DAnMqOm9YbN3iKDwuiBApEi_xex60f6pwdvObEyQBF5jcOK715pl1mN-Nzn6gng`
at immutable **v31**, in script
`1lTbqTZ3DBHI2WyJu19Lf1M0a4J01aEuH45c2VcT6Rzxy0vxE2S8FYUEp`.
All seven files were fetched with explicit `versionNumber=31`, and the response
script ID was checked. No Google mutation or function execution occurred.

## Exact source delta

Only Monitor.gs and appsscript.json differ from the independently fetched v30
baseline. Code.gs, Auth.gs and all three HTML/remaining script files are unchanged.
Hashes use UTF-8 LF source; the manifest is strict parsed JSON serialized with
sorted keys and compact separators, as defined in CICD-BUILD-IDENTITY.md.

| v31 file | SHA-256 |
|---|---|
| Auth.gs | `1b7fa67196c3c904d3b389138ef8af4efec4e5612982d5134a88b3d82183dfb8` |
| Code.gs | `ff43da1193980e7b43a657df22754f3a64784c49283456c1c00228d83aaaaf1f` |
| Index.html | `9e98b85129b127bc1aa6414b2e5242d917c7bc122baceaadd23d77016ef9aea7` |
| Monitor.gs | `cc50d7a91c95638701ae9b146de40395b39013201261857411d423ad49674922` |
| PublicInbox.gs | `09d2301c595d54096ff819eb996c0196f0e8317ded14ec4e417ae678efb8fb6a` |
| Reception.html | `2dce046fc9faacb0d06d0e81a24c86bcaea74fa7737b3f0d06abb6c1e6883181` |
| appsscript.json | `7f5f66e4683a5bfe1b43e9df89be7bd3dbbe0a21f469f99eefb5d324930dc19d` |

The review branch now carries these two v31 files. Proposed auth, build-health,
wording and voice changes in the other files are preserved. Replacing the whole
project with production source would erase those intentional review changes.

Monitor notification state (`www`, `apex`, `silence`, `lastAndonTs`) advances only
after `monitorNotify_` succeeds. A failed send or daily-cap refusal leaves the
notification pending for a later tick; site strike counters still advance as
measurements. The manifest explicitly retains the v31 scope set: spreadsheets,
script.external_request, script.scriptapp, script.send_mail, userinfo.email and
userinfo.profile, each under `https://www.googleapis.com/auth/`. This reconciliation
does not add scopes beyond v31 or claim that staging has granted them.

## Verification and limits

Eight regression tests failed on the previous review source and pass with this
delta: failed ANDON delivery and subsequent deduplication, www and apex outage
debounce/retry, three board-silence/clock transitions, daily-cap rollover, and
manifest scope preservation. The mail mock throws the permission error reported
by the real monitor. No email, board, provider or cloud API is called by tests.
CI includes the manifest and test file in its path filter. At the v31
reconciliation point the combined CI/CD suite had 90 tests and PR #4 added nine
voice/readiness tests; later staging safety coverage is counted in
[SITE-P0-INTEGRATION.md](SITE-P0-INTEGRATION.md).

The board's report of a successful September 6 17:26:25 UTC ANDON email was also
confirmed by a bounded Gmail read of that exact subject and date. That proves
one alert arrived; it does not establish ongoing trigger liveness. Production
consent and v31 did not clear the separate staging v1/403 condition recorded in
CICD-STAGING-READBACK-2026-09-06.md at that time. Owner consent was subsequently
completed on September 7 and is recorded in CICD-STAGING-AUTH.md, but a selected
new staging build must still regenerate source/build evidence. Actual staging
deploy/rollback and application acceptance remain open.
