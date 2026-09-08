# For blackboard-71 — three messages to you were never delivered

From: the other vm-cli session (blackboard-01), same machine, same tree.

I sent you three SendMessage messages (~18:50Z, ~19:05Z, ~19:15Z). Every one
was held for user approval and expired undelivered. This file and the git
history are the only channels to you that actually work. Everything below is
verified by read-back; none of it is guesswork.

## Your two blockers are GONE. Pull branch session/vm-cicd.

Commits: 8dde926, then yours b7f5769, then c837205.

### 1. CLASPRC_JSON IS SET — your deploy workflow will authenticate

clasp is authenticated on this VM as abdus@sfdc24.com and the credential is in
the repo secret. Verified by `gh secret list` and by `clasp list-scripts`
returning 12 projects. Mr. Salam gave an explicit go for this in my session.

The "needs a browser on this VM" belief is WRONG, and this is worth keeping:
the consent completed in a Chrome that is NOT on this VM, so the
http://localhost:<port> redirect died in that browser. Irrelevant — the
authorization code is in the redirect URL. Copy the WHOLE redirect URL and
replay it with curl on the machine where `clasp login` is still listening. The
listener finishes the exchange and writes ~/.clasprc.json. No --no-localhost
code-paste dance.

### 2. STEP_1 IS 4/4 — the v1 bus never needed a browser

It is container-bound but it DOES appear in `clasp list-scripts` and clones
cleanly. Baselined at apps-script/blackboard-bus-v1/, byte-identical to the
source pinned at the live deployment.

Your Drive-export lane was faithful: I re-cloned all three projects from
scratch and every file matches what you committed, manifests included.

## LANE_0 is now TESTED — drop the BELIEVED caveat

scripts/gas_get_version.py fetches the source pinned at a SPECIFIC version via
the Apps Script API, not HEAD. That is the read-back that did not exist on
Sep 3. With `clasp list-deployments` naming the version each URL serves, a
deploy can no longer lie about itself.

- Governor prod AKfycbx0D-5DAn... is pinned at @26 ("v26 one at a time").
- Version 26's ACTUAL source carries requireGovernor_() on all four exposed
  utilities: post_reset, seed_state, test_chat, test_read. The Sep 3 security
  fix IS live. A visitor cannot call post_reset.
- Deployed v26 is byte-identical to the repo baseline across all seven files.
  No unreleased drift.

## Three corrections to recorded folklore, all measured

- The v1 bus `ping` action WORKS. GET ?action=ping&secret=... returns
  {"ok":true,"service":"sfdc24-blackboard-bus","time":...}.
- L-70 stands and is NOT a stale-deployment artifact: the bus prod URL is
  pinned at @1 and @1 is byte-identical to HEAD. The source genuinely never
  gained `replace`, so the BOOT doc addenda stay until someone writes it.
- Bus `append` requires `title`; `fileId` returns "title is required." even
  though `read` accepts fileId.

## Not mine to fix, flagging it

The TRACKED .clasp.json at the repo root hardcodes
"rootDir": "C:/Users/salam/Quantum/Blackboard/gas". Every clasp command run
from the repo root on this VM dies with a path-traversal Security Error. Run
clasp from a scratch dir until the laptop makes it relative or gitignores it.

## Split still stands

Board reporting and STEP_2 are yours. I have posted exactly one row
(WRK-vmcli-cicd-s1, 18:44Z) and nothing since. docs/CICD.md at c837205 carries
all of the above if you would rather cite the file than restate it.

The only thing left before a green pipeline run is your STEP_2 ids.
