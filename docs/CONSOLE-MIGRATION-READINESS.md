> **SUPERSEDED 2026-09-26** by [`EXPRESS.md`](EXPRESS.md): kept as history, do not follow it. Its status and the reason are in [`DOC-REGISTER.md`](DOC-REGISTER.md).

# Running SFDC24 off the Claude console instead of the laptop

**Measured 2026-09-22 on the laptop by `claude-code-cli`.** Evidence levels:
**MEASURED** = observed today; **BELIEVED** = inferred and marked.

Companion to `docs/claude-cli-vs-console.md`, which is grok's analysis of the
console *as a channel* (fast judgement, no tools). This document is the other
question: can the console be the **runtime** — the thing that actually ships.

> The credential inventory behind the counts below is deliberately **not** in
> this repository. A list of which secret lives in which file is a map for
> whoever reads it next, and that rule already cost us one force-push. It is on
> the laptop, gitignored, and available on request.

## The honest starting point

**Nothing has migrated yet. MEASURED.** Every deploy, pull request, probe and
board write on 2026-09-22 ran from the laptop: ten Apps Script versions
(v39→v48), seven merged pull requests across two repositories, and every live
verification. None of it could have run from a console session today.

Two things are easy to mistake for this migration and are not it:

- **PR #172 `codex/openai-cloud-migration`** moves *Codex* to OpenAI cloud — a
  different vendor and a different runtime, and it is his own thread. It does
  not move Claude.
- **`docs/claude-cli-vs-console.md` (PR #142, draft)** recommends console for
  judgement and CLI for anything toolful. That is a routing rule, not a
  migration plan, and its own conclusion is that toolful work stays on a machine
  "until proven". Proving it is what this document is for.

## What is bolted to the laptop

**MEASURED, stated as counts rather than locations.**

- **Six credential stores**, none of them in the repository — correctly. Between
  them they cover Apps Script deployment, GitHub, GCloud, Salesforce, Azure, and
  a local environment file holding **30 keys** across nine providers and
  services. A console session has none of it, and no amount of cleverness
  substitutes for a secret.
- **Nine enabled scheduled tasks**: four agent doorbells, the WhatsApp inbox
  poller and outbox, the board backup and the inference report. These are the
  fleet's heartbeat. **If the laptop is off, the fleet is silent** — nothing in
  a console session rings those bells. This is the part nobody has started, and
  it is not a Claude problem: it is "where does the cron live".
- **Local state that exists nowhere else**: 94 git worktrees and 57 untracked
  paths in the Blackboard working tree, plus four tracked non-doc files that
  name the machine. The standing lesson applies — 28 files of running fleet code
  once existed only in a working tree.

## The probe, and why it did not run

A cloud session was launched twice today to do a small real task end-to-end
(repo read → edit → suites → pull request). **Both attempts were refused before
starting. MEASURED:**

```
Refusing to use <lowercase path>\.claude\worktrees\agent-... as an isolation
worktree: git resolves its working tree to <canonical path>/.claude/worktrees/
agent-... (a core.worktree redirect, or a checkout discovered above it), so
commands run there would write outside the worktree.
```

**The cause is the path case, not the cloud.** The session was started from a
lowercase spelling of the project directory; git resolves the repository to its
canonical case. Isolation refuses when it cannot prove writes stay inside the
worktree, and a case mismatch is exactly that. `isolation: worktree` and
`isolation: remote` failed identically, which is what rules out "remote is
gated" as the explanation.

**The fix is one thing: start the session from the canonical-case directory.**
This is the third time case has cost us — `clasp pull` refuses a non-canonical
directory, and a pruned worktree once resolved to the home repository. It belongs
in the start-up procedure, not in anyone's memory.

A leftover locked worktree from the first attempt remains under
`.claude/worktrees/`. It is untouched: deleting worktrees is not covered by
standing permission.

## What the console could do today, and what it could not

**BELIEVED** except where marked, and deliberately pessimistic.

| capability | console | why |
|---|---|---|
| Read the repo, reason, write a spec | **yes** | no credential needed |
| Branch, commit, push, open a pull request | **yes**, given a GitHub token in the cloud environment | |
| Run the python and node suites | **yes** | offline suites, no secret; whether `node_modules` is present is the only open question |
| Read the live board | **yes**, given the bus secret | plain HTTPS POST |
| Probe the live `/exec` | **yes** | public endpoint, no secret. MEASURED today with nothing but a User-Agent |
| **Deploy Apps Script** | **no** | needs an interactive Google credential that lives on the laptop |
| Read a Salesforce org | **no** | needs a credential |
| Send WhatsApp | **no** | needs a credential |
| Ring the agent doorbells on a schedule | **no** | the cron is nine Windows tasks |
| Touch the Azure VM | **no** | needs a credential |

So the split is not "console versus laptop". It is:

> **Anything that needs only the repository, GitHub and public HTTPS can move
> now. Anything that needs a credential or a clock cannot move until that
> credential or clock has a home off the laptop.**

## The test, defined so it can be run and failed

In this order. Each stage is independently useful, and each either passes with
evidence or stops.

**Stage 1 — does a cloud session exist at all?**
Start from the canonical-case directory and launch one cloud agent on a task
needing nothing but the repository. The stale-reference cleanup that accompanies
this document is exactly that shape and is deliberately small.
*Pass:* it reports its working directory, `git rev-parse HEAD`, node and python
versions, whether `node_modules` exists, and whether it reached github.com.
*Fail:* it cannot check out, or it has no network.

**Stage 2 — can it ship?**
Same task: branch, commit, push, open the pull request, let CI run.
*Pass:* a pull request opened by the cloud session, suite counts in its body,
green checks. *Fail:* no GitHub token.

**Stage 3 — can it see what we see?**
Read the live board back by Row_ID and probe the live `/exec` with a unique
`vid`. *Pass:* a row it did not write, and a 200 carrying a conversation token
and an audio key. *Fail:* egress blocked, or no bus secret. Standing trap: one
bus read touches two hosts and the redirect host must be allowlisted too.

**Stage 4 — the credential question, and it is his to answer.**
Nothing above needs the Apps Script credential. Stage 4 is whether deploys move
to a cloud runtime at all. **The recommendation is that they do not.** The better
answer is to take deployment off interactive credentials entirely and run it from
GitHub Actions with a service account, so *no* human session holds a Google
refresh token. That is a real piece of work, not a setting.

**Stage 5 — the clock.**
The nine scheduled tasks are the fleet's heartbeat and the laptop is their only
host. Moving them is its own project with its own decision: Actions `schedule`,
a GCE timer, or leave them and accept the dependency. Until then, "running
solely off the console" is not true however well stages 1–3 go, and saying
otherwise would be a claim the evidence does not support.

## Straight answer

- **Where it stands:** not started. The console has never shipped anything here.
- **What blocks the first test:** one thing — the path case.
- **When a test can run:** immediately after that. Stages 1 to 3 are a single
  session and need no new secret.
- **What "solely off the console" needs beyond the test:** a home for six
  credential stores and a home for nine scheduled tasks. Those are the actual
  migration, and both are his calls.
