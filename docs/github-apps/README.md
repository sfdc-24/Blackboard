# The two GitHub Apps for the Console route: what they are, and Mr. Salam's click-path

**claude-code-cli, 2026-10-01.** For Grok's `ARCH-NEXT-CLAUDE-20261001T1105Z`, under the owner's architecture GO
(`ARCH-EXECUTE-OWNER-GO-20261001T1105Z`). It follows Blackboard #306 (`docs/MIGRATE-OFF-LAPTOP-CLAUDE-CONSOLE.md`),
which gives Cloud Run read access for its clone and the broker a narrow write path.

This PR prepares everything. **Nothing is created, installed or stored by it.** Creating and installing an App takes
the owner's own clicks on GitHub, and storing its key takes his own terminal.

## Why GitHub Apps rather than personal tokens
Codex and Gemini agreed with this, and the owner preferred it on 2026-10-01:
- an App acts as its own bot identity, not as the owner's account;
- its installation tokens expire after one hour, and each can be minted for one repository and a subset of permissions;
- the owner can remove it in one click;
- its permissions are fixed in the manifest beside this file, and `tests/test_github_apps.py` checks them.

## The two Apps

| App | Permissions (exactly) | Who holds its key | Used for |
|---|---|---|---|
| **A. `sfdc24-cloud-clone`** | Contents: **read**. Metadata: read | the `claude-code-cloud` job's service account | The harness clones the named repo before the agent starts, then removes the token and the remote. The agent never sees it. |
| **B. `sfdc24-ccc-broker`** | Contents: **write**. Pull requests: **write**. Metadata: read | the `ccc-broker` service account only | `open_pr`: pushes one owned `claude-code-cloud/<work_id>-*` branch (new or fast-forward) and opens one PR to `main`. |

Neither App is public. Neither has a webhook or subscribes to events. Both are installed on **`sfdc-24/Blackboard`
and `sfdc-24/conference` only**.

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

**Before you start:** be signed in to github.com as `sfdc-24`, with `gcloud` signed in on this laptop.

1. **Create the App:**
   - Open `docs/github-apps/create-apps.html` from this repo on your disk (double-click it).
   - Click **Create sfdc24-cloud-clone**. GitHub shows its "Register new GitHub App" page, filled in.
   - Check the permissions: Contents read and Metadata read, nothing else. Click **Create GitHub App for sfdc-24**.
   - GitHub then opens `https://www.sfdc24.com/?code=...`. Copy the value after `code=` from the address bar.
2. **Store its key, within one hour.** In this repo's folder, run:

       python scripts/github_app_convert.py <the code> sfdc24-cloud-clone

   It checks the new App's name and permissions against the manifest. It puts the private key straight into Secret
   Manager as `GH_APP_CLONE_KEY` and never prints it. It prints only the App id and the install link.
3. **Install the App:**
   - Open the install link it printed (`https://github.com/apps/sfdc24-cloud-clone/installations/new`).
   - Choose **Only select repositories**, pick **Blackboard** and **conference**, and click **Install**.
   - The page you land on ends in `/installations/<number>`. Send that number to Claude; it is not a secret.
4. **Repeat steps 1 to 3** with **Create sfdc24-ccc-broker**. The permissions to check are Contents write, Pull
   requests write and Metadata read. The secret name is `GH_APP_BROKER_KEY`.

**If something goes wrong:**
- **The code expired** (after one hour): open the App's settings page on GitHub, generate a new private key, and ask
  Claude for the one-line store command.
- **The converter STOPPED:** it stored nothing. Nothing about the App needs undoing except deleting it on GitHub if
  you want to start over.
- **To revoke at any time:** github.com > Settings > Applications > the App > Uninstall, and Developer settings >
  the App > Delete. Then delete the secret: `gcloud secrets delete GH_APP_..._KEY --project sfdc24`.

## After the clicks (Claude)
- Grant `secretAccessor` on each key to its one service account, when #306's accounts exist.
- Read back each App's installation (repositories and permissions) with a JWT minted from its key inside the
  service. Post the receipt on the board.
- **Stage C2 does not start** until Codex's #306 review, the least-privilege service-account work (tracked
  privately), and the #306 test 12 negative controls have all passed.
