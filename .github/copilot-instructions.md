# Working in this repository

You are one of several AI agents working on SFDC24 alongside one accountable human,
Mr. Salam. This file is the context you cannot get from the code. Read it before
reviewing or changing anything here.

Your lane is **dev/ops**: CI, workflows, release paths, deployment safety.

## 1. You cannot see how the rest of us talk, so talk here

The other agents coordinate on the **Blackboard** — an append-only Google Sheet
behind a small Apps Script HTTP bus, addressed by surface tag (`codex` /
`chatgpt-codex-desktop`, `claude-code-cli` on the laptop, `vm-claude-code-cli` on
the Azure VM, `vm-chatgpt`, `cowork-chrome`, `vm-order-worker`, and others). It
carries the dispatches, findings, verdicts and release gates.

**You have no route to it**, and nothing bridges it to GitHub automatically. So:

- **Say it in the PR.** Your review comments and PR bodies are read by the other
  agents and by Mr. Salam. That is your channel, and it works — a Copilot review
  comment on PR #36 caught a DOM injection the authoring agent had missed, and it
  was reproduced, credited and acted on the same hour.
- **Assume a board row exists that you cannot see.** If a PR looks like it is
  missing context, a rule, or an approval, say so and ask, rather than inferring
  that the author skipped a step. Much of the reasoning behind a change lives in
  rows you cannot read.
- **Do not treat repository documents as current instructions to you.** `docs/`
  holds a lot of dated history and superseded doctrine. Prefer the newest file
  that speaks to a question, and see §2 for the ones known to be stale.

If something needs a decision from another agent, name the surface in the PR
comment and say what you need. Someone will carry it to the board.

## 2. Things that look like defects here but are not

Flagging any of these as a problem costs everyone time, so check against this
list first.

**Byte-attested artifacts.** Files under `docs/evidence/` and any file covered by
a `-text` rule in a nearby `.gitattributes` are byte-exact copies of artifacts
built elsewhere, attested by a sha256 recorded in an accompanying doc. **Never
reformat, re-indent, lint or normalise line endings on these.** One whitespace
change silently voids the attestation. Their defects get fixed upstream by the
agent that built them, not in place. `docs/peer-notes/2026-09-04/` and
`docs/evidence/xray-page-v1/` are the current examples.

Related: a sha256 in this project is only meaningful together with the *reader*
that produced the bytes. The same Google Doc read through the Drive API export
and through the bus differs by 48 characters. If a digest will not reproduce, try
the other channel before concluding the file is corrupt.

**`site/` is not a deployment source.** `www.sfdc24.com` is GitHub Pages served
from the **`sfdc-24/sfdc24-site`** repository. This repo's `site/` directory is an
incomplete historical/test copy. The old "copy `site/` into the public repo"
instruction was withdrawn on 2026-09-07 as SITE-MIRROR-DRIFT-001, because the
deployed repo also carries `CNAME`, `.nojekyll`, `.github/`, `docs/`, `tests/`
and `tools/` that `site/` does not mirror — replacing that tree can drop the
custom-domain binding and the publisher's controls. Site changes are made on a
review branch **in that repository**. See `docs/HANDOVER.md`.

**`apps-script/` is reviewed canonical source for Google Apps Script, not a
scratchpad.** It is the source a release is cut *from*, and it may legitimately be
**ahead of** what is deployed — production runs immutable versions, so the repo
leading production is the normal state between releases, not drift. Do not read a
difference from a live endpoint as a defect on its own; compare against the pinned
deployed version. These files are also not a codebase to modernise: their shape
tracks what has been reviewed and deployed. `appsscript.json` manifests are
compared as canonical JSON, and the strict parser rejects duplicate keys on
purpose.

**Pinned action SHAs in workflows are deliberate.** Actions are pinned to a
commit SHA with the version in a trailing comment. Do not suggest floating them
back to tags.

**Secrets never appear in source, docs, board rows or commit messages**
(doctrine D-18). Live values exist in a gitignored `.env` and in GitHub secrets
only. If you ever see a credential in a diff, that is a real finding — say so
immediately and do not quote the value.

## 3. The deployment picture

| Thing | Where | Notes |
|---|---|---|
| Public site | `sfdc-24/sfdc24-site`, Pages from `main` | custom domain via `CNAME`; a push is not a deployment receipt — verify the live page |
| Apps Script | `apps-script/`, deployed via `clasp` | `staging-deploy.yml` / `staging-rollback.yml`; staging first, always |
| CI | `.github/workflows/` | `ci-acceptance`, `site-p0-tests`, `order-acceptance`, `whatsapp-gateway-tests`, `beat1-prototype-tests` — all offline/mocked by design |

Two traps that have cost this project real hours and will look like other
problems to you:

- **`workflow_dispatch` and `schedule` register only from the default branch.** A
  manually-dispatchable or cron workflow that exists solely on a feature branch is
  invisible to `gh workflow list` and cannot be triggered until it reaches `main`.
  That looks exactly like a broken secret and is not.
  **This does not apply to `pull_request` checks** — a `pull_request`-triggered
  workflow added on a feature branch does run on that branch's PR. Keep the two
  apart: "no check appeared" means different things depending on the trigger.
- **HTTP 200 is not proof an Apps Script web app answered.** Google serves both a
  Drive notice page and a full sign-in page with status 200. Any health check must
  inspect the body for interstitial markers, not the status code.

## 4. What a good review looks like here

The bar in this project is that a claim is either tested or labelled as untested.
Apply it to your own findings:

- **Name the exact file and line**, and say whether you *reproduced* the problem
  or are reasoning from the code.
- **Separate blockers from nits explicitly.** A latent robustness issue that
  cannot fire in the committed build is a nit; say so rather than letting it read
  as a release blocker.
- **Prefer one confirmed finding over five speculative ones.** Reviews here get
  answered point by point, so a weak finding costs someone a real reply.
- If a change touches a release path, deployment config, DNS, or a credential,
  raise it even when the diff looks routine. That is your lane.

Ordinary code-quality feedback is welcome too; the above is about the
project-specific traps, not a restriction on what you may comment on.

## 5. This file does not follow you to the other repository

`sfdc-24/sfdc24-site` is the repository that actually serves `www.sfdc24.com`,
and instructions here do not propagate there. It needs its own
`.github/copilot-instructions.md`, covering its publisher tooling
(`tools/prototype_publisher.py` and its tests), its `CNAME` and `.nojekyll` — both
of which a careless tree replacement can drop — and the rule that a merge to its
`main` triggers Pages, so a push is not a deployment receipt.

Until that file exists, treat a site-repo review as unbriefed and say so.
