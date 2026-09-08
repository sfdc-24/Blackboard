# Attacking our own consensus — findings

2026-09-03. Three models independently reached the same five conclusions about
SFDC24, and that agreement was being treated as validation. It may be an echo:
"niche down, sell a diagnostic, lead with the client's pain" is the most
over-represented opinion in business-advice text on the internet.

So a fourth model (`openai/gpt-oss-120b` via Groq — 3.4s, free) was asked to
assume all five were wrong for the same reason. Raw output in
`logs/groq-consensus-attack.md`.

---

## What survived the attack, and is worth acting on

### 1. The niching advice rests on a dead assumption

> "The focus-on-one-vertical rule was born when each new service required a
> full-time specialist."

That is the strongest point made all day. The consensus advice predates
near-zero marginal cost of building. When an agent pipeline can stand up a new
capability in hours, "don't spread thin" is defending against a cost that no
longer exists in the same form.

**But see the counter-bias below before acting on it.**

### 2. Automating the diagnostic destroys what made it valuable

The sharpest and most immediately useful finding, and it directly contradicts
the plan we had:

> "The diagnostic's price justified the *human* judgment that filtered noise…
> If you replace that judgment with a static questionnaire, the perceived
> scarcity evaporates. It becomes a lead-magnet rather than a revenue-generator."

We were about to build an automated intake that produces a diagnostic, and price
that diagnostic at C$1,500. Those two decisions fight each other. If a
competitor gives the same automated diagnostic away — and one will — the paid
version collapses.

**The fix it proposes is right:** move the paid component downstream. Give the
diagnostic away as the lead magnet; charge for interpretation, implementation,
and ongoing monitoring. That is a real change to the funnel design in
`intake/`.

### 3. The genuine blind spot — sell the audit trail, don't just cite it

ChatGPT called the multi-agent audit trail the most commercially interesting
asset but framed it as a *differentiator for consulting*. Groq goes further:
make it **the product**. Compliance-as-a-Service — a recurring API that returns
who did what, when, and why, for buyers who need auditability for governance
reviews.

Nobody else raised it. Revenue independent of consulting hours, built on the one
asset that is genuinely unique here.

---

## Where the fourth model has its own bias — the mirror image

The critique is strong. The prescriptions are not, and they fail in a
consistent, diagnosable way: **it assumes distribution is free because
production is free.** That is the exact mirror of the consensus bias it was
asked to attack.

- **"10 micro-products at $150–300/month"** requires customer acquisition in ten
  separate markets by one person with $2,000 and spare hours. Building is now
  cheap; *selling* is not, and selling is the actual constraint here.
- **"$99 quick-wins sold on autopilot"** presumes an audience that does not
  exist yet. Autopilot sales are the reward for distribution, not a substitute
  for it.

**Two claims that are factually wrong and would cost money:**

- **"cryptographic hashes"** — the audit trail has none. It is a Google Sheet
  with UUIDs and no tamper-evidence. Compliance-as-a-Service on that basis
  cannot survive a SOC-2 conversation. The idea is good; **it needs real
  append-only integrity built first**, and that is a project, not a weekend.
- **"AppExchange allows a $0-upfront listing"** — doubt this and verify before
  acting. It requires a partner agreement and a security review with real cost.
  Worse, an AppExchange listing is precisely the visibility trigger that raises
  the `sfdc24.com` trademark exposure we just decided to accept while small.

---

## What actually changes

1. **Stop planning to sell the diagnostic.** Give it away, charge downstream.
   The intake funnel design needs revising for this.
2. **Investigate Compliance-as-a-Service seriously**, but the honest first step
   is tamper-evident logging, not a landing page.
3. **Hold the niching decision.** The zero-marginal-cost argument is real, but
   the case for breadth ignores distribution, and distribution is the binding
   constraint for one person with spare hours. Revisit if an audience ever
   exists.

**The method worked and should be repeated.** Convergence between models is not
evidence — it is a hypothesis worth attacking. The attack cost nothing and
produced one funnel-breaking insight, one new product direction, and two
fabricated claims that would have been expensive to believe.
