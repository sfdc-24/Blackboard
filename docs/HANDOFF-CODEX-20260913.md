# Handoff to codex — 2026-09-13 (standing, activates only if the Claude cloud session stops)

Mr. Salam, 2026-09-13: Claude usage limits approaching; if the cloud session
("SFDC24 and blackboarding") must stop, codex carries the coordination role.
Until then this is a *standby* document — do not duplicate live work.

## What the session owns today

1. **PR #75** (this branch): the client-demo readiness plan + bus security
   hardening roadmap. Draft, clean, docs-only, no checks by path design.
   Both fleet comments handled (billing correction verified and folded in).
2. **Status cadence to Mr. Salam**: twice daily (13:07Z / 22:07Z) in his
   format `<Workstream> - <NN>% / Capability built - ... / Pending - ...`.
   Grounded in main + open-PR state; skip entirely when nothing moved.
   WhatsApp once credentials exist; push-notification + session note until
   then. Last baselines: W1 25, W2 20, W3 45, W4 55, W5 20, W6 30, W7 50.
3. **Two items staged for the moment egress to `script.google.com` opens**
   from the cloud environment (still policy-blocked as of this writing):
   - Post the PR #70 Linux-verify row to the board. Payload (align
     `source_tag` to live board conventions first; one attempt then
     read-back per README-board-clients; #70 since **merged** as `4d4e76b`):

     ```json
     {
       "row_id": "CLAUDE-CLOUD-PR70-LINUX-VERIFY-20260911",
       "source_tag": "claude-cloud",
       "payload": "phase=VERIFY|id=CODEX-01A0870A-ORDER-PWSH7-MANIFEST-STRING-CLAIM-20260911|by=claude-cloud|pass=true|pr=70|exact_head=d09a4d05a955cccccd8989f1b9b49e4936218cd7|merged_head=4d4e76b|scope=linux-pwsh-7.5.4-only|evidence=baseline main 563fde3 fails 5/2 on the two replay cases; PR head 7/7 EXERCISED; mutation DateKind=Default flips to 5/2|note=full verdict on PR 70 review thread",
       "project_tag": "ORDER",
       "gist": "PR70 Linux leg independently verified GO"
     }
     ```
   - Dispatch **RQ1–RQ3** (fleet research questions) exactly as written in
     `CLIENT-DEMO-READINESS-PLAN.md` §4, answers tagged `DEMO-READY-RQ1..3`;
     synthesis lands back into that doc.

   Any instance with working bus credentials may post both — the cloud
   session's egress block is environmental, not a hold.

## Unblock queue owner: Mr. Salam (surface ONE at a time, L-86)

Current order: (1) egress allowlist `script.google.com` +
`script.googleusercontent.com` for env `env_015U7qtSJbKKzGPRAE6U4z4a`;
(2) WhatsApp creds (`META_TOKEN`, `WA_PHONE_NUMBER_ID`, `WA_TO`) for the
cadence; (3) GCP VM approval (unlocks W1 tenancy build AND PR #40's Linux
host); (4) Zoom Developer Pack credits; (5) PR #40 merge ruling.

## Standing decisions already made (do not relitigate)

- Demo isolation is mechanical: per-client bus tenants, demo-scoped keys,
  reset-to-seed; internal board never touches a client context.
- No CMS before the Wednesday demo gate; WordPress not the default.
- Salesforce lane = adopt official `salesforcecli/mcp`, extend for gaps;
  reconcile with PR #45.
- Demo shape plays to what exists: human/presenter-box presents, agent
  listens and answers (Zoom voice-out rides the presenter-box lane on main,
  not RTMS).
- Wednesday timeline in `CLIENT-DEMO-READINESS-PLAN.md` §6.

## Merge etiquette on PR #75

Docs-only; Mr. Salam is the audience. Merge when he says so or when the
plan's execution makes the docs the record; carry any factual corrections
as commits on `claude/nifty-wright-0aflya` with dated notes, as the
billing-HALT correction was.
