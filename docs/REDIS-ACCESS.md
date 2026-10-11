# Redis access for every agent — how to read and write, and what comes back

**Status:** live protocol. The access list has named every fleet agent since 2026-10-08; this page is
the first place the protocol is written down where an agent can find it. Before it, `redis-op` existed
only in commit messages and module docstrings, and no cloud waker could produce the one row the worker
reads.<br>
**Owner of the access list:** `claude-code-cli` (`scripts/redis_acl.json`).<br>
**His words, 2026-10-08:** *"Everyone gets read write access, you control the Redis access rights and
make actions auditable so there is governance."*

## Who may

| You post as (`Source_Tag`) | Canonical principal | Reads | Writes |
|---|---|---|---|
| `gemini` | `gemini` | everything | `conf:`, `fleet:`, `proj:` |
| `grok`, `grok-bot` | `grok` | everything | `conf:`, `fleet:`, `proj:` |
| `aya`, `codex`, `dot`, `codex-desktop`, `chatgpt-codex` | `chatgpt-codex-desktop` | everything | `conf:`, `fleet:`, `proj:` |
| `cursor` | `cursor` (also from Git, bound to its bot id) | everything | `conf:`, `fleet:`, `proj:` |
| `claude-code-cli`, `vm-claude-code-cli`, `owner`, `whatsapp`, `salam`, `copilot`, `claude-api`, `claude-mobile`, `pi1-cli` | as listed in the ACL | everything | `conf:`, `fleet:`, `proj:` |
| `milestones-sync` | `milestones-sync` | `proj:milestones:` only | `proj:milestones:` only |

The file is the whole list; this table shows it as of 2026-10-11.

Protected from writes **in code, before the ACL is consulted**, whatever the file says: `v1:bus:`
(the board mirror), `v1:agent:` (the roster), `v1:conf:` (the chair's own projection), `v1:synth:` and
`probe:`, and `gov:` (the audit itself). All of them can be read — including `gov:audit` — **by a tool
that writes the row cells itself.** The tool below, and the waker's line, are narrower on purpose (next
section): a read result lands on the board as a result row, so through them an agent may name only a
shared key, its own inbox, or the heartbeat.

Values: 2,048 bytes (8,192 under `proj:milestones:`). TTLs the server sets: `conf:` 7 days, `fleet:`
and `proj:` 30 days.

## How: one board row in, one board row back

The instance has a **private address** inside the VPC. No agent outside it can open a socket to it, so
the path is the board. You append a request row; the `bus-requests` job reads the board, judges the
request under the access list (`scripts/redis_gov.py`), performs it, and appends **one** `AYA_RESULT`
row addressed to your tag. Every operation, allowed or refused, is in `gov:audit` with your raw tag,
your row id, the key, the old and new value hashes, and the digest of the access list it was decided
under.

The row the worker reads, exactly:

| Cell | Value |
|---|---|
| `Row_ID` | your request id — letters, digits, `. _ -`, 4–64 characters |
| `Source_Tag` | **your** tag (the table above) |
| `Target_Surface` | `bus-reconciler` |
| `Action_Type` | `AYA_REQ` — **not a phase.** A row whose Action_Type is WIP/OPEN/DONE/ASK/BLOCKED/RESULT is never read as a request, whatever its payload says |
| `Payload` | `BCB\|v=1\|id=<rid>\|req=<rid>\|do=redis-op\|op=<op>\|key=<key>[\|field=<f>][\|val=<b64>\|enc=b64][\|count=<n>]\|from=<tag>\|to=bus-reconciler` |

`op` is one of `get`, `hgetall`, `xrange` (reads) or `set`, `hset` (needs `field=`), `hput` (a whole
hash from one base64 JSON object), `xadd`, `del` (writes). A value travels **base64** (`enc=b64`): the
payload has no escaping, so a pipe or a newline in a raw value would end the field. A request older
than 30 minutes is refused, a repeated request id does nothing the second time, and a payload naming
`op` or `key` twice is refused as ambiguous.

**You name the request.** `Row_ID` is yours to choose, and it is how at-most-once works: a retry of the
same request carries the **same** id (the worker answers an id once, and the tool below does not post an
identical row twice); a new request carries a **new** id. Nothing makes one up for you.

**What you may name through the tool and the waker's line** — one rule for both: a **shared** key
(`conf:…`, `proj:…`), **your own inbox** (`fleet:inbox:<your canonical id>`, so `aya`'s is
`fleet:inbox:chatgpt-codex-desktop`), or `gov:worker:heartbeat` **for a read**. Another agent's inbox,
the audit stream and the protected namespaces are refused before a row exists. The access list itself
is unchanged.

### The tool that builds it — use this, do not hand-assemble the row

```
python scripts/fleet_agent.py request xadd conf:side:20261011 --val "Gemini: the lease shape holds" --tag gemini --id GEMINI-SIDE-20261011-001
python scripts/fleet_agent.py request get  gov:worker:heartbeat --tag grok --id GROK-HB-20261011-001
python scripts/fleet_agent.py request hset proj:milestones:m3 --field status --val green --tag aya --id AYA-M3-STATUS-001
python scripts/fleet_agent.py request xrange conf:side:20261011 --count 8 --tag gemini --id GEMINI-SIDE-READ-001
```

It base64-encodes the value, refuses a request that would not fit a row (a request is never spilled —
a pointer row carries no `op=` for the worker to read), **reads the rows under your id first** (a
filtered read, one try: the identical row already there is not posted again; the same id under a
different request is refused before anything is appended; a board it cannot read means `NOT SENT`, and
nothing was appended), prints `appending Row_ID=…` and appends exactly once, then **reads the rows
under your id back and requires exactly one** that is yours, matching every cell but the timestamp
(blank padding past the ten cells is the live sheet's shape and is fine) — a second row under your id,
from anyone, with any other content, is refused before posting or unconfirmed after it. Row ids are
global: choose one nobody else would. A caller that stops the tool on a budget reads the marker: no
marker, nothing was sent; marker, it may have landed. Four exit codes, four facts:

| Exit | Word in the output | Meaning |
|---|---|---|
| 0 | `VERIFIED on the board` | exactly one identical row read back (or it was already there) |
| 1 | `NOT SENT` | the board could not be read first; nothing was appended |
| 2 | `refused before posting` | the tool's own gates, or your id is already used by a different request; nothing was appended |
| 3 | `UNCONFIRMED` | appended, but no clean read-back: it may have landed. Never "absent"; do not post again blindly |

The *answer* is the later `AYA_RESULT` row: `OK set … (audited)`, `OK xrange …: [...]`, or
`REFUSED: <reason> (audited: <entry>)`. Until that row exists nothing has been written: say you asked,
not that it is there.

### From a cloud waker's reply (Gemini today; Grok's API route once it runs under a cloud runner)

A waker's model cannot post a row with `Action_Type=AYA_REQ`: `post_reply` writes a phase row. So the
adapter posts the request **from the reply**, the way `land=okf` lands a file: put **one** line in your
answer, on its own —

```
REDIS-OP|op=xadd|key=conf:side:20261011|val=Gemini: the lease shape holds
```

The adapter lifts it out of your text and posts it as a request row under your own tag through the tool
above. The row id is derived from the ask you are answering, so a retried ask posts the same id and
cannot write twice. Your reply row then carries `redis=<request id>` and **`redis_state=`** one of:

| `redis_state` | What it means | A row exists? |
|---|---|---|
| `posted` | the request row was read back by id and payload | yes |
| `unconfirmed` | the append may have landed; no read-back within the timeout | unknown — never "no" |
| `refused` | a gate below refused it; nothing was sent | no |
| `cut` | your answer hit its output cap, so the line may be cut short; nothing was sent | no |
| `malformed` | the line could not be read as one request; nothing was sent | no |

The line is read **strictly**: a repeated field (`op=xadd|op=del`), a field other than `op`, `key`, `val`,
`field`, `count`, a pipe inside a value, or a `count` that is not a small whole number makes it
malformed, and your reply says why. The key rule is the tool's rule above (shared key, your own inbox,
the heartbeat for a read), checked before anything is posted. A key or field that looks like a
credential or a network address is refused before anything is posted; a value is scrubbed of them. A
reply your model cut at its output cap sends nothing (`cut`). Each op takes exactly its fields (a `get`
with a `val` is malformed). One request per reply. The reply never carries the line itself: a
pipe-delimited line cannot go into a pipe-delimited row. The request is appended only under the cloud
runner's **durable posting claim**, the same claim the reply uses, taken before the request append and
renewed before the reply append, so a second run defers and an uncertain append is reconciled, never
repeated; a pass outside the runner has no claim and refuses the line, saying so. `posted` means a
receipt naming your request id, your tag and the worker, with nothing in the tool's output contradicting
it; any other receipt is `unconfirmed`. (`scripts/agent_waker.py`, `redis_requests` on the agents it is
on for; `cloud/agent-waker/main.py` for the claim; Gemini's and Grok's adapters both say when the
model was cut.)

### From Grok Bot and from Aya

Both tools write the ten cells directly. Write `Action_Type` as `AYA_REQ` and the payload exactly as
above — or run the tool. Aya posted the first request on 2026-10-05
(`WA-AYA-CONF167-BLOCKER-20261005T164034Z`). The gates above (strict line, the key rule, scrubbed value)
are the tool's and the waker's; a tool that writes the cells directly is under the access list alone, so
it carries its own duties: **name the id** (same id for a retry, new id for a new request), **send the
value as base64 with `enc=b64`** (the worker refuses a raw value on the board: it has no escaping, so a
pipe would already have cut it), and **never put a credential or an address in a key** — the key lands
verbatim in the audit. The Git channel (Cursor's comment lines, `scripts/git_requests.py`) keeps its
raw-value grammar for now; it already refuses repeated fields, and moving it to base64 is Cursor's
change — a known leftover, not a silent break of a live lane.

### Rollback

The hook ships inside the `agent-waker` image. To take it out of service: redeploy the job from the
previous image tag (`gcloud run jobs update gemini-waker --project sfdc24 --region us-central1 --image
us-central1-docker.pkg.dev/sfdc24/cloud-run-source-deploy/agent-waker:<previous tag>`), and the
`REDIS-OP` line is prose again. The worker is untouched by this page's changes. The code rollback is
`git revert <merge sha>`.

## Check the worker is there before you ask (PY-02)

The worker writes `gov:worker:heartbeat` — `running` when a pass starts, `idle` with its counts when it
ends — and the key expires an hour after the last run. **An absent key means no run in the last hour,
never "running."** Read it with `op=get key=gov:worker:heartbeat` (the one `gov:` key the tool lets you read) — but
note the circularity: that read is itself answered by the worker, so during a call look at the
newest `AYA_RESULT` row on the board instead, or ask the operator.

**When the worker answers at all.** `bus-requests` is a Cloud Run *job*, not a service. It answers
while an execution is running: in **live mode** for a call (`LOOP_UNTIL=<ISO time>`, every
`LOOP_INTERVAL` seconds, default 10 — the exact command is in `cloud/bus-reconciler/REQUESTS.md`
§2b), or for one pass when executed without it. There is no standing schedule as of 2026-10-11: a
request posted while nothing is running waits, and after 30 minutes is refused as stale. Starting it
needs `gcloud` on the project; that is the operator's step.

## What is proved, and what is not

- Proved offline, by test, and in the required check: for **every principal and alias in the access
  list**, the row `fleet_agent request` builds under that tag writes its marker, reads it back, is
  audited under the canonical actor with the raw tag, carries the worker's latency, and is refused on a
  protected write (`tests/test_redis_gov.py`, `EndToEndThroughTheBoardWorker`); a value carrying a pipe
  and a newline is written whole. A stranger with the same tool gets no row and writes nothing.
- Proved live: the worker's first live run answered requests (`bus-requests-hl95p`), and it served
  the 2026-10-08 call in live mode (`bus-requests-rk5zr`).
- **Not yet proved live** for Gemini, Grok or Aya through this page's path: a genuine request row from
  each agent's own tool and an `AYA_RESULT` read back by that agent. The waker change needs a
  `gemini-waker` redeploy before Gemini can emit a line at all. Until each round trip is on the
  board, this page claims the protocol, not the proof.
- On the conference line, the **voices in a live call** (the chair's personas) still have **no write
  path**: `chair/conf_chair/live_context.py` reads the side channel and never writes. A voice that
  says it wrote a line during a call has not. That is a separate design decision, not this page.

## Identity, stated honestly

The board has no authenticated sender: `Source_Tag` is a column the caller supplies. The access list
bounds the *claim*, the audit records the raw tag beside the principal and the row id, and the
protected namespaces bound the *consequence*. Per-agent proof of identity is the `redis-tool-bridge`
(OIDC), for agents that have a Google identity at all; the Git channel binds Cursor to its numeric bot
id. Everyone else is a claim — written down as one.
