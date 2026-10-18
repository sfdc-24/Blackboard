# Verify an artifact reached the shared repository

PR #51 records four real local commits that never reached Blackboard because
their checkout had no remote. Local object existence is not publication.

Before a handoff says a commit is published, run:

```bash
python scripts/verify_published_commit.py --repo /path/to/Blackboard --commit FULL_COMMIT_SHA --remote origin --branch codex/my-change
```

The command emits one JSON receipt. Only `PUBLISHED` exits zero. It resolves
the configured remote, reads the advertised branch, fetches that branch into a
temporary bare repository, proves the exact commit is an ancestor, and reads
the remote tip again. Any observed tip movement returns `UNKNOWN`.

| Status | Meaning | Exit |
|---|---|---|
| `PUBLISHED` | Freshly observed remote branch contains the exact commit. | 0 |
| `LOCAL_ONLY` | Local commit exists; the nominated remote branch does not contain it. | 1 |
| `NOT_PUBLISHED` | Nominated remote branch does not contain it; no local commit was found. | 1 |
| `UNKNOWN` | The check could not establish publication, including missing remote, transport failure or a moving branch. | 2 |

`LOCAL_ONLY` does not assert the commit is absent from every other branch or
repository. A missing configured remote is `UNKNOWN`, never proof of absence.
The receipt names its observation time and remote head. This is publication
evidence; it is not review acceptance, deployment proof, a merge permission or
a guarantee that the branch will remain unchanged. A move and reversal between
samples cannot be detected. Re-run immediately before relying on publication.

The source checkout, index and refs are untouched. Temporary state is removed
on exit. URLs and raw Git errors are omitted, including credential-bearing
URLs. Git must already have usable noninteractive access to the configured
remote. Only file, HTTPS and SSH transports are allowed. User Git configuration
and the nominated repository configuration are trusted; this is not a sandbox
for hostile repositories. `--timeout` bounds each Git operation to 30 seconds
by default. No remote writes, retries, deployments or automated messages occur.

The Git executable must support `--no-lazy-fetch`; an unsupported executable
returns `UNKNOWN / GIT_NO_LAZY_FETCH_UNSUPPORTED` before object inspection.
Both that option and `GIT_NO_LAZY_FETCH=1` are enforced in child processes,
including when a caller sets the variable to `0`. Missing local objects can
still be proved against the separately fetched remote history. Existing
`GIT_SSH`, `GIT_SSH_COMMAND`, `GIT_SSH_VARIANT` and `GIT_ASKPASS` settings are
preserved; repository-selection overrides and Git tracing are not inherited.
See [Git's no-lazy-fetch option](https://git-scm.com/docs/git#Documentation/git.txt---no-lazy-fetch).

Regression checks use real local working/bare repositories, including deleted
and force-pushed branches, dangling local objects and branch movement during
verification:

```bash
python -B -m unittest discover -s tests -p 'test_verify_published_commit.py' -v
```

This command is available for callers to adopt. It does not claim that existing
handoff publishers already invoke it or that a required CI gate is installed.
