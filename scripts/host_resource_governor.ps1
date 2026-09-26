#Requires -Version 5.1
<#
SFDC24 local-host resource governor.

This is deliberately a gate for NEW heavy local work, not a task killer and
not a hard memory limiter.  It keeps one cross-agent heavy lane, refuses to
start that lane under memory/CPU pressure, and immediately requests a lower
priority for accepted work so the interactive agents and Windows stay
responsive.  The STARTED receipt says whether Windows applied that request.

The laptop remains useful while the cloud does the bulk work:
  - full suites belong on hosted CI;
  - runtime probes belong on staging/Cloud Run;
  - the local heavy lane is for the smallest focused reproduction that needs
    this machine.

No command arguments are written to disk because they can contain credentials.
#>
[CmdletBinding()]
param(
    [ValidateSet('Status', 'Run')]
    [string] $Action = 'Status',

    [ValidatePattern('^[A-Za-z0-9._-]{1,40}$')]
    [string] $Owner = 'operator',

    [string] $FilePath,
    [string[]] $ArgumentList = @(),
    [string] $ArgumentListJson,

    [ValidateRange(0.5, 64.0)]
    [double] $GreenFreeGB = 4.0,

    [ValidateRange(0.5, 64.0)]
    [double] $RedFreeGB = 2.5,

    [ValidateRange(1.0, 100.0)]
    [double] $AmberCpuPercent = 75.0,

    [ValidateRange(1.0, 100.0)]
    [double] $RedCpuPercent = 90.0,

    [ValidateSet('BelowNormal', 'Idle')]
    [string] $PriorityClass = 'BelowNormal',

    [string] $StateRoot = (Join-Path $env:LOCALAPPDATA 'SFDC24\host-resource-governor'),

    [string] $LockPath = (Join-Path $env:LOCALAPPDATA 'SFDC24\host-resource-governor\heavy-lane.lock')
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'

function Get-Sfdc24HostMetrics {
    [CmdletBinding()]
    param()

    $os = Get-CimInstance Win32_OperatingSystem
    $cpuValues = @(Get-CimInstance Win32_Processor |
        ForEach-Object { if ($null -ne $_.LoadPercentage) { [double] $_.LoadPercentage } })
    if ($null -eq $os -or $cpuValues.Count -eq 0) {
        throw 'HOST_METRICS_UNAVAILABLE'
    }

    $totalGB = [double] $os.TotalVisibleMemorySize / 1MB
    $freeGB = [double] $os.FreePhysicalMemory / 1MB
    $cpuPercent = [double] (($cpuValues | Measure-Object -Average).Average)
    [pscustomobject][ordered]@{
        total_gb   = [math]::Round($totalGB, 2)
        free_gb    = [math]::Round($freeGB, 2)
        used_pct   = [math]::Round((1.0 - ($freeGB / $totalGB)) * 100.0, 1)
        cpu_percent = [math]::Round($cpuPercent, 1)
    }
}

function Get-Sfdc24ResourceState {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)] $Metrics,
        [double] $GreenAtFreeGB = 4.0,
        [double] $RedBelowFreeGB = 2.5,
        [double] $AmberAtCpuPercent = 75.0,
        [double] $RedAtCpuPercent = 90.0
    )

    try {
        $free = [double] $Metrics.free_gb
        $cpu = [double] $Metrics.cpu_percent
    } catch {
        return 'UNKNOWN'
    }
    $thresholds = @(
        $GreenAtFreeGB,
        $RedBelowFreeGB,
        $AmberAtCpuPercent,
        $RedAtCpuPercent
    )
    foreach ($threshold in $thresholds) {
        if ([double]::IsNaN($threshold) -or [double]::IsInfinity($threshold)) {
            return 'UNKNOWN'
        }
    }
    if ([double]::IsNaN($free) -or [double]::IsInfinity($free) -or
        [double]::IsNaN($cpu) -or [double]::IsInfinity($cpu) -or
        $free -lt 0 -or $cpu -lt 0 -or $cpu -gt 100 -or
        $GreenAtFreeGB -lt 0.5 -or $GreenAtFreeGB -gt 64.0 -or
        $RedBelowFreeGB -lt 0.5 -or $RedBelowFreeGB -gt 64.0 -or
        $AmberAtCpuPercent -lt 1.0 -or $AmberAtCpuPercent -gt 100.0 -or
        $RedAtCpuPercent -lt 1.0 -or $RedAtCpuPercent -gt 100.0 -or
        $RedBelowFreeGB -ge $GreenAtFreeGB -or
        $AmberAtCpuPercent -ge $RedAtCpuPercent) {
        return 'UNKNOWN'
    }
    if ($free -lt $RedBelowFreeGB -or $cpu -ge $RedAtCpuPercent) {
        return 'RED'
    }
    if ($free -lt $GreenAtFreeGB -or $cpu -ge $AmberAtCpuPercent) {
        return 'AMBER'
    }
    return 'GREEN'
}

function Enter-Sfdc24HeavyLane {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][string] $Path)

    $full = [IO.Path]::GetFullPath($Path)
    [IO.Directory]::CreateDirectory([IO.Path]::GetDirectoryName($full)) | Out-Null
    try {
        # FileShare.None is the authority. The zero-byte file may remain after a
        # crash, but its handle is released by Windows, so a stale pathname can
        # never keep the lane blocked or be mistaken for an active owner.
        return ,[IO.File]::Open(
            $full,
            [IO.FileMode]::OpenOrCreate,
            [IO.FileAccess]::ReadWrite,
            [IO.FileShare]::None
        )
    } catch [IO.IOException] {
        return $null
    }
}

function Exit-Sfdc24HeavyLane {
    [CmdletBinding()]
    param([AllowNull()] $Handle)

    if ($null -eq $Handle) { return }
    $Handle.Dispose()
}

function Test-Sfdc24HeavyLaneBusy {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][string] $Path)

    $handle = Enter-Sfdc24HeavyLane -Path $Path
    if ($null -eq $handle) { return $true }
    Exit-Sfdc24HeavyLane -Handle $handle
    return $false
}

function Get-Sfdc24MetadataPath {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][string] $Root)

    $rootFull = [IO.Path]::GetFullPath($Root)
    $path = Resolve-Sfdc24StateChildPath -Root $rootFull `
        -Candidate (Join-Path $rootFull 'heavy-lane.json')
    return $path
}

function Resolve-Sfdc24StateChildPath {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string] $Root,
        [Parameter(Mandatory = $true)][string] $Candidate
    )

    $rootFull = [IO.Path]::GetFullPath($Root)
    $path = [IO.Path]::GetFullPath($Candidate)
    $prefix = $rootFull.TrimEnd('\', '/') + [IO.Path]::DirectorySeparatorChar
    if (-not $path.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)) {
        throw 'HOST_GOVERNOR_STATE_PATH_INVALID'
    }
    return $path
}

function Write-Sfdc24LaneMetadata {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string] $Root,
        [Parameter(Mandatory = $true)][string] $LaneOwner,
        [Parameter(Mandatory = $true)][string] $Executable
    )

    [IO.Directory]::CreateDirectory([IO.Path]::GetFullPath($Root)) | Out-Null
    $path = Get-Sfdc24MetadataPath -Root $Root
    $record = [ordered]@{
        schema      = 'sfdc24.host-heavy-lane.v1'
        owner       = $LaneOwner
        pid         = $PID
        started_utc = [DateTime]::UtcNow.ToString('o')
        executable  = [IO.Path]::GetFileName($Executable)
    }
    [IO.File]::WriteAllText(
        $path,
        ($record | ConvertTo-Json -Compress),
        (New-Object Text.UTF8Encoding($false))
    )
    return $path
}

function Remove-Sfdc24LaneMetadata {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][string] $Root)

    $path = Get-Sfdc24MetadataPath -Root $Root
    if (Test-Path -LiteralPath $path -PathType Leaf) {
        Remove-Item -LiteralPath $path -Force
    }
}

function ConvertTo-Sfdc24NativeArgument {
    [CmdletBinding()]
    param([AllowEmptyString()][Parameter(Mandatory = $true)][string] $Value)

    # ProcessStartInfo.ArgumentList does not exist on Windows PowerShell 5.1's
    # .NET Framework. Build one CommandLineToArgvW-compatible argument without
    # invoking a shell. This preserves spaces, quotes and trailing backslashes.
    if ($Value.Length -gt 0 -and $Value -notmatch '[\s"]') { return $Value }
    $builder = New-Object Text.StringBuilder
    $null = $builder.Append('"')
    $slashes = 0
    foreach ($character in $Value.ToCharArray()) {
        if ($character -eq '\') {
            $slashes++
            continue
        }
        if ($character -eq '"') {
            $null = $builder.Append((('\' * (($slashes * 2) + 1)) -join ''))
            $null = $builder.Append('"')
            $slashes = 0
            continue
        }
        if ($slashes -gt 0) {
            $null = $builder.Append((('\' * $slashes) -join ''))
            $slashes = 0
        }
        $null = $builder.Append($character)
    }
    if ($slashes -gt 0) {
        $null = $builder.Append((('\' * ($slashes * 2)) -join ''))
    }
    $null = $builder.Append('"')
    return $builder.ToString()
}

function Set-Sfdc24ChildPriority {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)] $Process,
        [Parameter(Mandatory = $true)][string] $Target
    )

    try {
        $Process.PriorityClass = [Diagnostics.ProcessPriorityClass] $Target
        return $true
    } catch {
        # Very short-lived children may exit before Windows accepts the
        # priority change. The caller reports this explicitly and keeps the
        # pressure and exclusive-lane guarantees in force.
        return $false
    }
}

function ConvertFrom-Sfdc24ArgumentJson {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][string] $Json)

    if (-not $Json.TrimStart().StartsWith('[')) {
        throw 'HOST_GOVERNOR_ARGUMENT_JSON_NOT_ARRAY'
    }
    try {
        Add-Type -AssemblyName System.Runtime.Serialization
        $bytes = [Text.Encoding]::UTF8.GetBytes($Json)
        $stream = New-Object IO.MemoryStream(,$bytes)
        try {
            $reader = [Runtime.Serialization.Json.DataContractJsonSerializer]::new([object[]])
            $parsed = $reader.ReadObject($stream)
        } finally {
            $stream.Dispose()
        }
    } catch [Runtime.Serialization.SerializationException] {
        throw 'HOST_GOVERNOR_ARGUMENT_JSON_INVALID'
    } catch {
        throw 'HOST_GOVERNOR_ARGUMENT_JSON_INVALID'
    }
    $values = @()
    foreach ($item in $parsed) {
        if ($null -eq $item -or $item -isnot [string]) {
            throw 'HOST_GOVERNOR_ARGUMENT_JSON_NON_STRING'
        }
        $values += [string] $item
    }
    # Emit the strings to the pipeline. Callers wrap the invocation in @() so
    # an empty, one-item, or many-item JSON array keeps the same width.
    return $values
}

function Invoke-Sfdc24HeavyRun {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)] $Metrics,
        [Parameter(Mandatory = $true)][string] $LaneOwner,
        [Parameter(Mandatory = $true)][string] $Executable,
        [string[]] $Arguments = @(),
        [Parameter(Mandatory = $true)][string] $LaneLockPath,
        [Parameter(Mandatory = $true)][string] $LaneStateRoot,
        [double] $GreenAtFreeGB = 4.0,
        [double] $RedBelowFreeGB = 2.5,
        [double] $AmberAtCpuPercent = 75.0,
        [double] $RedAtCpuPercent = 90.0,
        [string] $ChildPriorityClass = 'BelowNormal',
        [scriptblock] $PriorityApplier,
        [scriptblock] $MetadataRemover
    )

    $state = Get-Sfdc24ResourceState -Metrics $Metrics `
        -GreenAtFreeGB $GreenAtFreeGB -RedBelowFreeGB $RedBelowFreeGB `
        -AmberAtCpuPercent $AmberAtCpuPercent -RedAtCpuPercent $RedAtCpuPercent
    if ($state -ne 'GREEN') {
        Write-Host "REFUSED state=$state reason=use_hosted_ci_or_cloud"
        return 20
    }

    $lane = Enter-Sfdc24HeavyLane -Path $LaneLockPath
    if ($null -eq $lane) {
        Write-Host 'REFUSED state=BUSY reason=one_local_heavy_lane'
        return 21
    }

    $metadataWritten = $false
    try {
        $null = Write-Sfdc24LaneMetadata -Root $LaneStateRoot `
            -LaneOwner $LaneOwner -Executable $Executable
        $metadataWritten = $true

        $startInfo = New-Object Diagnostics.ProcessStartInfo
        $startInfo.FileName = $Executable
        $startInfo.UseShellExecute = $false
        $startInfo.CreateNoWindow = $false
        $startInfo.WorkingDirectory = (Get-Location).Path
        $startInfo.Arguments = (@($Arguments | ForEach-Object {
            ConvertTo-Sfdc24NativeArgument -Value ([string] $_)
        }) -join ' ')
        $child = New-Object Diagnostics.Process
        $child.StartInfo = $startInfo
        if (-not $child.Start()) { throw 'CHILD_START_RETURNED_FALSE' }
        if ($null -eq $PriorityApplier) {
            $priorityApplied = Set-Sfdc24ChildPriority -Process $child `
                -Target $ChildPriorityClass
        } else {
            try {
                $priorityApplied = [bool] (& $PriorityApplier $child $ChildPriorityClass)
            } catch {
                $priorityApplied = $false
            }
        }
        if (-not $priorityApplied) {
            Write-Warning 'CHILD_PRIORITY_NOT_APPLIED'
        }
        Write-Host ("STARTED owner={0} pid={1} priority_target={2} priority_applied={3}" -f
            $LaneOwner, $child.Id, $ChildPriorityClass,
            ([string] $priorityApplied).ToLowerInvariant())
        $child.WaitForExit()
        # Windows PowerShell can retain the default ExitCode value after a
        # very short-lived child exits before the priority assignment. Refresh
        # after WaitForExit so the wrapper returns the child's real verdict.
        $child.Refresh()
        return [int] $child.ExitCode
    } catch {
        Write-Host ("FAILED reason={0}" -f $_.Exception.GetType().Name)
        return 22
    } finally {
        try {
            if ($metadataWritten) {
                try {
                    if ($null -eq $MetadataRemover) {
                        Remove-Sfdc24LaneMetadata -Root $LaneStateRoot
                    } else {
                        & $MetadataRemover $LaneStateRoot
                    }
                } catch {
                    # A stale metadata file is advisory and may be overwritten
                    # by the next owner. It must never retain the authoritative
                    # exclusive lock handle.
                    Write-Warning 'LANE_METADATA_CLEANUP_FAILED'
                }
            }
        } finally {
            Exit-Sfdc24HeavyLane -Handle $lane
        }
    }
}

function Invoke-Sfdc24HostResourceGovernor {
    [CmdletBinding()]
    param()

    try {
        $safeLockPath = Resolve-Sfdc24StateChildPath -Root $StateRoot -Candidate $LockPath
    } catch {
        if ($Action -eq 'Run') {
            Write-Host 'REFUSED state=INVALID reason=lock_path_outside_state_root'
            return [pscustomobject]@{ ExitCode = 24; Output = $null }
        }
        $json = [pscustomobject][ordered]@{
            schema = 'sfdc24.host-resource-status.v1'
            state = 'UNKNOWN'
            heavy_lane = 'UNKNOWN'
            reason = 'lock_path_outside_state_root'
        } | ConvertTo-Json -Compress
        return [pscustomobject]@{ ExitCode = 0; Output = $json }
    }

    $metrics = $null
    try { $metrics = Get-Sfdc24HostMetrics } catch {
        if ($Action -eq 'Run') {
            Write-Host 'REFUSED state=UNKNOWN reason=host_metrics_unavailable'
            return [pscustomobject]@{ ExitCode = 23; Output = $null }
        }
        $json = [pscustomobject][ordered]@{
            schema = 'sfdc24.host-resource-status.v1'
            state = 'UNKNOWN'
            heavy_lane = 'UNKNOWN'
            reason = 'host_metrics_unavailable'
        } | ConvertTo-Json -Compress
        return [pscustomobject]@{ ExitCode = 0; Output = $json }
    }

    $state = Get-Sfdc24ResourceState -Metrics $metrics `
        -GreenAtFreeGB $GreenFreeGB -RedBelowFreeGB $RedFreeGB `
        -AmberAtCpuPercent $AmberCpuPercent -RedAtCpuPercent $RedCpuPercent

    if ($Action -eq 'Status') {
        $json = [pscustomobject][ordered]@{
            schema = 'sfdc24.host-resource-status.v1'
            state = $state
            heavy_lane = $(if (Test-Sfdc24HeavyLaneBusy -Path $safeLockPath) { 'BUSY' } else { 'FREE' })
            total_gb = $metrics.total_gb
            free_gb = $metrics.free_gb
            used_pct = $metrics.used_pct
            cpu_percent = $metrics.cpu_percent
            policy = 'one-heavy-local-lane; cloud-first; no-kill; no-hard-ram-cap'
        } | ConvertTo-Json -Compress
        return [pscustomobject]@{ ExitCode = 0; Output = $json }
    }

    if ([string]::IsNullOrWhiteSpace($FilePath)) {
        Write-Host 'REFUSED state=INVALID reason=FilePath_required_for_Run'
        return [pscustomobject]@{ ExitCode = 24; Output = $null }
    }
    $runArguments = @($ArgumentList)
    if (-not [string]::IsNullOrWhiteSpace($ArgumentListJson)) {
        if ($runArguments.Count -gt 0) {
            Write-Host 'REFUSED state=INVALID reason=choose_ArgumentList_or_ArgumentListJson'
            return [pscustomobject]@{ ExitCode = 24; Output = $null }
        }
        try { $runArguments = @(ConvertFrom-Sfdc24ArgumentJson -Json $ArgumentListJson) } catch {
            Write-Host ("REFUSED state=INVALID reason={0}" -f $_.Exception.Message)
            return [pscustomobject]@{ ExitCode = 24; Output = $null }
        }
    }
    $code = Invoke-Sfdc24HeavyRun -Metrics $metrics -LaneOwner $Owner `
        -Executable $FilePath -Arguments $runArguments -LaneLockPath $safeLockPath `
        -LaneStateRoot $StateRoot -GreenAtFreeGB $GreenFreeGB `
        -RedBelowFreeGB $RedFreeGB -AmberAtCpuPercent $AmberCpuPercent `
        -RedAtCpuPercent $RedCpuPercent -ChildPriorityClass $PriorityClass
    return [pscustomobject]@{ ExitCode = [int] $code; Output = $null }
}

if ($MyInvocation.InvocationName -ne '.') {
    $result = Invoke-Sfdc24HostResourceGovernor
    if ($null -ne $result.Output) { Write-Output $result.Output }
    exit ([int] $result.ExitCode)
}
