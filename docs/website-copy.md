# sfdc24.com — page copy, draft 1

claude-code-cli, 2026-09-03.

**The bar this is written against** (Mr. Salam's words): *a visitor should want to
ask a question, pay for it, and reach out.*

**Voice.** The Governor page already has the right one — "Be specific and I'll be
specific back." Everything below extends that. No AI hype, no "transform your
business", no adjectives doing work that facts should do. A Salesforce buyer has
read a hundred consultancy homepages and can smell filler in one line.

**Where the leverage is.** A real visitor typed this into your reception chat on
Sep 2 and got `offline reply · no-key`:

> "Cases get logged 14 different ways and nobody trusts the reporting."

That is a buying signal in the customer's own language. The homepage is built
around problems phrased like that, not around what we do.

**⚠️ THINGS ONLY YOU CAN FILL IN — marked `[[FILL]]` below.** I have not invented
client names, numbers, certifications or results. Publishing a fabricated proof
point is worse than having none, and the one thing that would actually stop
someone paying is catching you in an overstatement.

---

## HOME

### Hero

> # Salesforce work, done by agents. A human signs off.
>
> SFDC24 is a Salesforce consultancy that runs a fleet of AI agents on real
> client work — Apex, Flows, OmniStudio, integrations, data hygiene — with a
> human reviewing every decision that matters.
>
> **You can talk to it right now.** Tell it the problem you're actually having.
> It answers specifically or it says it doesn't know.
>
> `[ Ask a question ]`  ·  `[ Book 20 minutes ]`

*Design note: the chat is the primary call to action, not a footer widget. It is
the entire differentiator — a visitor who asks one question and gets a specific
answer has already experienced the product.*

### The part that gets people to type

> ### Sound familiar?
>
> - "Cases get logged 14 different ways and nobody trusts the reporting."
> - "Our Flows work until someone edits one, and nobody knows which one broke it."
> - "We paid for OmniStudio and use about a fifth of it."
> - "Every release is a weekend."
> - "We have four integrations writing to the same field."
>
> If one of those is close enough, say it in your own words below. You'll get a
> straight answer about what's involved — not a discovery call.

*Why this works: naming the problem in the customer's language proves you've seen
it before. The last line removes the fear of being funnelled into a sales call,
which is the main reason people don't type.*

### How it actually works

> ### Three steps, no mystery
>
> **1. You describe the problem.** In the chat, over WhatsApp, or on a call.
> Rough is fine — "the reporting is a mess" is a real starting point.
>
> **2. Agents do the legwork.** Several models work the problem in parallel —
> reading the org, tracing the data, drafting the fix. Everything they do is
> logged with its source.
>
> **3. A human signs off.** Nothing reaches you unreviewed. If an agent isn't
> sure, you're told it isn't sure.
>
> That third step is the whole point. Anyone can put a chatbot on a website.
> The judgment is what you're paying for.

### Proof

> ### `[[FILL]]`
>
> One or two real engagements: the problem, what changed, and a number if you
> have one. Get client permission before naming anyone.
>
> If you don't have a publishable one yet, delete this section entirely. An
> empty proof section reads as more honest than a vague one.

### Close

> ### Start with a question
>
> The fastest way to find out if we're useful is to ask us something hard.
>
> `[ Ask a question ]` · **abdus@sfdc24.com** · `[[FILL: phone/WhatsApp]]`
>
> Based in Toronto. Working with teams anywhere.

---

## PROJECTS

*Currently an empty shell. It should show the work, not list service categories —
"Integration Services / Data Migration / Custom Development" is what every
Salesforce partner page says and it persuades no one.*

> # What we're working on
>
> A running log of real work. Some for clients, some for ourselves.
>
> ### `[[FILL]]` — 3 to 5 entries, each:
> - **What the problem was** (one sentence, in the client's language)
> - **What we did** (two sentences, specific enough to be checkable)
> - **What changed** (a number if you have one, plain language if not)
>
> Candidates from your own work — only publish with permission:
> - the WhatsApp multi-agent gateway (yours, publishable, and genuinely unusual)
> - the Blackboard coordination layer
> - `[[FILL: client engagements]]`

*Note: the Governor board embed already lives on this page. Consider whether a
visitor should see it. It's impressive as evidence that real work is happening,
but it's raw internal state and reads as noise to someone who doesn't know the
system. A curated "recently shipped" view would convert better than the live feed.*

---

## TEAM

> # Who you'd actually be working with
>
> ### Abdus Salam
> `[[FILL: one paragraph — years in the Salesforce ecosystem, the kinds of orgs
> you've worked in, certifications if you hold them, and the one thing you're
> unusually good at. Write it plainly. "15 years untangling orgs other people
> built" beats any list of certifications.]]`
>
> ### The agents
> Several models work alongside — currently Claude, Gemini and GPT-family models,
> each on the kind of work it's best at. They draft, research and cross-check.
> They don't decide.
>
> **Why say this out loud:** you'd find out anyway, and a consultancy hiding its
> use of AI in 2026 is a consultancy with something to hide. Saying it plainly is
> the differentiator — the agents are the reason the work is fast, and the human
> sign-off is the reason it's safe.

---

## FAQ

> ### Is this just a chatbot?
> No. The chat is the front door. Behind it, several models work the problem in
> parallel and a human reviews the output before it reaches you.
>
> ### Do the AI agents see my Salesforce data?
> `[[FILL — answer precisely, and only what is true today. This is the question
> that decides whether a regulated buyer continues. If agents don't touch the org
> without explicit access, say exactly that.]]`
>
> ### What does it cost?
> `[[FILL. Even a range converts better than "contact us" — the visitors who
> leave over a number were never going to buy, and everyone else stops
> wondering.]]`
>
> ### How fast?
> `[[FILL — a real typical turnaround, not a best case. Promising fast and
> delivering fast is the whole brand; promising fast and missing kills it.]]`
>
> ### What if the AI gets it wrong?
> A human reviews everything before it reaches you, and every answer carries its
> source so you can check it. When an agent isn't confident, you're told so
> rather than given a confident guess.
>
> ### Can I just email a human?
> Yes. **abdus@sfdc24.com**. You'll get a person.

---

## IMPLEMENTATION NOTES

**Before publishing:**

1. **Fix the apex domain.** `sfdc24.com` refuses connections on 443 and 80 —
   only `www` resolves. Copy doesn't matter if the door doesn't open. Registrar
   forward: `sfdc24.com → https://www.sfdc24.com`.
2. **Turn the chat on.** `ANTHROPIC_KEY` and `GOVERNOR_PASS` are unset on the
   Governor Page API project — its Script Properties table is empty. Every CTA
   above points at a chat that currently answers `offline reply · no-key`.
   **Shipping this copy with a dead chat is worse than shipping nothing.**
3. **Fill every `[[FILL]]`.** Delete any section you can't fill honestly.
4. **Reconsider the Google Sites chrome.** "Report abuse" appears twice per page
   alongside "Search this site" and "Page updated". For a service people pay for,
   that reads as a hobby site.

**The one metric worth watching:** how many visitors type something into the
chat. Everything above is engineered toward that single action, because a
visitor who asks a question has already tried the product. The board logs every
reception message, so this is measurable from day one — you already have two
data points, and both went unanswered.
