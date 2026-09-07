# About — draft copy

Written 2026-09-07 by claude-code-cli at Mr. Salam's request. **Not published, and
not for publishing until he has read it.** It is about him, so he gets the last
word on every line.

Facts about him come from his public LinkedIn profile, which he pointed me to.
Everything about how the site was built comes from this repository's own record.
**Nothing here is invented, and the three places I could not verify something are
marked `[CONFIRM]` rather than filled in with something plausible.**

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

Since 2012 he has interviewed for ACCES Employment as a volunteer, helping people
who have just arrived in Canada find their footing in IT, and he leads web
development for Future Possibilities For Kids. That is not decoration on a
consulting page. Fourteen years of sitting across from people who need to be
understood quickly is most of what this business actually does.

*[CONFIRM] — whether you want the volunteer paragraph on a business page at all.
I think it is the most persuasive thing here, but it is yours to cut.*

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
different angle. *[CONFIRM] — you did not name Gemini in your list. It is in the
record, so I have included it; say the word and it comes out.*

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

## Try it for a week

If you want to see whether this works on your problem rather than on a case
study, there is a one-week trial.

Bring one real thing — a process that keeps breaking, a report nobody trusts, an
automation everyone routes around. Not a pilot chosen because it is safe. The
thing that is actually annoying you.

At the end of the week you have a written picture of what is happening, where it
breaks, and what to do first — and you keep it whether or not you continue.

*[CONFIRM] — the trial terms are yours and I have deliberately not invented them.
What does the week cost, if anything? How much of your time does it need? What
happens at the end if they want to carry on? I have described the shape only,
and left the numbers to you, because a made-up price on your own About page is
the one mistake there is no recovering from.*

---

## Notes for whoever ships this

- Nothing above claims the site does something it does not do today. The four
  beats described in `PRODUCT.md` are deliberately absent, because they are not
  live yet. See `HOMEPAGE-REWRITE-PROPOSAL.md` for why that rule matters.
- The eight-corrections paragraph is checkable against the board. Keep it that
  way — the moment it becomes a round number nobody verified, it is marketing.
- If the volunteer paragraph stays, it should stay in his words, not mine.
