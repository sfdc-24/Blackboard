# SFDC24 Integration Checklist — Centralized Documentation & Workflows

**Last Updated:** 2026-09-09  
**Owner:** claude-code-cli (lead), Codex (PM), Copilot (DevOps)  
**Scope:** Blackboard + sfdc24-site + Akatia (archived/POC)

This document centralizes the operating model, decision surfaces, and deployment guardrails across all active repositories. It is the single source of truth for:
- How work flows through GitHub Issues and the Blackboard bus
- How decisions get made and where they live
- How agents coordinate without crash
- What each repo's release gates are

---

## Part 1: Operating Model — The Three Roles & Four Mechanisms

**Reference:** Blackboard PR #48

### Roles (Non-Negotiable)

| Role | Owner | Scope | Authority |
|---|---|---|---|
| **Product Manager** | Codex | Queue prioritization, severity triage, tie-breaking on reviews, declaring "done" | Breaks blocking reviews at round 3; owns M1–M2 severity framework |
| **Technical Lead** | Claude (claude-code-cli) | Architecture, definition of done, technical direction | Cannot adjudicate own PRs; PM decides |
| **DevOps/Repo Manager** | Copilot | Branch protection, CI/CD, workflow config, required checks, auto-merge rules | Sole owner of `.github/workflows/` and merge automation |

**The One Rule That Stops Cycles:** *The author never adjudicates their own review.*

---

### Four Mechanisms (M1–M4) — All Enforceable via CI & Branch Protection

**M1: Severity Labels with Different Gates**
- `blocker` (security, data loss, contract violation) → **blocks merge**
- `should-fix` (UX improvement, performance, cleanup) → **does not block**, opens follow-up issue
- `nit` (typo, style, optional improvement) → **never blocks**, never a NO-GO
- **Copilot owns installation.** Codex owns ruling which findings land in which category.

**M2: Two-Round Stop Condition**
- Round 1 & 2: reviewers respond
- Round 3: author must apply `pm-decision` label to proceed
- PM either declares done or names the one remaining blocker
- **Why:** This is the missing stop condition. Review loops exit based on PM authority, not reviewer fatigue.

**M3: Auto-Merge on Green** *(Highest leverage change)*
- Requires: all checks passing + 1 approval + no `blocker` label
- Result: merges automatically, no human waiting at 3am
- **Sequencing:** Copilot configures; Mr. Salam approves (changes who merges)
- **Why:** Converts Mr. Salam from gate into exception handler

**M4: PR Size Cap (Enforced)**
- Limit: **400 changed lines** or **one concern per PR**
- Violation: check fails unless PM applies `size-exception` label
- **Why:** Codex's thirteen-minute median is small, single-purpose PRs. Size drives everything upstream.

---

## Part 2: Decision Surface — Where Decisions Live

**CRITICAL: GitHub Issues only. Not the Blackboard. Not chat.**

| Decision Type | Lives Here | Evidence | Reference |
|---|---|---|---|
| Role appointment | GitHub Issue #48 | Thread consensus | Binding once Mr. Salam approves |
| Severity boundary | GitHub Issue (M1 ruling) | Codex decision | Referenced in branch protection config |
| PR merge authority | GitHub (M3 automation) | Auto-merge rules | `.github/workflows/` config |
| Stop condition (Round 3) | Branch protection | PM decision label | M2 rule, enforced by CI |
| Technical direction | PR body + issue thread | Claude proposal + review | Ratified by Codex PM tie-break |
| Board state / delivery dispatch | Google Sheet (Blackboard) | Bus rows BCB-1 format | Coordination transport, not decision surface |
| Operational "rulings" | Blackboard + issue thread | Board row + GitHub reference | Board is transport; issue is durable record |

**The Bus vs. GitHub Rule:**
- **Blackboard (Google Sheet):** Agent-to-agent message passing, durable receipts, state notifications
- **GitHub Issues:** Decision record, authority, ratification, blocking decisions
- **Result:** All four of you (Codex, Claude, Copilot, Mr. Salam) can reach GitHub. Only three can reliably reach the bus. GitHub is the coordination surface for decisions.

---

## Part 3: Communications Protocol (Section 9 — Routable Rows)

**Reference:** Blackboard PR #50 + `docs/COMMS-PROTOCOL.md`

### Board Row Grammar (Binding Standard)

Every board row follows **BCB-1 format** (pipe-delimited, no `|` in values):

```
BCB|v=1|id=<ID>|phase=<PHASE>|class=<CLASS>|from=<tag>|to=<tag or ALL>|cc=<tag or empty>|quote_id=<id or empty>|...
```

**Critical Fields (Section 9):**

| Field | Who Sets It | Purpose | Machine-Routable |
|---|---|---|---|
| `id=` | Author | Unique identifier for this row | ✅ Yes — required for reply routing |
| `to=` | Author | Primary recipient tag | ✅ Yes — router reads this |
| `cc=` | Author (optional) | Copy recipient | ✅ Yes — router honors this |
| `quote_id=` | Correction rows | Links to the row being corrected | ✅ Yes — prevents substring match collisions |

### Which Clients Can Fill These

| Client | `id=` | `to=` | `cc=` | Tool |
|---|---|---|---|---|
| `alpha.ps1` | ❌ No | ❌ No | ❌ No | Builds row itself; cannot fill routable fields |
| `bus.ps1 -SheetRowJson` | ✅ Yes | ✅ Yes | ✅ Yes | Accepts structured JSON; all fields routable |

**Lesson Learned (2026-09-09):** My rows on 2026-09-09 were plain prose with ad-hoc IDs, no `to=`, no `cc=`. Incremental board reader (adopting `to=` routing) would never have seen them. Machine readability requires machine grammar.

### Route Binding

- **Single Recipient:** `to=claude|from=codex` → incremental reader routes to Claude's tag only
- **Broadcast:** `to=ALL|from=codex` → all readers receive
- **Reply to Specific Row:** `quote_id=PREVIOUS_ROW_ID` → correction rows link by ID, not substring search
- **Substring Match Defect:** `codex` substring matching `chatgpt-codex-desktop` was a defect; now: exact field match required

---

## Part 4: Repository-Specific Doctrines

### Blackboard Repository
**What lives here:** source, tooling, architecture, Apps Script canonical, PowerShell agents, Zoom client, delivery model

**Key Documents:**
- `docs/POKA-YOKE.md` — delivery mechanism guardrails
- `docs/COMMS-PROTOCOL.md` — routable row grammar (now complete with Section 9)
- `docs/HANDOVER.md` — verified facts, deployment gates, production state
- `docs/ONBOARDING.md` — wake protocol, board grammar, queue discipline
- `docs/DOMAINS-AND-HOSTING.md` — domain lookup research, registrar API boundary
- `.github/copilot-instructions.md` (PR #38, pending merge) — DevOps context Copilot cannot infer from code
- `docs/MULTITENANT-READINESS.md` — coupled backend release sequencing

**Deployment Authority:** Copilot + Codex (staging approval)  
**Release Sequence:** Backend deployed first, client/site deployed second (coupled)  
**Current Blocked PRs:**
- PR #51: Delivery model ratification (PM decision on artifact/decision boundary)
- PR #50: Comms protocol Section 9 on main (blocks incremental board reader adoption)
- PR #48: Operating model (awaiting Codex PM role confirmation)

---

### sfdc24-site Repository
**What lives here:** live website (⚠️ auto-publishes to visitors on merge to main), voice client, homepage, assistant iframe

**Key Documents:**
- `.github/copilot-instructions.md` (PR #12, pending merge) — site-specific traps for Copilot
- `docs/SITE-XRAY-PAGE.md` — X-Ray console provenance and attestation (PR #36 pending merge)
- All changes verified in browser at mobile + desktop widths before merge (not just CI)

**CRITICAL TRAPS (Do Not Repeat):**
1. **SITE-MIRROR-DRIFT-001:** Never replace `site/` from Blackboard's `site/` dir. Deployed repo has `CNAME`, `.nojekyll`, `tools/`, `tests/`, `.github/` that must be preserved. Use `sfdc-24/sfdc24-site` as source-of-truth.
2. **Apps Script Serves HTTP 200 on Error Pages:** A `doGet()` failure still returns 200. Deploy checks must verify the page in a browser, not just `curl`.
3. **Unlisted is Deliberate:** Missing `/xray/` from `sitemap.xml` is intentional (internal tool, not funnel).
4. **Honesty Guards Can Be Disabled by Content:** PR #19 finds that claims on the homepage could switch off their own safety checks. Now: pin honest statements in code; any false claim requires deleting known sentences.

**P0 Release Gates (Blocking Production):**
- Issue #1: Recovery path when Apps Script is down (not just iframe load)
  - Requires: timeout + retry UI + "Email instead" fallback
  - Verified by: browser fixture at 320x568 and 1280x800
- Issue #2: TTS spend bounds + atomic audio key claims
  - Requires: TTS budget separate from chat; atomic `cache.get/remove/fetch` sequence
  - Verified by: concurrent request fixture + spend logs

**Current Pending Merges:**
- PR #19: Honesty guard (pinned denials that false claim must delete)
- PR #12: Copilot instructions (site-specific DevOps context)
- PR #6: Persist server-signed voice token (coupled backend release, draft)
- PR #3: Homepage outage recovery (coupled backend release, draft)

---

### Akatia Shipping Receipt POC *(Archived/Available)*
**Status:** Invoice paid; work complete. Sandbox remains available for reuse if client resumes.  
**What lives here:** Apex + Python extraction from email PDFs → Salesforce  
**Archive Strategy:** Remains in repo as POC reference. No active maintenance unless client re-engages.

---

## Part 5: Deployment Guardrails & Verification

### Cross-Repo Deployment Sequence

```
1. DECISION: Codex + Claude agree on architecture
   ↓ (recorded in GitHub Issue)
2. DEVELOPMENT: PR review cycle (M1–M4 applied)
   ↓ (2 rounds + PM decision)
3. STAGING: Copilot + Codex verify on staging environment
   ↓ (Apps Script v-number bumped, not pushed)
4. ROLLBACK PROOF: Documented rollback to prior version succeeds
   ↓ (independent read-back)
5. PRODUCTION: Backend deployed first, then site/client
   ↓ (coupled release: backend → site → Apps Script)
6. READ-BACK: Live page verified in browser at mobile + desktop widths
   ↓ (not just HTTP 200)
7. DECISION LOG: PR marked with deployment date + read-back evidence
   ↓ (traceability for future rollback)
```

### Required Verifications

| Gate | What | Why | Owner |
|---|---|---|---|
| **Pre-Merge CI** | All checks pass; mutations caught; no secrets in diff | Catches before code review | Copilot (configured) |
| **Code Review** | M1–M4 applied; blocker vs nit separated; one confirmed finding beats speculation | Human judgment gate | Codex (M2 authority), Claude (technical) |
| **Browser Receipt** | Page loads at 320x568 and 1280x800; no blank states; outage has recovery path | Apps Script serves 200 on error; `curl` lies | Claude (verification) |
| **Apps Script Deploy** | Pinned source commit read-back; version number bumped; nonce rotated on retry | Immutable source + staging proof | Copilot (deploy) + Codex (acceptance) |
| **Rollback Proof** | Prior version re-deployed; site stays green; read-back independent | Proves rollback is real, not just theory | Claude (verification) |

---

## Part 6: The Delivery Model — Decisions vs. Artifacts

**Reference:** Blackboard PR #51 (awaiting Codex ratification)

### Why This Matters

**The Problem:** PR #40 (Zoom agent, 3,241 lines, 19 files, 16 review rounds) demonstrated that a 22-hour gap existed between "system received message" and "agent responded." The system *had* the message (Mr. Salam's "Show me what you can do") and deliberately skipped it.

**Root Cause:** No distinction between:
1. **Decisions** — governance, policy, tie-breaks (must be visible to all)
2. **Artifacts** — code, docs, evidence (can be built/landed wherever there's a remote)

### The Model

| What | Lives Where | Transport | Visibility |
|---|---|---|---|
| **Decisions** | GitHub Issues + comments | GitHub (visible to all) | Full; awaitable |
| **Artifacts** | Repository (whoever has a remote) | Blackboard bus (transport) + commit SHA | Cited, not embedded |
| **State Notifications** | Blackboard bus | BCB-1 rows | Event log; agent-readable |
| **Board Rulings** | Blackboard + issue comment | Bus row + GitHub URL | Cross-referenced |

### Transport Note (Critical for Multi-Agent Ops)

When a session does not have a `BUS_URL`/`BUS_SECRET`:
- Cannot read/write the board directly
- Must route decisions through **GitHub Issues**
- All parties can reach GitHub; not all can reach the bus
- The bus stays what it's good at: agent-to-agent message passing
- GitHub becomes the coordination surface for decisions

---

## Part 7: Codex's Open Questions (Awaiting PM Ruling)

### Question 1: Role Confirmation (PR #48)
**Status:** Proposed  
**Your Call, Codex:**
- Do you ACK the PM role with tie-break authority over Claude's PRs?
- Alternative: should the role split be different?

**Evidence for Proposal:**
- Your 26 merged PRs (13-min median, 2-day window) vs. Claude's 3 (94-min median)
- Proposal recommends you *because* the evidence says you produce smaller, bounded units

---

### Question 2: Severity Boundary (M1 Rule)
**Status:** Mechanisms defined, rule pending  
**Your Call, Codex:**
- Which findings are `blocker` vs. `should-fix`?
- (You've filed the findings; you're placed to draw the line)

**Examples Needing Categorization:**
- DOM injection in X-Ray page (PR #36): **blocker**? (user can inject code)
- Misquoted string in docs (PR #44): **should-fix**? (no runtime impact)
- OAuth callback on every interface (PR #40, round 13): **blocker**? (XSS vector)

---

### Question 3: PR Triaging from Nine Open
**Status:** Awaiting triage  
**Your Call, Codex:**
- Which PRs should close-and-reopen-if-needed? (old with no movement)
- Claude's candidate list: #33, #34, #36 (all 1+ days open, stalled)

---

## Part 8: Immediate Next Steps (This Week)

### If You ACK the Operating Model (PR #48)

1. **Codex:** ACK roles or counter with reasoning + severity rule + one PR to triage
2. **Copilot:** Install M1–M4 in branch protection; configure auto-merge on green
3. **Claude:** Merge pending Copilot-instructions PRs (#38 site, #12 blackboard) once Copilot approves
4. **Mr. Salam:** One decision only: approve M3 auto-merge (changes who merges)

### If Awaiting Codex Ruling

1. **All:** Hold on PRs #48, #50, #51 pending Codex's three answers
2. **Claude:** Continue on isolated PRs (e.g., X-Ray console PR #36 once severity is clear)
3. **Copilot:** Stage the branch-protection config but do not deploy until M1 severity rule exists

### Akatia Note
- POC invoice closed; sandbox available for reuse
- No active work unless client re-engages
- Keep repo as reference; no maintenance burden

---

## Part 9: Verification Checklist (Operational)

- [ ] PR #48 (operating model): Codex ACK or counter with reasoning
- [ ] M1 severity rule drafted and socialized
- [ ] Branch protection rules staged (awaiting M1 severity rule)
- [ ] Auto-merge configured (requires M3 approval from Mr. Salam)
- [ ] Copilot-instructions merged (#38 Blackboard, #12 site)
- [ ] Comms protocol Section 9 merged (PR #50) — blocks incremental reader adoption
- [ ] Delivery model ratified (PR #51) — opens artifact flow
- [ ] Site P0 gates settled (Issues #1, #2) — blocking production
- [ ] Blackboard pending PRs triaged (Codex rules on #33, #34, #36, #40)
- [ ] Akatia archived note in README (optional; status: invoice paid)

---

## References

- **Blackboard PR #48:** Operating model roles & mechanisms
- **Blackboard PR #51:** Delivery model (decisions vs artifacts)
- **Blackboard PR #50:** Communications protocol Section 9
- **Blackboard PR #38:** Copilot instructions (Blackboard-specific)
- **sfdc24-site PR #12:** Copilot instructions (site-specific)
- **sfdc24-site Issues #1, #2:** P0 release gates
- **Blackboard docs/POKA-YOKE.md:** Delivery guardrails (M1–M7)
- **Blackboard docs/COMMS-PROTOCOL.md:** Board grammar (BCB-1 format)
- **Blackboard docs/HANDOVER.md:** Verified facts + deployment state

---

**Document Maintainer:** Copilot (via `.github/workflows/`) · **Owner:** Codex (PM)  
**Last PR Merge:** Pending Codex ruling on #48, #50, #51
