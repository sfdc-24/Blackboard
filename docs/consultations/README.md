# Who asked whom, and what they said back

Every cross-agent consultation on this fleet, written by the call that
made it. Asked for on 2026-09-19: full traceability, in git, auditable,
and not required to be public.

The board carries the RESULT of a decision. This carries the argument -
which is the part that explains why the result looks the way it does.

Written by `scripts/consult.py`, which asks and records in one call, so
there is no step anyone can forget. The index is regenerated from the
files and cannot drift from them.

## Scoreboard

A verdict requires a RUN. Nothing here is scored by argument, and an
approval from Mr Salam never scores a record - if he says "go with your
recommendation", the same bias comes out the other side wearing an
approval. Only what happened when it ran counts.

`dissent right` is the column that matters: a peer agreeing with me and
being right teaches nothing, because that was already going to happen.
A peer that DISAGREED and turned out right is the entire return on asking.

| answered by | asked | correct | partly | wrong | open | dissented | dissent right |
|---|---|---|---|---|---|---|---|
| gemini | 2 | 1 | 0 | 0 | 1 | 1 | **1** |
| grok | 5 | 2 | 1 | 0 | 2 | 1 | **1** |

## Lessons, which is what the wrong ones are for

- **Editing the live WhatsApp Pipedream step to add a prefix reply** (gemini, refused) — gemini's fourth refusal was the right call and I took it. A prefix gate on inbound TEXT alone would have replied to anyone who messages the business number; the gate needs sender, message type and prefix together. And moving the model calls off the default path - my own idea, to stop paying for discarded answers - would have made the board write depend on model latency, so a slow or failed model would mean the message never reaches the board at all. Both faults were in my plan and neither was in my head before asking.
- **Site doctrine and execution split with grok (1 of 4)** (grok, correct) — It overruled me and it was right. I was going to link assets/site.css into all six pages, which carries a :root, a body background and a type scale - a silent redesign of six pages no suite renders. Ask before a change whose blast radius is pages nothing tests.
- **Site doctrine and execution split with grok (2 of 4)** (grok, correct) — Agreed with my own position, so this consultation returned nothing I did not already have. Worth recording as such: a peer that agrees is not evidence, and the scoreboard should show how often that happens.
- **Site doctrine and execution split with grok (4 of 4)** (grok, partly) — A peer answers from the context it has, not from the repository. Brief it on what has LANDED before asking what to do next, or two thirds of the answer is work that already exists.
  - _on the asking:_ My question described the dispatches and my limits but never said what had already merged. The stale answer was the predictable result of a question that left out the state of the world.

## Every consultation

| when (UTC) | answered by | outcome | dissent | subject |
|---|---|---|---|---|
| 2026-09-20T19:06:38Z | gemini | unverified | unscored | [Choice-design architecture for a static low-traffic site](2026-09-20-choice-design-architecture-for-a-static-low-traffic-site.md) |
| 2026-09-19T05:12:47Z | grok | unverified | unscored | [Operating doctrine for the agency (1 of 1)](2026-09-19-operating-doctrine-for-the-agency-1-of-1.md) |
| 2026-09-19T05:03:42Z | gemini | refused | yes | [Editing the live WhatsApp Pipedream step to add a prefix reply](2026-09-19-editing-the-live-whatsapp-pipedream-step-to-add-a-prefix-rep.md) |
| 2026-09-19T05:03:11Z | grok | partly | no | [Site doctrine and execution split with grok (4 of 4)](2026-09-19-site-doctrine-and-execution-split-with-grok-4-of-4.md) |
| 2026-09-19T05:03:11Z | grok | unverified | unscored | [Site doctrine and execution split with grok (3 of 4)](2026-09-19-site-doctrine-and-execution-split-with-grok-3-of-4.md) |
| 2026-09-19T05:03:11Z | grok | correct | no | [Site doctrine and execution split with grok (2 of 4)](2026-09-19-site-doctrine-and-execution-split-with-grok-2-of-4.md) |
| 2026-09-19T05:03:11Z | grok | correct | yes | [Site doctrine and execution split with grok (1 of 4)](2026-09-19-site-doctrine-and-execution-split-with-grok-1-of-4.md) |

7 consultations recorded.
