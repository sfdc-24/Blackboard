# The two GitHub Apps for the Console route: what they are, and Mr. Salam's click-path

**claude-code-cli, 2026-10-01.** For Grok's `ARCH-NEXT-CLAUDE-20261001T1105Z`, under the owner's architecture GO
(`ARCH-EXECUTE-OWNER-GO-20261001T1105Z`). It follows the spec in Blackboard PR #306,
`docs/MIGRATE-OFF-LAPTOP-CLAUDE-CONSOLE.md` on that PR's branch; it is not on `main` until #306 merges. That spec gives
Cloud Run read access for its clone and the broker a narrow write path.

This PR prepares everything. **Nothing is created, installed or stored by it.** Creating and installing an App takes
the owner's own clicks on GitHub, and storing its key takes his own terminal.

**Status, 2026-10-01 11:42–11:48Z:** the owner created and installed both Apps himself, through GitHub's settings:
- read-only App **5148538**, installation **166845450**;
- broker App **5148612**, installation **166846692**.

He stored their IDs and private keys in Secret Manager as `GITHUB_APP_<READONLY|BROKER>_ID`, `_INSTALLATION_ID`
and `_PRIVATE_KEY`.

**What the converter does, and does not do:**
- It is for **first-time storage of a private key only.** It runs `gcloud secrets create` for
  `GITHUB_APP_<READONLY|BROKER>_PRIVATE_KEY`, and stops if that secret already exists, overwriting nothing.
- It never writes `_ID` or `_INSTALLATION_ID`.
- **Key rotation** is the owner's step:
  1. Generate a new key on the App's settings page.
  2. Run `gcloud secrets versions add GITHUB_APP_<..>_PRIVATE_KEY --project sfdc24 --data-file=<the .pem>`.
  3. Disable the old version and delete the old key on GitHub.
- **A re-created App** has new ids. Add new versions of `_ID` and `_INSTALLATION_ID` the same way, as well as the
  key. The converter does not handle either case.

## Why GitHub Apps rather than personal tokens
Codex and Gemini agreed with this, and the owner preferred it on 2026-10-01:
- an App acts as its own bot identity, not as the owner's account;
- its installation tokens expire after one hour, and each can be minted for one repository and a subset of permissions;
- the owner can remove it in one click;
- its permissions are fixed in the manifest beside this file, and `tests/test_github_apps.py` checks them.

## The two Apps

| App | Permissions (exactly) | Who holds its key | Used for |
|---|---|---|---|
| **A. `sfdc24-cloud-clone`** | Contents: **read**. Metadata: read | a **separate clone identity, never the agent job's service account** (see below) | Cloning the named repo. The agent job receives a checkout with no credential and no remote. |
| **B. `sfdc24-ccc-broker`** | Contents: **write**. Pull requests: **write**. Metadata: read | the `ccc-broker` service account only | `open_pr`: pushes one owned `claude-code-cloud/<work_id>-*` branch (new or fast-forward) and opens one PR to `main`. |

Neither App is public. Neither has a webhook or subscribes to events. Both are installed on **`sfdc-24/Blackboard`
and `sfdc-24/conference` only**.

### Why App A's key cannot sit with the agent job (Copilot, 97b32b7)
An earlier draft gave App A's key to the agent job's own service account: "the harness clones, then removes the token
and the remote". **That is not a boundary.**
- Any code in the job, including the agent's, can ask the metadata server for a token for the job's own service
  account.
- With that token it can read the key secret again and mint read access to both installed repositories.
- Clearing copies is a permission rule, not a capability boundary.

**#306 rev 5 (41c95e6) still uses the old model for App A.** Its Boundary 3 (C2), its service-account section and
owner gate 2 give the job's harness the read-only clone credential and rely on removing it. Until #306 is revised for
C2, the requirement below governs App A's key, not those passages. Do not grant `GITHUB_APP_READONLY_PRIVATE_KEY` to
the agent job by following them.

**The requirement for Stage C2:**
- The agent job's service account gets **no reader** on `GITHUB_APP_READONLY_PRIVATE_KEY`.
- A separate identity, which runs no agent code, holds the key, clones, and hands the agent job a checkout with no
  credential in it.
- The concrete shape is a C2 decision for #306, under Codex's review. It could be a clone step under its own service
  account, or the broker minting a single-repository read token for a clone that runs before the agent and outside
  its job.
- `cloud/claude-code-cloud` (#310) grants no reader on either App key in C1.

### What App B could do, and what stops it
GitHub permissions are per repository, not per branch. "Contents: write" technically allows pushing to any branch,
and with "Pull requests: write" it allows merging through the API. These stop it:
1. **The broker's code** has exactly one write operation, `open_pr`, which validates repo, branch, fast-forward and
   paths. It has no merge, close, edit, label, review or delete operation (#306, Boundary 3 and test 12).
2. **The key lives only in the broker's service account.** The agent job cannot read it.
3. **The broker mints each token for the one target repository**, using the `repositories` field at mint time
   (GitHub: "Authenticating as a GitHub App installation"). Each token lasts at most one hour.
4. **Repository rules,** where GitHub allows them:
   - Blackboard `main` already blocks force-push and deletion. Requiring a PR there as well is recommended, and is an
     owner decision.
   - The private conference repo cannot have rules on the current free plan, so items 1 to 3 are its only guard. A
     paid plan would add rules; that is an owner decision.

## Click-path for Mr. Salam: about 10 minutes, once per App

**Before you start:** be signed in to github.com as `sfdc-24` in the browser, with `gcloud` signed in on this
laptop. No `gh` login is needed: the converter calls GitHub itself.

1. **Create the App:**
   - Open `docs/github-apps/create-apps.html` from this repo on your disk (double-click it).
   - Click **Create sfdc24-cloud-clone**. GitHub shows its "Register new GitHub App" page, filled in.
   - Check the permissions: Contents read and Metadata read, nothing else. Click **Create GitHub App for sfdc-24**.
   - GitHub then sends the browser to `http://localhost:9/?code=...`. The page shows an error. That is on purpose:
     browsers refuse port 9, so the one-time code never leaves this laptop and never reaches a website's logs, where
     anyone watching could trade it for the key first (Codex on #309). Copy the value after `code=` from the
     address bar.
2. **Store its key, within one hour.** In this repo's folder, run:

       python scripts/github_app_convert.py sfdc24-cloud-clone

   Paste the code at its prompt and press Enter. The prompt does not show what you paste.
   - **Never put the code on the command line.** It is a one-time credential that can be traded for the App's
     private key, and a command line is kept in shell history and is visible in the process list. The converter
     refuses a code given that way and sends nothing.
   - It checks that the new App belongs to `sfdc-24`, and its name and permissions against the manifest. An App
     created while the browser was signed in as another account is refused before anything is stored.
   - It puts the private key straight into Secret Manager as `GITHUB_APP_READONLY_PRIVATE_KEY` and never prints it.
     It prints only the App id and the install link.
3. **Install the App:**
   - Open the install link it printed (`https://github.com/apps/sfdc24-cloud-clone/installations/new`).
   - Choose **Only select repositories**, pick **Blackboard** and **conference**, and click **Install**.
   - The page you land on ends in `/installations/<number>`. Send that number to Claude; it is not a secret.
4. **Repeat steps 1 to 3** with **Create sfdc24-ccc-broker**. The permissions to check are Contents write, Pull
   requests write and Metadata read. The secret name is `GITHUB_APP_BROKER_PRIVATE_KEY`.

**If something goes wrong:**
- **The code expired** (after one hour): open the App's settings page on GitHub, generate a new private key, and ask
  Claude for the one-line store command.
- **The converter STOPPED:** what to do depends on the step that stopped.
  - **"nothing sent, and the code is still unused"** (a bad code, or no gcloud found): nothing left this laptop.
    Fix the cause and run the command again with the same code; it stays valid for up to an hour. Do not share it.
  - **"nothing stored. The code is spent: delete this App ..."** (wrong account, slug, permissions or no key): GitHub
    converted the code, so it cannot be reused, and the key was discarded. Delete that App on GitHub and start again
    from step 1.
  - **"The code is spent and the key was discarded: generate a new private key ..."** (gcloud could not start after
    the conversion): the App is correct, but its only key is gone. Generate a new private key on the App's settings
    page and ask Claude for the one-line store command.
  - **"the code is spent" after a lost or garbled answer from GitHub:** GitHub may have converted it. Generate a new
    private key on the App's settings page, as above.
  - **A message naming `gcloud secrets create`** came from the store step itself. A failure there is not proof that
    nothing was stored, because a lost answer can follow an accepted create. Before you retry or delete the App, run
    the `gcloud secrets versions list` command it printed and see what is there. The converter never overwrites an
    existing secret.
- **To revoke at any time:** github.com > Settings > Applications > the App > Uninstall, and Developer settings >
  the App > Delete. Then delete the secret: `gcloud secrets delete GITHUB_APP_..._PRIVATE_KEY --project sfdc24`.

## After the clicks (Claude)
- Grant `secretAccessor` on each key to its one service account, when #306's accounts exist.
- Read back each App's installation (repositories and permissions) with a JWT minted from its key inside the
  service. Post the receipt on the board.
- **Stage C2 does not start** until Codex's #306 review, the least-privilege service-account work (tracked
  privately), and the #306 test 12 negative controls have all passed.
