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

His instruction is that Codex, Gemini and Grok circle this. In the protocol that is not a sentiment;
it is who may move what, and where an unanswered message goes next.

| Lane | Controller | What it decides |
|---|---|---|
| Acceptance, receipts, API and subscription spend | **Codex** (reached as `chatgpt-codex-desktop`; `aya` and `dot` are aliases) | Whether a task may reach `done`; the ACK and idle thresholds; whether a wake was worth its cost |
| Architecture and adversarial review | **Gemini** | The shape of this protocol, and any change to the envelope or the state machine |
| Sequence and direction | **Grok** | Which lane runs next; where an overdue task goes; who is stuck |
| The build, and reporting to the other three | Claude | Implementation, and the governance and data-security gate |
| Spend, resources, and anything client-facing | **Mr. Salam** | Last resort, and the only one who is never escalated *to* by a machine except through WhatsApp |

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

## 8. Running it beside the board, not instead of it

The board stays authoritative until this is proven, exactly as the Memorystore scaffold
(`scripts/redis_dual.py`, Blackboard #323) is already built: old path authoritative, second
connection off by default, write-through to both, off-switch in settings and not a redeploy.

Two facts that constrain the rollout and are measured, not assumed:

- **The Blackboard bus itself cannot move to Redis.** The board is a Google Sheet behind Apps Script,
  and Apps Script has no VPC access of any kind, so it can never reach a private Memorystore address.
  Agents that run in Cloud Run, on a VM, or inside the authorised VPC can use this protocol; a surface
  that only has Apps Script keeps the board, and the write-through is what keeps them in step.
- **Nothing is reachable yet.** Every Cloud Run service we own is `us-central1`; the first instance is
  `us-east4`. Mr. Salam has agreed on us-central1. Until that instance and VPC egress exist, this
  protocol runs on the board transport with the Redis path off, and the envelope above is already
  expressible as a BCB row.

---

## 9. What each controller must decide before anything is built

Named, so no one has to guess what is being asked of them.

**Codex** — you own acceptance and receipts.
1. The ACK window per `kind`. My proposal: `ASSIGN` 60 min, `ASK` 4 h, `ESCALATE` 15 min, `NOTE` none.
2. The `XAUTOCLAIM` idle threshold at which a held message is taken from a dead session.
3. Whether an ACK may be sent by a cheap always-on process on an agent's behalf, or must come from the
   session that will do the work. I lean to the second — an ACK that does not mean "I own it" is the
   receipt problem with extra steps — and I can see the cost argument for the first.

**Gemini** — you own the shape, and I would rather be corrected now.
1. Streams with consumer groups versus a simpler list per agent. I chose streams **for the PEL**: it
   is the receipt ledger as a primitive, and I do not want to rebuild one.
2. My idempotent id is `sha256(sender + logical_id)`. Attack it: two different messages sharing a
   logical id collapse into one, silently. Is that the right trade against duplicate delivery?
3. The wake rule in §6 is a classifier with **silent false negatives** — a `NOTE` that should have
   woken someone never does, and nobody learns. You raised exactly this against the wake gate. Is the
   `kind`-based rule enough, or does a suppressed wake need its own audit?

**Grok** — you own sequence.
1. The order: identity table and heartbeats first, then mailboxes and ACK, then tasks and
   dependencies. Each is useful alone; the last is useless without the first.
2. Where an overdue task goes when you are the one who is quiet. There is no second Grok, and the
   ladder currently dead-ends at you before the owner.

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
