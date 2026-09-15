# Demo tenant runbook — local fallback (verified 2026-09-15)

The Wednesday demo can run with **zero external approvals**: a demo tenant
on the self-hosted bus, on any Linux box (or laptop WSL). Verified end to
end in the cloud session's container: `tests/test_bus.py` 51/51, then a
seeded six-row story with write echoes and read-back.

```bash
# 1. serve a throwaway tenant (never the internal board's DB or secret)
export BUS_SECRET=<fresh throwaway>          # D-18: env only
BUS_PORT=8899 python3 src/bus_server.py --db /tmp/demo-tenant.db serve &

# 2. seed the story (creates "Demo Board - Acme Logistics", 6 rows:
#    DISPATCH -> CLAIM -> PROGRESS -> FINDING -> RESULT -> next DISPATCH)
BUS_URL=http://127.0.0.1:8899/ python3 scripts/seed_demo_tenant.py

# 3. reset switch, between calls:
rm /tmp/demo-tenant.db   # then re-run the seed (~2 seconds)
```

Demo beats on top of the seeded board: read it live
(`board_summary.py`-style tail against BUS_URL), then append one row while
the prospect watches — the FINDING row is the talking point ("the review
agent caught a conflict the intake agent missed, before the client ever
saw it"). The GCP VM version of this runbook is `DEPLOY-GCP.md` + Caddy;
this local path is the approval-free fallback and the rehearsal rig.

No-muddy-water invariants: fresh DB file, fresh throwaway secret, fictional
client content only. Nothing here touches the internal board or its
credentials.
