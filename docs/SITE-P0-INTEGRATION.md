# Website backend integration with the reviewed staging build

PR #4 is stacked on PR #5 (`codex/review-followups`) so it can use the repaired staging pipeline and reconciled Governor v30 source without duplicating the CI/CD review. Its integration parent is `1d26dde2c40d25cbb95e529e449e5922dc778df0`.

The independent source read-back and seven v30 baseline hashes are in [CICD-GOVERNOR-V30.md](CICD-GOVERNOR-V30.md). This integration retains v30's `TTS_ENABLED` kill switch, degraded-reply restriction and Monitor.gs timestamp/alarm changes. It replaces the cache-only claim implementation with PR #4's property-backed claim and independent site/session audio budgets. The obsolete cache-only helper is removed. Reception retains its application-ready nonce handshake; the new build-health route remains independent of visitor chat or paid providers.

The local combined suite has 91 passing tests: the 82-test CI/CD/v30 suite plus nine voice/readiness tests. Two new voice cases verify that disabling TTS prevents new audio keys and blocks provider calls for keys issued before the switch was disabled. All provider requests and Google API/project methods used by tests are mocked. The voice workflow uses verified commit pins for checkout/setup-node and disables persisted checkout credentials.

Review the small PR #4 delta against PR #5, then adopt through the existing CI/CD owner lane. The source/build protocol must produce real staging deploy/rollback receipts, and the paired static-site behavior must be accepted on staging before production. Green offline tests are not live acceptance. No production source, deployment configuration, Google credentials, Salesforce records or DNS changed during this integration.
