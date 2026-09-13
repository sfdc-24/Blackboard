# Windows toolchain gotchas

Tags: windows, ssh, scp, powershell, git, msys

## This laptop's Windows OpenSSH has NO ssh.exe

`C:\Windows\System32\OpenSSH` ships `scp.exe`, `sshd.exe`, `ssh-keygen.exe`,
`ssh-add.exe`, `ssh-agent.exe` and `sftp-server.exe` — **but not the client.**

Consequences:

- `ssh` is not a command in PowerShell at all.
- `scp` resolves fine from PATH and then dies with

      CreateProcessW failed error:2
      posix_spawn: No such file or directory

  which reads like a missing FILE and is actually **scp failing to spawn its own
  sibling transport**.

Git Bash works only because Git ships its own pair at
`C:\Program Files\Git\usr\bin`.

**Resolve ssh and scp as a PAIR from one directory**, never independently. A
`scp` found on PATH next to a missing `ssh` is worse than no scp at all: it
resolves, it runs, and it fails misleadingly. A first fix that preferred
`Get-Command` found System32's scp and failed exactly that way *after* paying for
an API call.

    foreach ($d in @("C:\Program Files\Git\usr\bin", "C:\Windows\System32\OpenSSH")) {
      if ((Test-Path "$d\ssh.exe") -and (Test-Path "$d\scp.exe")) { $pair = $d; break }
    }

Source: commits `1d286db`, `2d0512a`.

## Git Bash rewrites POSIX paths in remote commands (MSYS translation)

`ssh HOST 'cat > /tmp/x'` becomes `cat > C:/Program Files/Git/tmp/x` **on the
remote**, and the file lands nowhere useful. Set `MSYS_NO_PATHCONV=1`.

Related: quoting through `wsl -- bash -c "...$var..."` is expanded by the OUTER
Windows shell first. Put everything in a file and invoke the file.

## core.fileMode is false, so chmod +x never reaches the index

`chmod +x` on Windows leaves the file staged as `100644`. CI that asserts
committed executable bits will fail, and at run time the script gives EACCES —
which one caller turned into "Zoom is not listening".

    git update-index --chmod=+x <path>

Verify with `git ls-files -s`, not with `ls -l`.

## PowerShell strict mode throws on a missing property

`$job.ChildJobs[0].JobStateInfo.Reason.Message` where `Reason` is null is a
TERMINATING error under `Set-StrictMode -Version 2.0`. A diagnostic that was
supposed to explain a failure crashed while explaining it and printed
`PropertyNotFoundStrict` instead of the cause.

**An error path that can itself error is an error path nobody has run.**

Also: `$IsWindows` does not exist in Windows PowerShell 5.1 and reading it under
strict mode throws — use `(Test-Path Variable:IsWindows)`.

Source: commit `959c28a`.
