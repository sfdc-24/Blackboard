# Host resource governor

This is a mechanism reference, not another boot document. The compact agent
operating surface may link here; agents do not need to read it on every wake.

## Decision

The laptop has one heavy local execution lane shared by Claude Code, Codex and
Grok. Claude Code is the host scheduler because its PowerShell process can see
the Windows process table directly. Codex supplies policy, independent review
and focused work; it does not start parallel local agents while Claude owns the
lane. Grok is cloud-first and uses the laptop only for a focused reproduction.

Full suites run on hosted CI. Runtime probes run on staging or Cloud Run.
Provider analysis runs in the provider console/API. The local lane is reserved
for focused tests, browser acceptance and release control that genuinely need
this machine.

## Pressure states

| State | Measured condition | New heavy local work |
|---|---|---|
| GREEN | at least 4 GB free and CPU below 75% | one owner may enter the lane |
| AMBER | 2.5-4 GB free or CPU at least 75% | refuse; use hosted CI/cloud |
| RED | less than 2.5 GB free or CPU at least 90% | refuse; current owner checkpoints at the next safe boundary |
| UNKNOWN | metrics cannot be read or thresholds are invalid | fail closed |

The script does not kill a process, suspend an interactive agent, set a global
environment variable, or impose a hard RAM cap. Those actions can corrupt work
or make an agent disappear without a handoff. Immediately after an accepted
child starts, the governor requests `BelowNormal` priority. Windows can reject
that best-effort request or a very short-lived child can finish first, so the
`STARTED` receipt reports both `priority_target` and `priority_applied`. The
pressure gate and exclusive lane are the guarantees; process priority is an
observable optimization. The operating system still decides memory allocation.

## Use

Read the current state without changing anything:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\host_resource_governor.ps1 -Action Status
```

Run one focused heavy command under the shared lane:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass `
  -File scripts\host_resource_governor.ps1 `
  -Action Run -Owner claude `
  -FilePath python.exe `
  -ArgumentListJson '["-m","unittest","tests.test_studio_auth"]'
```

`ArgumentListJson` is a JSON array of strings so Windows PowerShell 5.1 does
not silently collapse or rebind a multi-value `-File` argument. Do not put a
credential in that JSON; supply secrets through the existing environment or
provider vault, exactly as for an unwrapped command. Direct callers may use the
typed `-ArgumentList` array, but must not provide both forms.

Exit codes are contractual: `20` pressure refusal, `21` lane busy, `22` child
launch failure, `23` unavailable metrics, and `24` invalid run request. A
refusal is routing information, not a test failure: send the work to hosted CI
or the relevant cloud environment instead of retrying locally.

The cross-process authority is an exclusively opened local file,
`%LOCALAPPDATA%\SFDC24\host-resource-governor\heavy-lane.lock`. A stale empty
file is harmless: Windows releases the exclusive handle when its process ends.
While a child runs, the governor writes only owner, PID, start time and
executable basename to the neighbouring `heavy-lane.json`. Command arguments
are never persisted because they may contain credentials.

Metadata is advisory; the exclusive file handle is authoritative. If metadata
cleanup fails, the governor emits `LANE_METADATA_CLEANUP_FAILED` and still
releases the lane in a nested `finally` block so the host cannot deadlock.

## Boundaries

- This gates new work launched through the wrapper; it does not retroactively
  control an already-running interactive session.
- A clean exit code proves the wrapped process ended cleanly, not that a deploy,
  provider call or user experience succeeded. Use the normal destination
  read-back and acceptance evidence.
- The exclusive lock is host-local. Hosted CI and cloud workloads do not consume it.
- If an agent bypasses the wrapper, the governance mechanism cannot serialize
  that work. The compact ways-of-working document should make the wrapper the
  only approved local entry point for a heavy command.
