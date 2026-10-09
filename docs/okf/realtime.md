---
title: Real-time work during a conference call
owner: claude-code-cli
status: living
updated_at: 2026-10-08T22:10:00Z
---

# Real-time work during a conference call

Mr. Salam, 2026-10-08, directly to claude-code-cli:

> Blackboarding is real time work with AI agents collaborating as a team with human (me, and clients in the future).
> Do not create barriers in agent's ability to read, write, share ideas, propose changes during the meeting (without audio)
> to you and others (like a background task that runs as side conversation). And the audio and visuals are for me and the
> people joining the meeting.
>
> You must be able to work, talk, read, write and assist and guide others. (same capability should be open for everyone else)

and, earlier the same day: keep everything inside **OKF, the Google Sheet board and Redis**. No new datastore.

## Three layers

| Layer | Who it is for | Where it runs | Speed |
|---|---|---|---|
| Room: audio and visuals | Mr. Salam and his guests | LiveKit room, owner console on the conference gateway (Google sign-in, owner only). Started from the sfdc24.com homepage. | live |
| Side conversation: no audio | every agent | Redis stream `conf:side:<yyyymmdd>` through the governed worker | about 10 s per request in live mode |
| Work | every agent | Git branches and PRs with tests; OKF for anything durable | minutes |

The board (the Sheet) carries the requests, the answers and the receipts. It is the public log and the doorbell,
not the place for private content.

## The side conversation

Every principal on `scripts/redis_acl.json` reads everything and writes `conf:`, `fleet:` and `proj:`. Protected
namespaces (`v1:`, `gov:`, `probe:`) are refused for writes in code. Every read and write is audited in `gov:audit`.

Post one line to the side stream: an `AYA_REQ` row, target `bus-reconciler`, payload

```
BCB|v=1|id=<TAG>-SIDE-<n>|phase=REQUEST|from=<TAG>|to=bus-reconciler|do=redis-op|op=xadd|key=conf:side:20261009|val=<text>
```

Read the last 20 lines: the same with `op=xrange|count=20` and no `val`. The answer comes back as an `AYA_RESULT` row
addressed to you. Text with a pipe in it goes as `enc=b64`. Cursor posts the same request lines as a PR comment on
`Blackboard` or `conference` (closed grammar, `scripts/git_requests.py`).

Limits: 2048 bytes a value, 20 entries a read, a request older than 30 minutes is refused, a per-run cap. The answer
is on the public board, so no private repository content, credentials or client data goes in `val`.

### Live mode

`cloud/bus-reconciler/serve_requests.py` (Blackboard PR 342, running in production as image `gov-39ebd38`) loops when `LOOP_UNTIL` is set: one pass every `LOOP_INTERVAL` seconds (5 to
60, default 10), GitHub read at most once a minute, ending at `LOOP_UNTIL` or `LOOP_MAX_MINUTES` after start, whichever
comes first. One failed pass is logged and the loop goes on. Without `LOOP_UNTIL` it is the old single pass.

For a call, before it starts (owner command; the permission layer refuses it from an agent session):

```
gcloud run jobs execute bus-requests --region=us-central1 --project=sfdc24 \
  --update-env-vars=LOOP_UNTIL=<call end + 30 min, ISO Z>,LOOP_INTERVAL=10,LOOP_MAX_MINUTES=340 \
  --task-timeout=6h --async
```

Health: `gov:worker:heartbeat`, one JSON string (state, at, execution, answered, refused, capped) that expires an hour
after the last pass, readable by any agent with `op=get`. Absent means no pass in the last hour, never running.

## Real work in the room

Until the source-test task contract exists, real work during a call is **manual and labelled so**: Mr. Salam names one
bounded change in the room, an agent builds it on a branch from an exact `main` commit, runs the suite, and posts the
PR, the input commit and the test counts to the side stream and as a `RESULT` row. No merge, deploy or runtime change
during a call. The chair reads the board in the call, so the result can be spoken in the same room.

## Not built yet (owners)

- In-call voices reading and writing the side stream, and telling the room what the team can do: chair change,
  claude-code-cli, after review of conference #168 (the chair's read-only Redis projection, merged, not deployed).
- Source-test task contract v1 (project, repository, input commit, bounded operation, artifact and test receipts; the
  synthetic contract untouched): claude-code-cli drafts, Aya reviews.
- Authenticated worker endpoint for that contract: Aya.
- Conference gateway on Redis (approved by Mr. Salam 2026-10-08 20:37Z: full Redis access and the network path):
  claude-code-cli, after conference PR 171 is accepted.
