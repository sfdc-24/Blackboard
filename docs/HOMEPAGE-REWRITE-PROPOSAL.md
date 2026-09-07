# Homepage rewrite — proposal, not a change

Written 2026-09-07 by claude-code-cli. Mr. Salam authorised it: *"the buttons,
footer and all written content on sfdc24.com can be rewritten with this in
mind."* Nothing here is live. It needs his eye first, because it is his voice
and his business, and because one part of it must not ship yet.

---

## The constraint that decides the whole thing

**Copy can only change when the capability ships.**

The obvious rewrite is to describe the four beats — *describe it, watch it take
shape, tell me what is wrong, keep it.* That would be a lie today. Beats 1 to 3
run on `localhost:8732` and nowhere else. Putting them on the live homepage would
be exactly the failure this repo spent a day learning to catch: claiming work
that has not happened.

So the rewrite splits into two parts with different timing:

| | change | when |
|---|---|---|
| **A** | the hero, which currently excludes two of his three visitor types | **now** — it is a narrowing bug, and fixing it claims nothing new |
| **B** | the offer section, which sells a week-or-two diagnostic | **only when beat 1 is live** — until then, that section is accurate |

Part B is the thesis conflict from `SITE-THESIS-CONFLICT-001`. It stays wrong on
purpose until the thing that makes it right is actually serving. **Do not ship B
early.**

---

## A · the hero, and why it is a bug rather than a preference

Live today:

> **Salesforce operations for orgs nobody wants to touch**
> Tell it what's broken. You'll get a straight answer, or an honest "that's not us".

Mr. Salam's three visitor types are: someone who wants an **app or website
built**, someone who wants a **Salesforce environment stood up**, and someone
with a **specific technical problem**. That hero speaks to the third only. The
first two read "tell it what's broken", conclude they are in the wrong place, and
leave — and we never learn they came.

### The tension I am not going to resolve on my own

Widening "Salesforce operations" to "apps, websites, environments and fixes"
costs something real. The specialism is the credible claim: one consultant,
Salesforce operations, Lean Six Sigma, Toronto. A page that offers everything
reads like a page that has done nothing, and that is a worse failure than being
too narrow.

**So the proposal does not widen the claim. It widens the invitation.** What
the page promises to be good at stays exactly where it is; what it invites you to
talk about opens up. Those are different sentences and only one of them needs to
change.

### Proposed

> **Salesforce operations, and the things people ask for next**
> Tell it what you need — something fixed, something built, or an environment to
> work in. You'll get a straight answer, or an honest "that's not us".
> Conversations are saved. A human reads them.

Why this shape:

- **The specialism leads.** The credible claim is still the first thing read.
- *"the things people ask for next"* is true. Someone whose org you fixed asks
  for the tool next; that is the actual path, not a widening of scope.
- The three intents appear as **examples in one sentence**, not as a menu. He was
  explicit: adapt to the customer rather than making them select a template.
- *"or an honest that's-not-us"* is kept verbatim. It is the most trust-carrying
  line on the page and it is doing more work than anything around it.

### Also worth changing now, same reason

`/intake/` went live tonight and the nav says **"Start a request"**. That is
neutral to the point of saying nothing. Better: **"Tell us what you need"** —
same words as the hero, so the link and the destination agree.

---

## B · the offer section — drafted, deliberately not shipped

For when beat 1 is live, replacing *"a diagnostic … usually a week or two …
ends in a written recommendation"*:

> **What actually happens**
>
> You describe the thing — badly is fine, out loud if you prefer. It gets
> sketched back to you while you talk, in the simplest possible form, so you can
> point at the part that is wrong. You correct it. Then it gets built.
>
> You do not pay to find out whether this works. You pay when you already have
> the thing and want to keep it.

Why it is written this way:

- **No timeline.** "A week or two" invites comparison with other quotes, which is
  the mode `PRODUCT.md` exists to avoid.
- **"badly is fine"** removes the work of being understood — his rule.
- **The last two lines are the thesis in plain English**, and they are the whole
  differentiator: nobody else can say them, because nobody else hands over the
  thing first.

**The diagnostic does not disappear.** It stops being the pitch and becomes what
happens for people who want a document — which is a real thing some buyers want.
Smaller, further down, not the opening move.

---

## What I need from him

1. Ship **A** now, or change the words first?
2. Hold **B** until beat 1 is live — agreed?
3. Is *"and the things people ask for next"* honest? He would know better than I
   do whether that is how the work actually arrives.
