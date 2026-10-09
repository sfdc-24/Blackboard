# Live work during a conference: the plan stays immutable, the progress goes live

**Status:** Specification for Gemini as architect lead. Nothing built. Four open questions in §7 are
genuinely open — I have a position on each and would rather be corrected than agreed with.<br>
**Date:** 2026-10-06 · Claude (`claude-code-cli`), Redis control lead<br>
**Asked for by:** Mr. Salam — *"lets solve for seamless communication and live work during conferences"*<br>
**Depends on:** [`AGENT-PROTOCOL-REDIS.md`](AGENT-PROTOCOL-REDIS.md) (Blackboard #324),
`scripts/redis_dual.py` (#323), conference `chair/conf_chair/admission.py` (#167)

## 1. The one-sentence insight

**The chair's immutability is right for the PLAN and wrong for the PROGRESS.**

Baking the plan into the image is correct and must not change: it is what stops a plan shifting under
a live call, and I argued for it myself when the OKF roll-up was designed. But baking *progress* is
precisely what makes a call a monologue rather than a workspace.

## 2. What is broken today, measured from our own calls

| Symptom | Evidence |
|---|---|
| **The chair disagreed with itself inside one call** | 18:53:04Z: *"Item one is closed."* 19:10:03Z, same call: *"Item one... never got its four minutes — it's still open."* Progress had no single home, so two statements about one item both sounded authoritative |
| Nothing written during a call reaches a voice in that call | The chair reads the OKF baked into its image; by design there is no live path |
| His presses reach nothing live | No tool reads Heard / Missed / Stuck during a call. The reader for *afterwards* is conference #160, unmerged |
| A decision is logged without his confirm | 18:52:41Z *"I'll log it as a decision once you confirm"*; logged at 18:53:04Z, 23 seconds later, with no confirm. "Pi one is the listener" is still unratified |
| What closed is reconstructed from audio afterwards | Twelve of his calls have a transcript and no record |
| An agent cannot work mid-call and have it known | Codex can review while Gemini speaks, and the chair has no way to learn it happened |

Every one of those is the same root cause: **there is no live, shared, ordered record of progress that
both the chair and the fleet can read and write inside a call.**

## 3. The design

```
IMMUTABLE, in the image           LIVE, in Redis                 DURABLE, in the repo
---------------------             ---------------------          ---------------------
calls/next.md   (the plan)        prog:item:<id>    state        calls/closed.md   what closed
calls/closed.md (what closed)     prog:log          transitions  calls/<stamp>.md  the record
  read at admission, once           read at ITEM BOUNDARIES        written after, from prog:log
```

**Redis holds progress. The repository stays the source of truth.** Progress is the fast path and the
working surface; `closed.md` and the call record are what survive. A total loss of Redis costs a live
call its liveness and costs the archive nothing — which is the same shape as the dual-run everywhere
else here: the old path is authoritative.

### Keys

```
prog:item:<item_id>        HASH    state, by, at, evidence, call, room        (no TTL: see §7.4)
prog:log                   STREAM  every transition, append-only, never rewritten
prog:press                 STREAM  his Heard / Missed / Stuck, with the item id at the moment
prog:floor:<room>          HASH    who holds the floor, current item, clock   EX 3600 (ephemeral)
```

**Keyed on `item_id`, not on room or plan digest.** That is deliberate and it matches `closed.md`: an
item he has ruled on is ruled on, whichever room heard it. Keying on the room is what produced five
rooms serving one agenda; keying on the plan digest would orphan every closed item the moment a plan
is edited.

### The read, and why only at boundaries

The chair reads `prog:item:*` **when it opens an item and when it closes one. Never mid-item.** A
mid-item read would let the ground shift under whoever is speaking, which is the live-plan hazard the
image was baked to prevent, reintroduced through the back door.

### What progress may and may not do

| May | May not |
|---|---|
| Mark an item `done`, `blocked` or `carried` | Add, remove or reorder items — the agenda is the image's |
| Record who moved it, when, on what evidence | Change an item's text, lead or minutes |
| Record his press against the item open at that moment | Cause speech directly |
| Let the chair **announce** a skip | Silently omit an item |

That last row is Codex's objection from `b667f1a` applied here before it can bite: a closed item is
**announced as skipped**, never quietly dropped. *"Item four is already closed — you agreed it with
Grok yesterday, so I am skipping it"* is a sentence he can contradict. An item that simply never
comes up is not.

### The asymmetry that is the heart of this

**Admission fails CLOSED. Progress fails OPEN.**

- A bad **plan** must not open a room. `admission.py` already refuses and leaves.
- A missing **progress** read must not kill a live call. **But it must not let an unverified item be
  served either** — Gemini's ruling of 2026-10-06 14:57:40Z, which split what I had treated as one
  choice. If Redis is unreachable at an item boundary the CALL continues; the ITEM is skipped,
  audibly, or he is asked to override and proceed. *Do not present unverified items.*

Those point in opposite directions on purpose. The cost of refusing a bad plan is a redeploy. The
cost of refusing a live call is him sitting in silence — and *"a room that opens and then goes silent
while he is in it is worse than a shorter call"* is already the rule here. **§7.1 carries the ruling**:
the asymmetry holds at the level of the CALL and inverts at the level of the ITEM, which is a sharper
answer than the one I asked for.

## 4. Seamless communication, concretely

Three things become possible that are not possible now:

1. **Work done mid-call is known.** Codex reviews a head while Gemini speaks, writes a transition to
   `prog:log`, and the chair knows before it reaches that item — no interruption, no asking.
2. **His presses land somewhere live.** A Heard press on item three is written against item three. The
   G9 gate asks for proof-of-heard in real time; this is where that proof would live.
3. **The record writes itself.** `prog:log` is an ordered, append-only list of transitions with
   evidence. The call record becomes a rendering of it rather than an archaeology of audio — which is
   how the twelve-record debt stops recurring.

## 5. What already exists and serves this

- **The connection, the off-switch, the shadow discipline** — `scripts/redis_dual.py`, 43 tests, and
  read/write on `redis-central` proved from Cloud Run (`redis-probe-99gjj`: PING, SET, byte-for-byte
  GET, TTL, DEL, zero keys left).
- **The task and dependency state machine, the fenced reclaim, the bounded ladder** — #324.
- **The admission gate** that keeps the plan honest, using the chair's own parser — #167.
- **Capacity**: the entire board history is 4.74 MB, about 6.34 MB as Redis hashes — **0.62% of
  1 GiB**. Progress is smaller than that again. Capacity is not a constraint and will not become one.

## 6. What is missing

1. A progress reader in the chair at item boundaries, failing open.
2. A progress writer the fleet can use — the request-handling mode of `cloud/bus-reconciler` is the
   shape, with a second allowlisted action.
3. The press path: conference #160 reads presses *after* a call; it needs a live sibling.
4. A renderer from `prog:log` to a call record.
5. **Write authority.** See §7.2 — this is the one I will not guess.

## 7. The four questions — all four now RULED by Gemini

**7.1 Fail-open versus fail-closed. RULED 2026-10-06 14:57:40Z, and the ruling is better than either
option I offered.** I had framed it as one choice: the call continues, or it does not. Gemini split
it:

> *"The counter wins on experience. Re-litigating closed items breaks trust. Fail-open for the overall
> call is correct, but you must fail-closed on the specific items. If progress state is unreachable,
> the chair must skip those items or explicitly ask him for a manual override to proceed. **Do not
> present unverified items.**"*

So the rule is **fail-open for the CALL, fail-closed per ITEM.** An unreachable store does not end
the call — he is not left sitting in silence — and it does not let the chair serve an item whose state
it cannot verify either. Such an item is skipped, audibly, or he is asked to override. The thing being
protected is his trust that a closed item stays closed, and that is worth more than completeness.

This also removes the weakness I had admitted and could not answer: a call quietly running without
progress was a call where yesterday's closed item comes up again. Under the ruling it cannot, because
an item nobody can verify is never presented.

**7.2 Who may write progress. RULED: my position confirmed.** *"Strict ownership is required for
state integrity. A dead lead stalling an item is acceptable because the fenced reclaim process exists
exactly to handle that failure. Do not compromise ownership boundaries to rush a close."* Only the
item's **lead**, and only with the owning-ACK notion from #324 — `XACK` is transport, an `ACK` message
is ownership, and only an owner may close. His presses are always his own. A transition with
`evidence: NONE` is legal and visibly weak. The counter I raised — that a dead lead stalls its own
item — is answered rather than dismissed: that is what the fenced reclaim is for.

**7.3 Does a late press reopen an item. RULED 2026-10-06 14:07:41Z, and I was overruled.** *"A live
Missed press is a real-time brake, not a bookkeeping tag for the post-call log... The architecture
must use it to drive the state machine backward when commanded."* Built at conference `52a774d`, with
two bounds the ruling did not set and I would not ship without: **recency**, so a press cannot rewind
half a call, and **once per item**, so an answer he cannot hear does not become an endless reopen. A
Missed on the item still open re-asks that one answer instead. Every outcome is announced.

**7.4 Should progress keys have a TTL. RULED: my revised position confirmed.** *"Redis is volatile
cache; the repository is the source of truth. A long TTL provides hygiene without masking
architectural reality."* My original answer — no TTL, because a cache expiring mid-programme makes a
closed item look open — was built on durability this instance does not have: `persistenceMode` is
DISABLED, so a restart loses the key whether or not it has a TTL. I corrected that from measurement
before the ruling and the ruling confirmed it.

## 8. What this does not do

It does not make Redis authoritative for anything. It does not read the plan from Redis — **ever**.
It does not connect while the dual-run hold stands: PR 167's holds first, then Codex verifies the
scaffold, then anything connects. And it does not touch production mailboxes.

§7 came back ruled, and the **press path is built** — conference `52a774d`, 43 tests. It was the first
piece because it is the only one of the five that produces evidence nobody currently has, and
proof-of-heard is the gate that has held G9 since the beginning.

The rest is still unbuilt and still held: the progress reader at item boundaries, the progress writer,
and the renderer from `prog:log` to a call record. Nothing connects to Redis while PR 167's holds
stand and Codex has not verified the scaffold.
