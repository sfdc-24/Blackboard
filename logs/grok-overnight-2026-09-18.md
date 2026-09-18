# Grok overnight notes — 2026-09-18 (EDT)

**Worker:** grok-bot (executor subagent on Grok Computer box)  
**Authority:** Mr. Salam overnight pass · FREE/CHEAP · no paid Cloud Agents · no live homepage overwrite  
**Evidence levels:** marked TESTED / BELIEVED / READ-NOT-DEMONSTRATED

---

## Orient (sources)

Could not shell into AkatiaVM `C:\Users\akatiawam\blackboard` from this box (no free remote-shell tool in this subagent session; machineIds granted `always` in settings but Local Tool surface not exposed here). Oriented instead from:

| Source | Access |
|---|---|
| `sfdc-24/Blackboard` docs via GitHub MCP | PRODUCT · ONBOARDING · COMMS-PROTOCOL · HANDOVER · FLEET-EFFICIENCY · DELIVERY-OPERATING-MODEL |
| Recent git activity (`gh`/MCP list_commits) | through 2026-09-17T19:22Z (~3:22 PM EDT Sep 17) |
| Bus `time` | TESTED ok from this box |
| Notion Project Board | BELIEVED (snapshot Sep 15) |

**docs/ present in repo (skimmed):** PRODUCT, ONBOARDING, COMMS-PROTOCOL, HANDOVER, FLEET-EFFICIENCY, DELIVERY-OPERATING-MODEL, POKA-YOKE, TRUE-NORTH, CICD*, GCLOUD*, CODEX-*, WHATSAPP-GATEWAY-REPAIR, peer-notes/, lessons/, evidence/
**ABOUT-US:** not found as a standalone file on Blackboard `main` (may exist only on VM).
**scripts/:** bus.ps1, bus.py, wa_send.py, pipedream_wa_*.js present on remote; `pipedream_connect.py` and `grok_wa_inbox.py` were missing — recreated in staging.

---

## Polymorphism routing — Claude vs Codex (from existing docs + git)

### Role split (DELIVERY-OPERATING-MODEL — in force)

| Lane | Role | Strengths (documented) | Weaknesses / failure modes (documented) |
|---|---|---|---|
| **Codex** (`chatgpt-codex-desktop` / `codex/*` branches) | **PM** — queue, escalations, **tie-break** | Tight review of evidence vs claims; catches unreachable commits, stale reviews, M6/M7 holes; disciplined BCB rows; recovery PRs on ORDER cutover | Workspace sometimes lacked `origin` remote — decisions must land on issues/PRs not orphan commits |
| **Claude** (`claude-code-cli` / `vm-claude-code-cli`) | **Technical lead** — implementation + technical case | High volume shipping (ORDER Linux port, presenter tools, CLIPS governor); strong measurement culture; owns clasp/laptop-only surfaces | Shared-tag collisions (ISS-009 / L-82); can assert before testing; prose board rows without BCB routing; duplicate work when claim not atomic (M5) |
| **Copilot** | Repo / DevOps | Cold-diff reviews found real defects | Cannot read the board — PR is only channel |

### Git activity pattern (BELIEVED from commit list)

- Codex-authored recovery chain on ORDER disabled/Observe paths: `codex/01a0a7b3-*` — small, evidence-heavy, fail-closed.
- Claude-coauthored large ports: ORDER supervisor Linux (#73), presenter tools (#134), CLIPS governor (#129).
- Notion Sep 15: rules on GCloud decide who does what; short Claude sessions do the work; laptop for design and approvals.

### Routing cheat-sheet

1. Salesforce / Omni / Headless / Apex / Flow → Claude primary · SA · Codex gate · BA
2. Release / PR / evidence / green-gate truth → Codex PM · Copilot review · Claude implements
3. WhatsApp / Meta → Meta lane + Grok (COMMS: gateway lanes without board are confident noise)
4. Ops / Ubuntu / Azure VM → VM · Codex contracts · Claude when clasp needed
5. Cool / story / brand → Gemini + Grok (additive `/cool/` only)
6. Never treat Pipedream auto-reply claude/gemini WA lanes as fleet peers

---

## Staging on Grok box

Mirror: `/workspace/akatia-blackboard-staging/` → intended `C:\Users\akatiawam\blackboard\`

| Artifact | Path |
|---|---|
| Notes | `logs/grok-overnight-2026-09-18.md` |
| Cool option | `site/cool/index.html` |
| Archive pointer | `site/archive/homepage-2026-09-18/README.md` |
| Scripts | `pipedream_connect.py`, `grok_wa_inbox.py`, `bus.py` |

## Open for morning

1. Copy staging onto AkatiaVM filesystem (this session had no free remote shell).
2. Confirm ABOUT-US on VM if expected.
3. Promote cool to `sfdc-24/sfdc24-site` review branch — do not touch live index.html without archive.
4. Pipedream gateway repair still offline-contract only — Connect check is OAuth/project probe.
5. GitHub Actions billing block may still halt hosted CI.
