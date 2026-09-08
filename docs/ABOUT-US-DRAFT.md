# About — draft copy

Written 2026-09-07 by claude-code-cli at Mr. Salam's request. **Not published, and
not for publishing until he has read it.** It is about him, so he gets the last
word on every line.

Facts about him come from his public LinkedIn profile, which he pointed me to.
Everything about how the site was built comes from this repository's own record.
**Nothing here is invented.** The three places I could not verify were marked
`[CONFIRM]` and Mr. Salam answered all three on 2026-09-08: the volunteer
paragraph is cut, Gemini stays named, and the one-week trial section is dropped
rather than given invented numbers. No unverified claim remains.

---

## Who you are dealing with

**Abdus Salam. Principal Consultant, SFDC 24. Mississauga, Ontario.**

Fourteen years in enterprise consulting, and since 2022 doing it as one person
rather than as part of an agency. Salesforce Certified Platform Administrator and
Sales Cloud Consultant. Certified ScrumMaster. Six Sigma Black Belt. More
recently, certifications in generative AI applications and in internet security —
which is the part that matters most for what this site is, and why the AI work
here is bounded rather than enthusiastic.

The work has not been theoretical. A professional identification portal designed
and implemented across Salesforce and WordPress. Two years on Access Haiti's
supply chain implementation in Salesforce. The kind of engagements where the
system has to keep working after the consultant leaves.

---

## How this site was built, honestly

This site was built by a human directing a fleet of AI agents that work on the
same shared record and check each other's work. That sentence is easy to write
and usually a lie, so here is what it actually means.

**Claude** — Anthropic — does most of the building. It writes the code, deploys
it, and monitors the live site. It also wrote this page.

**ChatGPT** — OpenAI, working as Codex — reviews and accepts. It has independent
authority to reject Claude's work, and it uses it. On the day before this page
was written it rejected one of Claude's release claims, correctly, after testing
the live homepage in a way Claude had not thought of.

**Meta AI** establishes what is actually possible on WhatsApp, which is how
Mr. Salam is reached when something needs a human decision.

**Google Gemini** works on architecture and reviews the same code from a
different angle.

**Salesforce's Headless 360 framework** is how the Salesforce side is built and
inspected without a browser sitting in the middle of it.

### The part most companies leave out

They disagree, in writing, and the disagreements are kept.

On one ordinary day the agents corrected each other eight times. A drift check
was found to be wrong twice by a peer. A release was blocked because the evidence
behind it did not cover the case it claimed to. A monitoring system that had
reported "all clear" for three days was found to have never been able to send an
alert at all — it had been failing silently since the day it was built.

None of that was hidden, because a record nobody can lose is the only reason any
of it gets caught. **Anything consequential is reviewed and signed off by a human
before it becomes a decision or an action.** That arrangement is deliberate: you
should not have to understand or manage a fleet of agents to get value from one.

---

## What that gets you

**Speed on the tedious part.** Reading a tangled org, cross-referencing what the
documentation claims against what the system does, drafting the change, writing
the tests. Work that used to be billed by the week.

**A second opinion that is not a favour.** Most consulting reviews are one person
checking their own reasoning. Here, work is checked by something that did not
write it and has no stake in it having been right.

**An audit trail as a by-product.** Every decision, every measurement and every
correction is written down as it happens, not reconstructed afterwards for a
report. When somebody asks in six months why a thing was done, the answer exists
with its evidence attached.

**One accountable human.** Not a bench, not a rotating team, not a junior with a
methodology. The same person who takes the call makes the calls.

---

## Notes for whoever ships this

- Nothing above claims the site does something it does not do today. The four
  beats described in `PRODUCT.md` are deliberately absent, because they are not
  live yet. See `HOMEPAGE-REWRITE-PROPOSAL.md` for why that rule matters.
- The eight-corrections paragraph is checkable against the board. Keep it that
  way — the moment it becomes a round number nobody verified, it is marketing.
- The volunteer paragraph was cut on his instruction (2026-09-08). It is worth
  saying why the page is not poorer for it: what it argued — that he is used to
  understanding people quickly — the rest of the page now has to earn by being
  clear rather than by claiming it.
- There is deliberately no pricing and no trial offer. He dropped that section
  rather than let a number onto his own About page that he had not set.
