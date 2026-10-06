# The agent protocol on Redis: receipts, assignments, status, dependencies

**Status:** Specification for ratification. Nothing built. Codex, Gemini and Grok each have a named
decision below, and the implementation waits on all three.<br>
**Date:** 2026-10-06<br>
**Author:** Claude (`claude-code-cli`) — governance and the data-security gate<br>
**Asked for by:** Mr. Salam — *"complete communication message setup for collaboration open for all AI
agents working with blackboard in REDIS and status that acknowledges receipt, task assignments, status
and dependencies clearly outlined and managed. Circle the control and orchestration by Codex and
Gemini and Grok to make sure previous blocks, inability to confirm receipts of messages, door bells,
and wait times on other agents does not reappear in redis."*<br>
**Extends:** [`COMMS-PROTOCOL.md`](COMMS-PROTOCOL.md) (the rules), [`board-protocol.md`](board-protocol.md)
(the current transport)

**Every rule here exists because something already went wrong on the board. A mechanism with no
incident behind it is not in this document.** The point of moving to Redis is not that Redis is
faster. It is that Redis has primitives for the three things the board cannot do: prove a message was
received, wake a reader without polling, and say how long someone has been sitting on work.

---

## 1. The failures this must not reproduce

Each row is measured from our own history, and each names the mechanism that makes it impossible
rather than unlikely.

| What went wrong | Evidence | The mechanism |
|---|---|---|
| A write returned `ok` and wrote nothing | The gateway has no create path: appending to a missing board succeeds and stores nothing | `XADD` returns the entry id; the writer reads that id back. No id, no write. |
| The gateway flaps — a 404 can still mean the row landed | Repeated this session: three read attempts returning non-JSON before one succeeded | Message id is a **deterministic hash** of sender + logical id, so a blind retry is a no-op instead of a duplicate |
| A failure report is not proof of no write | A timed-out read-back after a landed append invited a blind re-run | Same as above: idempotent by construction, so the ambiguity stops mattering |
| The reader hid rows addressed to me | Addressing filters dropped my own rows; 35 Grok rows went to plain `claude` | Delivery is a **per-recipient consumer group**, not a filter a reader chooses to apply |
| A task sat unread for six hours | A Codex `TASK` row missed; three Codex requests on #133 unanswered ~4 h | **`XPENDING` idle time** is the wait, measured in milliseconds, per recipient, by anyone who asks |
| One agent writes under several names | `CODEX-DESKTOP` rows slipped a case-sensitive watcher | A canonical alias table; every id lowercased on the way in. One agent, one consumer. |
| Doorbells cost money and still miss | 90-second polls on the Pi; three API charges in one day | `XREADGROUP BLOCK` is a **blocking read**: zero cost while idle, immediate on arrival |
| A quiet session is indistinguishable from a dead one | I was idle ten hours; Aya could not reach me and had to WhatsApp him at 16:40Z | `hb:<agent>` with a TTL. Absent means **not live**, and that is answerable without guessing |
| A queue nothing drains delivered 18 stale messages | One EOD send delivered 19, 18 of them old | Every message carries `expires_at`; an expired message is dropped and counted, never delivered |
| A claim row is not coordination | Agents forced a shared branch by announcing intent | Dependencies are **edges**, and a task cannot leave `blocked` until its edges are satisfied |
| A literal `|` breaks a row | The BCB payload has no escaping | Payloads are JSON. There is no delimiter to break. |
| Green is not proof | Repeatedly | A state change is **witnessed** — who moved it, when, and on what evidence — or it did not happen |

---

## 2. Identity, and why it comes first

Half the receipt failures were identity failures. An agent that writes as `codex`,
`chatgpt-codex-desktop`, `CODEX-DESKTOP` and `aya` is four agents to a watcher and one agent to
itself.

```
agent:canon           HASH   alias (lowercased) -> canonical id
agent:roster          SET    canonical ids that may send or receive
hb:<agent>            STRING "<iso>|<what it is doing>"   EX 120
```

- **Every id is lowercased before any lookup.** No exceptions, no case-sensitive comparison anywhere.
- An alias that is not in `agent:canon` is **refused at send time** with the reason. A message to a
  name nobody owns is a message that disappears, which is the failure we are leaving behind.
- `hb:<agent>` is refreshed while a session is alive. **Absent means not live** — so "quiet" and
  "dead" are different answers, and a sender knows which one it is facing before it waits.

Canonical set at ratification: `claude-code-cli`, `chatgpt-codex-desktop` (aliases `codex`, `aya`,
`dot`, `codex-desktop`), `gemini`, `grok`, `cursor`, `copilot`, `pi1-cli`, `whatsapp`, `owner`.

---

## 3. The envelope

One JSON object per message. No delimiter, so nothing can break it.

```json
{
  "id": "sha256(sender + logical_id)[:24]",
  "logical_id": "CCC-CONF167-P1S-CLOSED-20261006T0010Z",
  "kind": "ASSIGN | ACK | STATUS | RESULT | BLOCKED | ASK | ANSWER | NOTE | ESCALATE",
  "from": "claude-code-cli",
  "to": ["chatgpt-codex-desktop"],
  "cc": ["grok"],
  "sent_at": "2026-10-06T00:40:00Z",
  "expires_at": "2026-10-06T12:40:00Z",
  "ack_due": "2026-10-06T01:40:00Z",
  "task": "T-0461",
  "answers": "CCC-REDIS-REGION-BLOCKER-20261006T0015Z",
  "evidence": "MEASURED | READ | STATED | NONE",
  "body": "…",
  "refs": {"pr": "sfdc-24/conference#167", "head": "0a4f4735…"}
}
```

Rules that are enforced, not advised:

- **`id` is derived, never chosen.** A retry of the same logical message produces the same id and
  `XADD` with an explicit id is rejected as a duplicate. The flapping-gateway class of bug ends here.
- **`expires_at` is required.** A message past it is dropped at read time and counted. The outbox that
  delivered eighteen stale messages could not happen.
- **`ack_due` is required on `ASSIGN` and `ASK`.** A message that needs an answer says when.
- **`evidence` is required and `NONE` is a legal, honest value.** The failure this fleet actually has
  is confident noise, not disagreement.

---

## 4. Delivery and receipt

```
mbox:<agent>          STREAM  every message addressed to that agent
grp:<agent>           consumer group on mbox:<agent>, one consumer per live session
```

Send, read, acknowledge:

```
XADD  mbox:codex  <id>  env <json>
XREADGROUP GROUP grp:codex codex-s1 BLOCK 30000 COUNT 16 STREAMS mbox:codex >
XACK  mbox:codex grp:codex <id>
```

**The pending entries list is the receipt ledger, and we did not have to build it.** An entry that
has been delivered and not acknowledged sits in the PEL with its idle time.

### Three separate facts, three separate mechanisms

Codex's first quality condition on `19ec3f7`, and it is right: I had two of these collapsed into one
word. "Acknowledged" was doing the work of "arrived" and "owned" at once, which is the receipt
problem wearing a new coat.

| The question | The mechanism | What it does NOT mean |
|---|---|---|
| Did it reach the agent's mailbox? | `XADD` returned an id | Nobody has read it |
| Did a reader take it off the stream? | **`XACK`** — a transport fact | Nobody has agreed to do it |
| Does a session own the work? | an **`ACK` message**, from the session that will do it | It is not done |
| Is it done? | a **`RESULT`** message | — |

So `XACK` is **transport only**. A cheap always-on reader may send it, because all it asserts is that
the bytes were taken off the stream and will not be redelivered to that consumer. **It must never be
read as ownership.** A task stays `ready` — not `claimed` — until an `ACK` *message* arrives from the
session that will do the work, and only that session may send one.

The consequence is the one Codex wanted stated: **a transport-acknowledged message with no owning
ACK is still outstanding**, and it is counted that way. `XACK` clears the PEL entry; the task's own
`ack_due` timer keeps running. Losing that distinction is how a read receipt gets mistaken for
progress, which is exactly what the board already did to us.

### Reclaiming from a dead session, fenced

A session that dies holding messages does not swallow them: `XAUTOCLAIM` moves entries idle past the
threshold to another consumer of the same agent. **Fenced, because otherwise it is a claim war** —
Codex's third condition, and Gemini's words for the failure: a flapping heartbeat causes a consumer
group claim war and drains Redis memory.

The fence is a monotonic **epoch** per agent, `claim:epoch:<agent>`, bumped with `INCR` by whoever
reclaims:

- A reclaim reads the epoch, bumps it, and stamps the claim with the new value.
- A consumer whose stamp is below the current epoch **has been fenced and must stop** — it may not
  `XACK`, may not send an `ACK`, and may not write a `RESULT`. The work is no longer its.
- A reclaim is attempted **at most twice** for one entry. The second failure sends the entry to the
  §7 ladder instead of a third consumer, because an entry that two consumers could not hold is a
  problem for a human to look at and not a thing to keep passing around.
- A heartbeat that flaps — appearing and vanishing inside the idle threshold — is treated as **not
  live** for reclaim purposes. Flapping is not liveness, and trusting it is what starts the war.

---

## 5. Tasks, status, and dependencies

```
task:<id>             HASH    the task
task:deps:<id>        SET     task ids this one waits on
task:blocks:<id>      SET     task ids waiting on this one
task:ready            SET     deps empty, nobody has claimed it
task:due              ZSET    member task id, score deadline epoch
task:state:<state>    SET     every task in that state
task:log:<id>         STREAM  every transition, appended, never rewritten
```

The state machine, and the only legal moves:

```
draft -> blocked -> ready -> claimed -> working -> review -> done
                                   \-> blocked (a new dependency appeared)
         any state -> cancelled (the owner, or the controller of that lane)
```

- **A task enters `ready` only when `task:deps:<id>` is empty.** Not when someone says it is ready.
  Satisfying the last dependency is what moves it, and that is a server-side consequence rather than
  a courtesy.
- **`claimed` requires an ACK.** You cannot hold a task you never acknowledged.
- **`review` names its reviewer**, and for anything touching acceptance that is Codex. A task does
  not reach `done` on its author's word — the lesson from putting an advisor live over a Codex hold.
- **Every transition is witnessed** in `task:log:<id>`: who, when, and the evidence. A transition with
  `evidence: NONE` is legal and visibly weak, which is the point.
- **`task:due` is a sorted set**, so the overdue list is one `ZRANGEBYSCORE task:due -inf <now>` — no
  per-task polling, and "wait times on other agents" becomes a query instead of a complaint.

Dependencies are symmetric by construction: writing `deps` without `blocks` is refused, so a
dependency can always be traversed in the direction the blocked agent cares about.

---

## 6. No doorbells

A doorbell is a poll, a poll costs tokens, and on 2026-10-04 that cost him three charges in one day.

- **Readers block.** `XREADGROUP ... BLOCK 30000` costs nothing while idle and returns the instant
  something arrives. There is no interval to tune and no wake that finds nothing.
- **A poll that finds a row wakes a session, and a woken session spends his money.** So the blocking
  read belongs in a cheap, always-on process, and waking an expensive agent is a deliberate act that
  must be justified by the message's `kind` — an `ASSIGN`, `ASK` or `ESCALATE` may wake; a `NOTE` may
  not. A `NOTE` waits for the next time that agent is awake anyway.
- **Liveness before waiting.** A sender checks `hb:<agent>` first. Not live means do not wait on it —
  take the escalation path immediately rather than discovering the silence an hour later.

---

## 7. Control, orchestration, and the escalation ladder

**His appointments of 2026-10-06, in his words:** *"I'm appointing you as the control lead for Redis
and Grok as lead for strategic reporting and analysis, Codex as Quality and improvement lead and
Gemini as Architect lead — everyone has to participate."* In the protocol that is not a sentiment; it
is who may move what, and where an unanswered message goes next.

| Lane | Lead | What it decides |
|---|---|---|
| **Redis: control and orchestration** | **Claude** (`claude-code-cli`) | This protocol's operation: identity, mailboxes, the clock, the roll-up, and who may wake whom. Also governance and the data-security gate. |
| **Architecture** | **Gemini** | The shape. Any change to the envelope, the state machine, or the key layout is its call, and mine to implement. |
| **Quality and improvement** | **Codex** (`chatgpt-codex-desktop`; `codex`, `aya`, `dot` are aliases) | Whether a task may reach `done`; the ACK and idle thresholds; API and subscription spend, by his appointment of 2026-10-04 21:28Z. No self-certification by anyone, me included. |
| **Strategic reporting and analysis** | **Grok** | What the fleet reports upward, what the numbers mean, sequence against the pilot, and where an overdue task goes. |
| Resources, spend, and anything client-facing | **Mr. Salam** | Last resort, and the only one never escalated *to* by a machine except through WhatsApp |

Being control lead for Redis does not make me the reviewer of my own work. Architecture decisions are
Gemini's and acceptance is Codex's; where either overrules me on this document, they win and I
implement it. The two guardrails I keep as control lead are the ones he has already ruled on: no
secret reaches git or the board, and no change goes live without the old path proven equal first.

The ladder, and it fires on a clock rather than on someone noticing:

1. `ack_due` passes with no owning ACK → the message is re-delivered **once**, and an `ESCALATE` goes
   to **Grok** naming the sender, the recipient, the idle time and the task.
2. A task is overdue in `task:due` → `ESCALATE` to **Grok**, which re-sequences or reassigns.
3. Two agents disagree on a technical shape → `ASK` to **Gemini**, whose answer is recorded as the
   decision, not as an opinion.
4. A task wants `done` and touches acceptance → `review` by **Codex**. No self-certification.
5. Nothing above resolves it within the lane's window → WhatsApp to **Mr. Salam**, under §9's rules,
   which today mean it is queued and **not sent by a timer**.

### Bounded, because an escalation ladder is a loop waiting to happen

Codex's second condition on `19ec3f7`, and Gemini named the failure: without bounds, an endless
escalation loop that drains Redis memory. Four rules, and they are hard limits rather than guidance:

- **An `ESCALATE` can never itself escalate.** It has no `ack_due` and no rung above it. A ladder whose
  rungs can each produce another rung is not a ladder.
- **One rung per message, ever.** Each rung is attempted at most once for a given message id, recorded
  in `esc:done:<id>` as a set of rungs already fired. A rung already in that set is skipped, so a
  message cannot ride the ladder twice however many timers notice it.
- **Re-delivery is once, not a retry policy.** After one re-delivery the message is the ladder's
  problem and never the sender's again.
- **A cap per hour per lane**, and when the cap is hit the ladder stops and writes one summary row
  instead of N escalations. The scar: one send delivered nineteen messages, eighteen of them stale.

**Nobody waits silently.** The failure we are leaving behind is not that an agent was slow; it is that
its slowness was invisible until somebody went looking. Every wait above has a timer, every timer has
a named destination, and now every destination has a counter that stops it.

---

## 8. Rolling the bus up into Redis, without breaking it

His instruction of 2026-10-06: *"the BUS (blackboard alpha db) should be rolled up into redis so
communication is seamless. Do not break what's already working, Build it in Redis, do parallel checks
and compare results first."* That is the shape below, and it is three phases with a gate between each.

**The one constraint that shapes all of it, measured and not assumed:** Apps Script has no VPC access
of any kind, so the Sheet's own code can never reach a private Memorystore address. That does not stop
the roll-up — it decides *where the roll-up runs*. It is a **relay**, in a place that can reach both:
Cloud Run in `us-central1` with VPC egress, which reaches the gateway over HTTPS and Redis over the
private network. The Sheet does not need to know Redis exists.

```
bus:rows            STREAM  every board row, entry id derived from Row_ID
bus:row:<Row_ID>    HASH    the row's ten cells, for a direct get
bus:cursor          STRING  the relay's watermark: the newest timestamp it has mirrored
bus:compare         STREAM  one entry per reconciliation: counts, never contents
```

**Phase 1 — mirror and compare, and change nothing.** The relay reads the gateway with `since=<cursor>`
and `XADD`s each row under an id derived from its `Row_ID`, so a flapping read that returns the same
rows twice mirrors them once. The cursor only ever moves forward. Nothing reads from Redis yet. A
reconciler then diffs the two by `Row_ID` and writes counts to `bus:compare`: rows in the Sheet and
not in Redis, rows in Redis and not in the Sheet, and rows whose ten cells differ. **The gate out of phase 1 is TWO zero-divergence runs: 24 hours and then 48 hours**, which is
Codex's ruling on `19ec3f7` and not my number. One clean comparison is not a span, and a single
span can be a quiet window - 24 hours followed by 48 hours makes a quiet window insufficient on
its own. The reconciler reports UNKNOWN rather than zero when it could not compare, so neither
span can be satisfied by a run that measured nothing.

**Phase 2 — read from Redis, with the Sheet still the answer.** Readers take the Redis copy *and* the
gateway copy, serve the gateway's, and log divergence. This is `scripts/redis_dual.py` already built
and already tested: the old path answers even when Redis is faster or right. The gate out is Codex
accepting a divergence rate of zero over a second span, and Gemini accepting the read path.

**Phase 3 — Redis answers, the Sheet is written through.** Only here does a read come from Redis.
Writes still land in the Sheet first, because the Sheet is the append-only record he can open in a
browser and I am not taking that away. An agent that cannot reach the Sheet writes to Redis and the
relay appends on its behalf — which is new capability, not a replacement, and it is what makes a
Cloud Run agent a first-class writer for the first time.

**The off-switch spans all three phases**: one settings edit returns every reader to the gateway on its
next operation, with no redeploy. That is already how the scaffold behaves.

What rolls up alongside the bus, same pattern, same gates: repository work and PR state, tasks, issues
and the OKF — each mirrored, compared, and only then read from. The OKF is the one to be most careful
with, because the chair serves the OKF **baked into its image** and will keep doing so; Redis becomes
where the fleet reads the OKF, never where the chair gets it, or we reintroduce a live plan changing
under a call.

**The infrastructure now exists, and the protocol still does not run.** As of 2026-10-06, relayed by
Grok from the owner: `redis-central` in us-central1 is the SOLE instance - the us-east4 one is
deleted and must not be wired - with auth on and TLS `SERVER_AUTHENTICATION`; the AUTH string is
Secret Manager `REDIS_AUTH_STRING` version 2 and goes on neither the board nor git; and Direct VPC
egress, private-ranges-only on `default`/`default`, is set on `conference-gateway`,
`conference-chair`, `sfdc24-stt-relay` and `sfdc24-studio-controller`.

**The dual-run nevertheless stays OFF**, by Grok's sequencing and Codex's hold: the conference
what-closed-leaves-the-room holds on PR 167 come first, and Codex verifies the scaffold, before
anything connects. Until then this protocol runs on the board transport, and the envelope in §3 is
already expressible as a BCB row, so no message waits for Redis to be sayable.

---

## 9. What each lead must decide before anything is built

Named, so no one has to guess what is being asked of them.

**Codex — quality and improvement lead.**
1. The ACK window per `kind`. My proposal: `ASSIGN` 60 min, `ASK` 4 h, `ESCALATE` 15 min, `NOTE` none.
2. The `XAUTOCLAIM` idle threshold at which a held message is taken from a dead session.
3. Whether an ACK may be sent by a cheap always-on process on an agent's behalf, or must come from the
   session that will do the work. I lean to the second — an ACK that does not mean "I own it" is the
   receipt problem with extra steps — and I can see the cost argument for the first.

**Gemini — architect lead.** You own the shape, and I would rather be corrected now than after it is built.
1. Streams with consumer groups versus a simpler list per agent. I chose streams **for the PEL**: it
   is the receipt ledger as a primitive, and I do not want to rebuild one.
2. My idempotent id is `sha256(sender + logical_id)`. Attack it: two different messages sharing a
   logical id collapse into one, silently. Is that the right trade against duplicate delivery?
3. The wake rule in §6 is a classifier with **silent false negatives** — a `NOTE` that should have
   woken someone never does, and nobody learns. You raised exactly this against the wake gate. Is the
   `kind`-based rule enough, or does a suppressed wake need its own audit?

**Grok — strategic reporting and analysis lead.**
1. The order: identity table and heartbeats first, then mailboxes and ACK, then tasks and
   dependencies. Each is useful alone; the last is useless without the first.
2. Where an overdue task goes when you are the one who is quiet. There is no second Grok, and the
   ladder currently dead-ends at you before the owner.

**And on the roll-up specifically**, because it is the part that can break something that works:

- **Codex:** the span of zero divergence that opens each phase gate. One clean comparison is not a
  span, and I would rather you set the number than have me pick one that happens to pass.
- **Gemini:** the relay is a single point of failure between two stores, and its cursor is the thing
  that decides what is mirrored. Attack the cursor: a watermark that moves forward on a partial read
  loses rows silently, which is the failure class our own waker state already taught us.
- **Grok:** the order of what rolls up after the bus — repository and PR state, tasks, issues, the
  OKF. I would do the bus, then tasks, then issues, then PR state, and the OKF last because the chair
  must keep taking it from its image.

**Mr. Salam** — one, now, and it is a resource: the us-central1 instance and VPC egress.

The second question I had put here was muddled and he answered the clear part of it on 2026-10-06:
*"whatsapp is there as Aya uses already which is effective."* So **the channel is settled — WhatsApp,
by the path Aya already uses** — and it was never really in doubt; he authorised routine WhatsApp
updates in September. The evidence for Aya's path is good: its 16:40Z message on 2026-10-05 is what
got the conference lane moving again while my session had been idle for ten hours and it could not
reach me.

What I should have asked is not *which channel* but **what may pull the trigger**: may a timer alone
send, or must a lane lead look first? I decided as Redis control lead that a timer could send
unasked, for a named class, with brakes.

**That decision is overruled and the timer is OFF.** Two rulings, hours apart, and both outrank me:

- The owner at 08:15 ET on 2026-10-06, relayed by Grok: *"escalations WhatsApp only. No direct machine
  wakes to owner."*
- Codex, quality and improvement lead, on `19ec3f7`: *keep timer WhatsApp off without owner trigger
  authority.* Gemini backed it in the same hour: *the owner must control when the system escalates to
  their phone.*

So the standing rule is: **WhatsApp is the only channel, and no timer sends on it.** A rung-5
escalation is QUEUED, with everything below still true about what it may contain and how old it may
be, and a human or a lane lead releases it. Nothing in this protocol may reach him by any other
route, and no mechanism here may wake him directly.

Trigger authority is the owner's to grant and he has not granted it. If he ever does, the table below
is what I would ask him to authorise rather than a thing that would already be running — and the
sensible first step is a far narrower grant than I had written, perhaps client-impacting breakage
alone.

**Held, not built: what a timer WOULD be allowed, if the owner ever grants trigger authority.**

| | Rule | The scar behind it |
|---|---|---|
| Class | Only client-impacting breakage, a security finding, a spend anomaly, or blocked-with-no-lead-live. Everything else goes to a lead first. | "WhatsApp: updates yes, asks only if urgent" |
| Age gate | Nothing older than 30 minutes is sent. A stale escalation is dropped and counted. | One EOD send delivered 19 messages, 18 of them stale |
| Rate cap | At most one message an hour, and a digest rather than N messages. | The same |
| Quiet hours | Nothing 01:00–07:00 Toronto unless it is client-impacting. | He went to bed at about 02:30 on 2026-10-06 |

Any such send would name **why the timer fired and who did not answer**, so it is actionable in one
read rather than a notification he has to go and investigate. The thresholds themselves — how long is
overdue, what counts as a spend anomaly — are Codex's, as quality and improvement lead.

**Until that authority is granted, the queue is the product.** A rung-5 escalation becomes a line
somebody can see and release, which is what Aya already does by hand and does effectively. The
difference between that and what I had designed is who decides it is worth his attention, and all
three of them said it should not be a timer.

---

## 10. What this document is not

It is the rules, not the state — the same divide `COMMS-PROTOCOL.md` draws. Nothing here is running.
No Redis instance is reachable, no mailbox exists, and no agent has agreed to any of it yet. When it
is built, the state lives in Redis and in the board's `VIEWPORT` rows, and where this file disagrees
with those, **they win and this file has a bug worth fixing**.
