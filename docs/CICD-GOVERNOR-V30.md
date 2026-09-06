# Governor v30 reconciliation — September 6, 2026

A fresh, read-only Apps Script API deployment listing identified Governor production deployment `AKfycbx0D-5DAnMqOm9YbN3iKDwuiBApEi_xex60f6pwdvObEyQBF5jcOK715pl1mN-Nzn6gng` at immutable version **30**, in script `1lTbqTZ3DBHI2WyJu19Lf1M0a4J01aEuH45c2VcT6Rzxy0vxE2S8FYUEp`. The API source was then fetched with `versionNumber=30` and its returned script ID checked. No Google mutation or function execution occurred.

The content read-back contained seven files. Hashes below are SHA-256 over UTF-8 text with LF line endings; `appsscript.json` is canonical parsed JSON with sorted keys and compact separators. They describe the read-back baseline, not the proposed source after fixes.

| v30 file | SHA-256 |
|---|---|
| Auth.gs | `1b7fa67196c3c904d3b389138ef8af4efec4e5612982d5134a88b3d82183dfb8` |
| Code.gs | `ff43da1193980e7b43a657df22754f3a64784c49283456c1c00228d83aaaaf1f` |
| Index.html | `9e98b85129b127bc1aa6414b2e5242d917c7bc122baceaadd23d77016ef9aea7` |
| Monitor.gs | `20b28ee3fdcbf8d58043bd74afab512282f390d2f0a7e8aeb49fb3a6a7581ea1` |
| PublicInbox.gs | `09d2301c595d54096ff819eb996c0196f0e8317ded14ec4e417ae678efb8fb6a` |
| Reception.html | `2dce046fc9faacb0d06d0e81a24c86bcaea74fa7737b3f0d06abb6c1e6883181` |
| appsscript.json | `6d4529b2eddb34c66f4b5452ec4c7d2631b5385d984c68aaa6b0281ffe9eb8ad` |

Compared with the reviewed v29 baseline at `f82eca47fd6c0ad2d93113c0f49e40f8519d58a0`, only Code.gs and Monitor.gs changed (manifest compared as parsed JSON). Code.gs adds the degraded-reply TTS restriction, independent `TTS_ENABLED` kill switch and cache claim under a script lock. Monitor.gs parses timestamps numerically, preserves an unreadable-clock state and changes alarm deduplication.

Those two file deltas were reconciled into PR #5 with a three-way comparison against the v29 baseline, preserving the proposed callback, wording and build-health changes. The reconciled Monitor.gs exactly matches the LF v30 read-back hash. Code.gs and Auth.gs intentionally differ from production because they also contain proposed review changes. This is no claim that the repair branch is deployed.

Four regression tests preserve the TTS kill switch, readable-clock selection despite malformed cells, unknown-clock state and alarms with unreadable timestamps. The first three fail against the unreconciled v29 source and pass after reconciliation. Existing build-health and callback tests also pass.

The v30 cache-lock implementation explicitly does not claim exactly-once semantics under cache replica lag. The stronger property-backed claims and independent audio budgets remain the PR #4 proposal. Its integration must retain v30's kill switch and Monitor.gs changes. Monitor trigger execution, application behavior on staging and production promotion remain separate acceptance checks.
