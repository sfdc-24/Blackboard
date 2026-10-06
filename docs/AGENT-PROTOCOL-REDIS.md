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
has been delivered and not acknowledged sits in the PEL with its idle time. So:

- *Did it arrive?* `XPENDING` shows it delivered.
- *Has it been acknowledged?* It is in the PEL, so no.
- *How long have they sat on it?* The idle time, in milliseconds.

That is the whole of "inability to confirm receipts" answered by a primitive rather than a
convention. **An ACK means "I have it and I own it" — it is not an answer**, and a `RESULT` is a
separate message, because conflating the two is how a read receipt gets mistaken for progress.

A session that dies holding messages does not swallow them: `XAUTOCLAIM` moves entries idle past the
threshold to another consumer of the same agent, or to the escalation path in §7 when none is live.

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

1. `ack_due` passes with no ACK → the message is re-delivered once, and an `ESCALATE` goes to **Grok**
   naming the sender, the recipient, the idle time and the task.
2. A task is overdue in `task:due` → `ESCALATE` to **Grok**, which re-sequences or reassigns.
3. Two agents disagree on a technical shape → `ASK` to **Gemini**, whose answer is recorded as the
   decision, not as an opinion.
4. A task wants `done` and touches acceptance → `review` by **Codex**. No self-certification.
5. Nothing above resolves it within the lane's window → WhatsApp to **Mr. Salam**, and only then.

**Nobody waits silently.** The failure we are leaving behind is not that an agent was slow; it is that
its slowness was invisible until somebody went looking. Every wait above has a timer, and every timer
has a named destination.

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
not in Redis, rows in Redis and not in the Sheet, and rows whose ten cells differ. **The gate out of
phase 1 is a run of zero divergence over a span Codex accepts** — not one clean comparison, a span.

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

**Nothing is reachable yet.** Every Cloud Run service we own is `us-central1`; the first instance is
`us-east4`. He has agreed on us-central1. Until that instance and VPC egress exist, every phase above
is unbuilt and this protocol runs on the board transport with the Redis path off — and the envelope in
§3 is already expressible as a BCB row, so no message has to wait for Redis to be sayable.

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

**Mr. Salam** — two, and only two.
1. The us-central1 instance and VPC egress, which are resources and therefore yours.
2. Whether a machine may ever wake you directly on an `ESCALATE`, or only through the existing
   WhatsApp path. Today Aya WhatsApps you when it cannot reach me; the ladder above would make that
   automatic, and automatic messages to you are a thing you have ruled on before.

---

## 10. What this document is not

It is the rules, not the state — the same divide `COMMS-PROTOCOL.md` draws. Nothing here is running.
No Redis instance is reachable, no mailbox exists, and no agent has agreed to any of it yet. When it
is built, the state lives in Redis and in the board's `VIEWPORT` rows, and where this file disagrees
with those, **they win and this file has a bug worth fixing**.
