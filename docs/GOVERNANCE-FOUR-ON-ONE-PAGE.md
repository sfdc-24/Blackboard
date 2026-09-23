# Four governance documents, on one page

Four pull requests have been open since 11–15 September proposing how this fleet
should work: **#75, #101, #110, #112**. They were written eight to twelve days
ago and have not been read together. This is what each asks for, what has
happened since that confirms or kills it, and the smallest number of decisions
that closes all four.

**They are not four decisions. They are two.**

---

## The four, in one line each

| PR | who, when | asks for |
|---|---|---|
| **#112** | claude-code-cli, 15 Sep, 297 lines | Move rule-shaped decisions off large models onto an always-on Python rule engine; make model sessions short and bounded; keep the laptop for design and approvals. |
| **#110** | 14 Sep, 231 lines | A dated architecture snapshot — every component with its evidence *and its limit* — plus mother/child isolation options, a data-store decision table, and a standing review mandate. |
| **#101** | codex, 14 Sep, 51 lines | A review companion naming **five material gaps** in identity, tenancy, idempotency and migration, and asking each participant to return up to three defects against it. |
| **#75** | claude, 11 Sep, 595 lines / 5 files | *Clients never touch the working board.* Per-client demo tenants with a reset switch, plus the bus security roadmap that has to exist first. |

---

## What has happened since they were written

This is the part that should decide it. Four of their claims have since been
tested by events.

**#101's gap 4 was proven right, expensively.** It said on 14 September that
*"Apps Script, Python and Alpha are distinct contracts; source/deployed
equivalence is unproved."* On 22 September that turned out to be exactly true:
**nine test suites could not open the file they were guarding**, the workflow
path filters had silently stopped triggering, and once the suites could load,
**28 assertions failed against the live deployment** — no TTS spend counters, no
signed conversation identity, a sign-in checking its issuer with a substring.
All of it had been written, reviewed and CI-verified against a source that was
never deployed. **codex called this eight days early and nothing was done with
it.**

**#101's gap 1 has been half-answered.** *"Shared-secret authentication and
caller-supplied actor labels do not establish distinct identities."* On the site
that is now fixed: v46 replaced a caller-chosen `vid` with a server-signed
conversation identity. On the bus it is still true.

**#112's autonomy finding held.** It said progress needs an open interactive
session. On 22 September a cloud session could not even *start* from this
checkout — both worktree and remote isolation refused on a path-case mismatch —
and nine scheduled tasks remain the only unattended work, all laptop-bound. If
the laptop is off, the fleet is silent.

**#112's cost finding is the one with money attached and it has not been
revisited.** Seven days of laptop Claude measured at **$2,009 at API list
price**, of which **72% was cache reads** — an average of **486,000 tokens
re-read per response**. Nothing since has changed that shape.

**#75's premise got more urgent, not less.** *Clients never touch the working
board.* The `/stream/` page now creates Salesforce **Leads** from a spoken
session, and the org those Leads land in was unidentified until yesterday.

---

## Decision A — how this runs, and on what

**#112, #110 and #101 are three views of one question.** #112 proposes the
operating model, #110 inventories what exists and what it is allowed to become,
#101 lists what must be true before any of it touches a client.

**What yes commits to:** building a small always-on rule service on the existing
GCE instance that decides ownership, stalls, collisions and hygiene, and
shortening model sessions to bounded briefs. #112 costs this honestly — *"moving
today's work to the API unchanged would cost more, not less"* — so the saving is
conditional on the sessions actually getting shorter.

**What yes does not commit to:** CLIPS. #112 already concedes codex's point that
a plain Python policy table gives the same explanation traces, and says to build
the table first and adopt `clipspy` only if it demonstrably wins on the same
fixtures. No cloud purchase and no Postgres decision is approved by saying yes
here.

**What waiting costs:** the re-read bill continues, and every rule-shaped
decision keeps being made by a large model reading the whole history — or not
made at all.

**My recommendation:** **yes to #112's direction, merge #110 as a dated
reference, and adopt #101's five gaps as the acceptance checklist.** Then close
all three. #110 is not really a proposal — it is a snapshot with honest limits
attached, and its value decays every day it sits unmerged. #101 has already been
vindicated once; treating it as a checklist rather than a debate is the cheapest
way to honour that.

**The one thing I would change:** #112 assumes the fleet's shape of 15
September. grok is out of allowance, codex is moving to OpenAI cloud, and the
scheduled tasks all still run on the laptop. The rule engine should be specified
against **today's** fleet, not that one.

---

## Decision B — whether clients touch anything

**#75 is the separate one.** Its principle — *clients never touch the working
board; a demo tenant is a mechanism, not discipline* — is still right and is now
load-bearing, because the site creates Leads.

**But its workplan is dead.** It is a plan "to Wednesday 2026-09-16", with
hourly status updates in a format that stopped being used. Merging it as written
would put a timetable a week in the past into `main` as though it were live.

**My recommendation:** **take the principle and the security roadmap, drop the
workplan.** `BUS-SECURITY-HARDENING.md` is a genuine inventory of what is already
right and what is not, and it should land. `CLIENT-DEMO-READINESS-PLAN.md` should
be rewritten against a real demo date or closed. The seed script and tenant
runbook only matter once a client is actually being invited.

**What waiting costs:** nothing today — no client is on the bus. The risk starts
the day one is, and that day is now closer than it was, because the streaming
page has a lead path.

---

## So: two yeses, and what I do with each

| you say | I do |
|---|---|
| **A: yes** | Merge #110 as a dated snapshot. Adopt #101's five gaps as a written acceptance checklist and close it with the review it asked for. Re-spec #112's rule engine against today's fleet, starting with the Python policy table on incident fixtures — not CLIPS. |
| **A: not yet** | Close all three with the reason, so they stop presenting as live proposals. The evidence in them stays in the repository either way. |
| **B: yes** | Land `BUS-SECURITY-HARDENING.md`, rewrite the readiness plan against a real date, park the seed script until a client is named. |
| **B: not yet** | Land the hardening roadmap only. Close the rest of #75. |

Either way **four pull requests stop being open**, and nothing in them is lost —
the branches survive and every close carries its reason.

Related: `docs/PR-BACKLOG-TRIAGE-20260923.md` for the other fifteen.
