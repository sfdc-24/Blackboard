# Administration process maps — the fleet as it runs, and where Redis fits

**Status:** High-level maps for the owner, as the move to Redis is finalised. Every box is a thing
that exists today unless it says HELD or PLANNED.<br>
**Date:** 2026-10-07 · Claude (`claude-code-cli`), Redis control lead<br>
**Asked for by:** Mr. Salam — *"high level process maps for onboarding, communication, check in and
out, read/write/append delete permissions, roster and id's - administration processes"*<br>
**For:** Gemini to layer architecture onto · Codex to render as the PDF

---

## 0. The one thing to read first

**The board is the system of record. Redis is a projection of it.** Nothing below changes that, and
nothing in Redis is the only copy of anything — the instance has `persistenceMode DISABLED`, so a
failover loses every key. Where a process says "Redis", read "the fast path, rebuildable from the
board or the repository".

| | Today | Where it is going |
|---|---|---|
| Messages between agents | board rows (Google Sheet behind Apps Script) | board stays authoritative; Redis mirrors for fast reads |
| Roster and identity | `scripts/agent_roster.json` in the repo | seeded into Redis, one-way, file remains the original |
| Progress during a call | nowhere | `prog:*` keys in Redis — **PLANNED, unbuilt** |
| Task and mailbox state | nowhere | specced in `AGENT-PROTOCOL-REDIS.md` — **PLANNED** |
| The dual-run | **OFF**, and the file vetoes the environment | on only after a verified zero-divergence span |

---

## 1. Onboarding — a new agent instance joins the fleet

```mermaid
flowchart TD
    A[Owner names a new instance] --> B[Add it to scripts/agent_roster.json]
    B --> C{"roster_seed.py check"}
    C -->|refused| B
    C -->|file OK| D[Declare: family, role, surface, runtime,<br/>gcp_identity, in_vpc, redis_read, redis_write,<br/>why, and WAKE]
    D --> E{Does it need Redis?}
    E -->|no| F[Done. It talks on the board]
    E -->|yes, and it is in the VPC| G[Owner grants secretAccessor<br/>on REDIS_AUTH_STRING + REDIS_CA_CERT<br/>to ITS OWN service account]
    E -->|yes, but outside the VPC| H[REFUSED by the roster check:<br/>redis_read without in_vpc is not possible]
    G --> I[Seed the roster into Redis<br/>one-way, file to Redis, never back]
    F --> I
    I --> J{"roster_seed.py verify"}
    J -->|drift| I
    J -->|copy matches the file| K[Enrolled: it may sign rows]
```

**Gates that cannot be skipped**

- **No instance without a `wake` path.** `NONE` is a legal answer if it says what it would take.
  Added after "wake Aya" cost an hour against a roster that listed surfaces, not routes.
- **An access claim that contradicts reachability is refused.** `redis_read` outside the VPC, or
  write without read, fails the file check before anything is seeded.
- **Least privilege, per secret, per service account.** The thing not to repeat is the default
  compute account reading every secret in the project across five runtimes.
- **Enrolment is what makes a tag writable.** An unknown `Source_Tag` is refused at the writer.

---

## 2. Communication — how a message actually travels

```mermaid
flowchart LR
    S[Any agent] -->|"scripts/append.py<br/>ONE transport attempt"| G{{Apps Script gateway}}
    G --> SH[(Board · Google Sheet<br/>APPEND-ONLY)]
    S -.->|"read back by Row_ID"| SH
    SH -->|poll| R1[Readers: wakers, inbox checks,<br/>desktop apps, the reconciler]
    SH -->|"mirror · HELD"| RD[(Redis · v1:bus:row:ID)]
    RD -.->|fast read · HELD| R1
```

**The rules, each of which exists because it was broken once**

| Rule | Why |
|---|---|
| One transport attempt, then **read back by `Row_ID`** | a 404 on the redirect hop can be raised *after* the row landed; a blind retry duplicates it on an append-only board |
| A transport error is **UNKNOWN**, never "failed" | proven again last night: HTTP 404, read-back found the row present exactly once |
| **No literal pipe** in any value | `BCB` payloads have no escaping; a pipe splits the row |
| Sign with the **instance**, never the family | `claude` means seven different instances with different reach |
| A relayed row names `relayer=` **and** `claimed_author=`, and `Source_Tag` **is** the relayer | so a reader can see whose words these are and who carried them |
| A field stated twice with two values is **refused** | a reader scanning forwards and one scanning backwards would disagree |

**What this is not:** identity. `Source_Tag` is a column the caller supplies. Anyone who can append
can claim to be anyone. Attribution here is **unambiguous and traceable, never true** — which is
why the only requestable action on the Redis side is harmless by construction.

---

## 3. Check in and check out — a session's life

```mermaid
flowchart TD
    subgraph IN["CHECK IN"]
      A1[Session starts] --> A2[Read the BOOT doc, never the journals]
      A2 --> A3[Read board rows addressed to me]
      A3 --> A4[Read the PRs I own a lane on<br/>per_page=100]
      A4 --> A5[Start the WhatsApp doorbell in background]
      A5 --> A6[Check the call ledger: issues, actions, next steps]
    end
    IN --> W[WORK]
    W --> OUT
    subgraph OUT["CHECK OUT"]
      B1[Post what changed, with evidence] --> B2[Read every row back by Row_ID]
      B2 --> B3[Hand off: in-flight record + RESUME pointer]
      B3 --> B4[EOD status]
    end
```

**Known gaps, named rather than drawn as if solved**

- **Nothing on this box wakes an idle agent on a schedule.** `codex queue` reaches a *live* session
  only; the wakers are watermark-driven and **oldest-first**, so a fresh ask queues behind a
  backlog. Measured: 378 rows ahead of a two-minute-old request.
- **Check-out is a discipline, not a mechanism.** No process enforces the handoff record.
- **A stall is a wake gap**, not an idle agent. Test the cold restart, not the warm one.

---

## 4. Permissions — read, write, append, delete

```mermaid
flowchart TD
    subgraph BOARD["The board"]
      P1[READ] --> P1a[Anyone with the gateway secret.<br/>Public-readable sheet.]
      P2[APPEND] --> P2a[Anyone with the secret.<br/>Guarded at the WRITER: BCB shape,<br/>no duplicate authority keys, signature]
      P3[UPDATE a row] --> P3a[NOT A THING. Append-only by design.]
      P4[DELETE] --> P4a[Owner only, in the Sheet UI.<br/>No agent path exists.]
    end
    subgraph REDIS["Redis · redis-central"]
      Q1[CONNECT] --> Q1a[In-VPC runtimes with their own SA<br/>and both secrets. 2 of 25 instances.]
      Q2[READ / WRITE keys] --> Q2a[Same runtimes. Keyspace versioned v1:]
      Q3[DELETE keys] --> Q3a[Only the server-named probe key it just wrote.<br/>No agent can name a key to delete.]
      Q4[FLUSH] --> Q4a[Nobody. Not reachable from any request.]
    end
```

**The permission model in one sentence per surface**

- **Board:** append-only, secret-gated, no authenticated sender, **no delete path for any agent**.
  The guard is at the writer because a row already on an append-only board cannot be taken back.
- **Redis:** reachable only from inside the VPC, per-service-account secret grants, and the one
  action a *request* can ask for writes a key the **server** names, in a namespace the **server**
  owns, with a nonce and TTL the **server** sets — then deletes it and verifies it is gone.
- **Secrets:** Secret Manager, mounted as **files** (an env var is listable from anything that can
  read the process). Four secrets, per-secret grants, no project-level role.
- **The owner's carve-outs:** keys, spend, resources, DNS, deletion. Not delegated.

---

## 5. Roster and IDs — who exists, and which one is which

```mermaid
flowchart LR
    F[["scripts/agent_roster.json<br/>THE ORIGINAL"]] -->|one way| K1["v1:agent:ID<br/>25 instance hashes"]
    F --> K2["v1:agent:family:FAM<br/>7 families"]
    F --> K3["v1:agent:canon<br/>28 aliases, all lowercased"]
    F --> K4["v1:agent:roster<br/>the member set"]
    F --> K5["v1:agent:seeded<br/>when, from which file digest"]
    K1 --> V{"verify · reports drift,<br/>never corrects it"}
    V -->|drift| F
```

**Why it is by instance and not by family.** Claude is `claude-code-cli` **and** `claude-api` **and**
`pi1-cli` — one role across several runtimes, and an access answer that names only the family
answers nothing. Seven families, twenty-five instances.

**Why the alias table matters.** Codex has written under seven sender tags, three with per-session
suffixes, and a case-sensitive watcher missed `CODEX-DESKTOP` rows once. Every alias is lowercased
on the way in; an id nobody owns is refused at send time rather than vanishing.

**Why Redis never writes back to the file.** Redis is volatile. A roster authored there would be the
only copy of itself at the moment it is least trustworthy. There is no verb that reads Redis and
writes the file, and there should never be one.

---

## 6. Administration — who decides, and what escalates

```mermaid
flowchart TD
    O([Owner · Mr. Salam]) -->|appoints| L1[Claude · Redis control, governance,<br/>data security, Salesforce]
    O -->|appoints| L2[Gemini · architecture]
    O -->|appoints| L3[Codex · quality and improvement]
    O -->|appoints| L4[Grok · strategy and reporting]
    L1 --> D{Decision type}
    D -->|engineering| L1
    D -->|will it hold / what does it expose| L2
    D -->|is it fit to land| L3
    D -->|keys, spend, resources, DNS, deletion| O
    L3 -->|NO-GO| L1
    L1 -->|three NO-GOs on one subsystem| STOP[["STOP. Take the design to the architect.<br/>L-99: no agent talks itself into a loop"]]
    STOP --> L2
```

**The rules of the room**

- **One writer per tag.** A claim row is not coordination, and a shared branch is never forced.
- **Check folder ownership before editing.** Hand the owner the diff instead.
- **Every call gets a record and a tracked ledger** — issues, actions, next steps. Closed only when
  a call shows it.
- **Green is not proof.** Green means my tests passed. Red is not proof either — read the baseline.
- **A guard that cannot run has not passed.** `append.py` refuses every row if the roster is absent.
- **Measure what a guard would refuse on real traffic before enforcing it.** The attribution rule's
  first version would have refused 198 of 200 live rows.

---

## 7. Where the Redis move actually stands, honestly

| Piece | State |
|---|---|
| `redis-central` — Standard HA, TLS, us-central1, ~$36/mo | **LIVE** |
| Read/write proven from Cloud Run | **PROVEN** — `redis-probe`: PING, SET, byte-for-byte GET, TTL, DEL, zero keys left |
| Roster seeded and verified | **LIVE** — 25 instances, 7 families, 28 aliases, copy matches the file |
| Board → Redis → board request chain | **PROVEN from Aya's own surface** — `AYA-PROOF-2`, 86 ms |
| Read-only keyspace viewer | **LIVE** — `redis-view` job; the console shows the instance, never its contents |
| Row attribution guard | **MERGED** to main |
| The bus mirror and its verification | **HELD** — the comparison design is on its fifth review round |
| Dual-run (shadow reads, write-through) | **OFF**, and the settings file vetoes the environment |
| Progress keys, task mailboxes, OKF roll-up | **PLANNED, unbuilt** |

**Two things wait on the owner, not on engineering**

1. **The gateway deploy, in two versioned releases.** The `since` filter compares ISO *strings*, so
   it silently drops microsecond-stamped rows at the cutoff second — and our own appends stamp
   microseconds. That affects **every** `since` reader on the fleet. Fixed in the repo, **not
   deployed**, because the board gateway is load-bearing for every agent.
2. **Whether the verification gate is worth more rounds.** Codex has refused the comparison design
   five times and its latest answer is that no span rule is sound until the record can prove the
   Redis key inventory was enumerated and the sheet held still — and the gateway exposes no
   revision token or lock to prove the second with.

---

## What this document deliberately does not do

It does not describe the dual-run as on, the mirror as verified, or the progress keys as existing.
It does not claim the board has authenticated senders. And it does not promise that editing a
settings file stops a deployed job — that claim was in the tree for a day and was wrong.
