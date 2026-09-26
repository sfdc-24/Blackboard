> **OBSOLETE 2026-09-26**: it plans around the Azure VM, which was dropped on 2026-09-24. Kept as history, do not follow it. How we work now: [`EXPRESS.md`](EXPRESS.md).

# The GCloud migration — why, the rules, and how to help

**Owner:** `claude-code-cli` (session `a4b334b8`), by Mr. Salam's direct
assignment and codex ruling `CODEX-01A0870A-USER-GCLOUD-CLAUDE-LEAD-20260910`.

**This file exists because Copilot has no board access.** Copilot contributes
through GitHub PR reviews and reads nothing else, so anything Copilot needs to
know has to live in the repo. Everyone else can read the board; this is the one
place all of us can read.

---

## 1. Why we are doing this at all

Not "cloud is nicer". A measured defect.

```
COPILOT-DEV-RG/AkatiaVM          priority=Spot     evictionPolicy=Deallocate
COPILOT-DEV-RG/AkatiaVM-regular  priority=Regular  (no eviction)
```

`AkatiaVM` is **preemptible**. Azure takes it away. That is what Mr. Salam meant
by a host that keeps *"pausing and having to disconnect every so often"*.

And it reframes the team roster. `vm-cli` had been silent 45 hours and
`vm-order-worker` 47 hours, and both were reported as idle agents. They are not
idle — **their host is preemptible**, and every wake mechanism we build on top of
a Spot VM inherits the eviction.

So the migration is not infrastructure hygiene. **It is the unblock for half the
workforce**, and it is ranked accordingly.

*(A second, independent reason some agents look idle: rows addressed to them are
unreachable by the documented wake read. That is a different defect, tracked in
`scripts/open_for_me.py`. Do not conflate the two.)*

---

## 2. The rules — Mr. Salam set these and they are binding

Four stages, in order. **Nobody skips ahead.**

| stage | what | state |
|---|---|---|
| 1 | Setup: applications and installation scripts on the instance | **COMPLETE** |
| 2 | Migrate all data and scripts into the instance | in progress |
| 3 | Run the POC and prove it works **independent of the Azure VM** | not started |
| 4 | After **at least 48 hours** of everything functioning, **HE** decides on decommission | his call, not ours |

**Three things that follow, and they are not negotiable:**

- **Azure stays running and untouched.** No power action, no schedule change, no
  task change, no decommission. Stage 3 proves independence; it does not switch
  anything off.
- **Nobody tells Mr. Salam that Azure is obsolete before stage 4.** Not in a
  board row, not in a PR, not in a status summary.
- **Decommission is his decision.** Ours is to give him the evidence to make it.

---

## 3. Where it actually is right now

Stage 1 is complete and was verified from the instance's own serial console
rather than asserted:

```
instance   blackboard-bus · project sfdc24 · zone us-east1-b
machine    e2-micro · Ubuntu 24.04 LTS · NOT preemptible
health     {"ok": true, "service": "sfdc24-blackboard-bus", "version": "1.0.1"}
hardening  User=blackboard-bus · ProtectSystem=strict · NoNewPrivileges=yes
bind       127.0.0.1:8787 — no fleet client can reach it yet, and none should
```

**Not preemptible is the entire point.** If a future change makes this instance
Spot to save money, it recreates the defect the migration exists to fix.

Two things worth knowing about how it got there, because both will come up again:

- The `.deb` was delivered as **base64 in instance metadata with a startup
  script**, not over `scp`. `gcloud` on Windows shells out to PuTTY, whose
  host-key cache prompt cannot be answered from a non-interactive session.
  Metadata is non-interactive and leaves an audit trail on the instance.
- **The bus secret is generated on the instance and never leaves it.** Nothing
  on the laptop has seen it, so it cannot reach a log, a transcript or a board
  row. It is deliberately *not* the Apps Script secret, which has leaked into
  source history before (ISSUE 025 / GW-DIAG-003).

---

## 4. Benchmark it — this is meant to become a product

Mr. Salam's direction: *"this type of large migration and system setup should be
blackboard's niche and a capability, so benchmark performance, execution before
and after so we can showcase this as a capability we can deliver for clients."*

So instrument **both hosts on the same measures**, before and after. A migration
nobody can measure is a migration nobody will buy, and an anecdote is not a
benchmark.

First numbers, from stage 1: **~4.5 minutes** from instance reset to a hardened
service answering health; **~10 minutes** from project selection to serving.
Those are a starting point, not the deliverable.

---

## 5. How to help — by role

### Copilot
**Your channel is the PR, and you are the most consistent reviewer we have** —
eleven reviews across nine PRs, and a real defect found in every one, including
two that would otherwise have shipped.

What is most useful from you on this lane:

- **Installation and packaging scripts.** `packaging/` and anything that runs on
  the instance. Shell correctness, quoting, error handling, idempotency.
- **Anything that assumes Windows.** Ten scripts in this repo are
  `#Requires -Version 5.1`, which does not exist on Linux at all. Flag any *new*
  code that adds to that pile.
- **Secret handling.** If a change would put a secret on a command line, in a
  log, in a board row or in a PR body, say so loudly. That is the failure mode
  this project has actually had.
- **Line endings.** A shebang ending in `\r` makes the kernel look for an
  interpreter named `bash\r`. `.gitattributes` pins the executable files, but
  new ones need pinning too.

You do not need board access to do any of that, and we are not going to pretend
you have context you cannot see. If a PR description assumes knowledge that is
only on the board, **say so and we will fix the description**.

### Gemini
You gate GCloud and Apps Script per the fleet role map. Two live questions:

1. **The project.** `sfdc24` is the recommendation — the only deliberately named
   project, already ADC's own quota project, Compute reachable, zero existing
   VMs so a first instance shares a network with nothing. Twenty of the
   twenty-four projects on the account are `sys-*` containers auto-created by
   Apps Script, including the one called "Blackboard Production".
2. **Agent Platform, and it changes the design.** If the ORDER worker and the two
   watchers run on Vertex AI Agent Engine rather than as systemd units on a VM,
   then the Linux box is only the **bus host** and Agent Platform is the
   **worker host** — different machines, different failure modes.
   **Agent Platform does not evict**, which is a better answer to "operates
   without pausing" than paying Regular priority to keep a VM awake.

### codex
Exact-head reviews and oversight, as now. Two specific asks:

- **Holds.** You own the holds on this lane. If a standing constraint applies to
  the migration, name it before it is tripped rather than after. A hold that is
  older than the newest VIEWPORT, or filed `Category=DONE`, is invisible to the
  documented wake read — that is how the G1 hold was breached.
- **The VIEWPORT clock.** v018 was written with a timestamp four hours in the
  future, so "newest by timestamp" selects a superseded row. Worth fixing at the
  writer, because the whole fleet reads its horizon that way.

### vm-cli
**`packaging/` is your lane per ONBOARDING.** Mr. Salam assigned the GCloud
migration to `claude-code-cli` directly, which supersedes that for this task —
but it is still your package. **Review rather than duplicate.** If you disagree
with a choice made here, say so on the board; it is easier to change now than
after stage 3.

### vm-claude-code-cli
Reviewer lane, and it has been working: three reproduced NO-GOs in one evening,
each with a negative control, plus two of your own errors caught and corrected
before you reported a number. That last part is why your reviews get acted on
immediately.

---

## 6. What nobody should do

- **Do not touch Azure.** Not power, not schedules, not tasks, not deletion.
- **Do not repoint a fleet client at the new bus.** It binds `127.0.0.1` and
  HTTPS has not been decided (`docs/DEPLOY-GCP.md` §3). The secret rides in
  request bodies; plaintext across the internet is not acceptable.
- **Do not make the instance preemptible.** That recreates the defect.
- **Do not put a secret anywhere it can be read** — command line, log, board row,
  PR body, commit message.
- **Do not declare Azure obsolete.** See §2.
