# ORDER release candidate preflight

`verify_order_release_candidate.ps1` is the offline trust boundary between the
deterministic six-file packager and `build_order_release_wrapper.ps1`. Run it on
the operator machine before archive or installer bytes are embedded in an Azure
Managed Run Command wrapper.

The preflight does not trust the packager receipt by itself. It independently:

- anchors Git to the exact absolute repository supplied by the operator;
- removes inherited `GIT_*` and `GCM_*` context, disables object replacement,
  and never fetches or checks out a ref;
- requires an ordinary in-tree `.git` directory, rejects reparse points in its
  metadata, and rejects replacement refs, grafts, alternate object stores, and
  repository-local external config includes;
- resolves the full lowercase 40-character commit and its tree;
- reads the six canonical release files directly from that commit as blobs;
- reconstructs the packager's canonical ZIP representation and requires the
  candidate archive to match byte for byte, including order and metadata;
- requires the strict, BOM-free packager receipt to match the archive, commit,
  byte count, and all six Git-object SHA-256 values;
- requires the caller-pinned installer digest and proves that installer is the
  exact `infra/azure/install_release_from_archive.ps1` blob from the same
  commit; and
- rejects a reparse point anywhere in an input path.

It only reads local files and Git objects. It does not extract the archive,
write an output file, contact a remote, invoke Azure, inspect or change a VM,
or inspect or change Task Scheduler.

The checkout copy of the installer is not authoritative. In particular, a
Windows checkout with `core.autocrlf=true` can project the committed LF bytes as
CRLF while remaining clean according to `git status`. The preflight rejects
that projection even when the caller supplies its matching local digest. The
cutover must instead stage the installer byte for byte from the pinned Git blob
using a separately reviewed binary-safe, create-new staging step, then pass
that staged file and its independently pinned blob SHA-256 to this command.
The current six-file worker archive does not contain the installer, so a raw
Git-blob staging step remains required. This preflight validates that staged
result but deliberately does not manufacture it.

## Inputs

The packager writes its receipt to standard output. Capture that child
process's standard output directly to a new file so the UTF-8 bytes are
preserved; do not pipe it through Windows PowerShell 5.1 text redirection,
which can transcode native output. The preflight accepts either the exact JSON
bytes or the same bytes followed by the single line ending produced by
`Console.WriteLine`. It rejects a BOM, leading whitespace, multiple records,
and extra or reordered properties.

With an already captured receipt, invoke:

```powershell
$commit = '0123456789abcdef0123456789abcdef01234567'
$installer = 'C:\absolute\staging\install_release_from_archive.ps1' # Raw pinned Git blob, not checkout projection.
$installerSha256 = (Get-FileHash -LiteralPath $installer -Algorithm SHA256).Hash.ToLowerInvariant()

.\infra\azure\verify_order_release_candidate.ps1 `
  -RepositoryPath (Get-Location).Path `
  -CommitId $commit `
  -ArchivePath 'C:\absolute\staging\order-release.zip' `
  -PackagerReceiptPath 'C:\absolute\staging\packager-receipt.json' `
  -InstallerPath $installer `
  -ExpectedInstallerSha256 $installerSha256
```

A successful invocation exits `0`, writes nothing to stderr, and emits exactly
one compact `blackboard.order-release-candidate-preflight-receipt.v1` record.
The receipt has no timestamp or machine path, so the same candidate produces
the same receipt. Require all of the following before calling the wrapper
builder:

- `ok:true`
- `status:"VERIFIED_OFFLINE"`
- the exact caller-pinned `release_id`, `commit_id`, and `installer_sha256`
- the independently resolved `tree_id`
- the exact `archive_sha256`, `archive_bytes`, and six `file_sha256` values
- `git_context_status:"EXPLICIT_REPOSITORY_REPLACEMENTS_DISABLED"`
- `transport_status:"NOT_STARTED"`

Any mismatch exits `1`, leaves stdout empty, and emits one bounded failure
receipt on stderr. A preflight success is authority to build the transport
wrapper only. It is not evidence that guest installation, the installed
manifest, scheduled-task XML, SYSTEM smoke, canary processing, rollback, or
Azure schedule recovery succeeded; those remain separate cutover gates.
