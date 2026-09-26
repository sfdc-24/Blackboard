#Requires -Version 5.1
Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'

$sut = Join-Path $PSScriptRoot '..\scripts\host_resource_governor.ps1'
if (-not (Test-Path -LiteralPath $sut -PathType Leaf)) { throw "cannot find $sut" }
. $sut

$script:Passed = 0
$script:Failed = 0

function Assert-Equal {
    param([string] $Name, $Expected, $Actual)
    if ($Expected -ceq $Actual) {
        $script:Passed++
        Write-Output "PASS $Name"
    } else {
        $script:Failed++
        Write-Output "FAIL $Name expected=[$Expected] actual=[$Actual]"
    }
}

function Assert-True {
    param([string] $Name, [bool] $Condition, [string] $Detail = '')
    if ($Condition) {
        $script:Passed++
        Write-Output "PASS $Name"
    } else {
        $script:Failed++
        Write-Output "FAIL $Name $Detail"
    }
}

function Assert-Throws {
    param([string] $Name, [scriptblock] $Operation, [string] $ExpectedMessage)
    try {
        & $Operation
        $script:Failed++
        Write-Output "FAIL $Name did not throw"
    } catch {
        if ($_.Exception.Message -ceq $ExpectedMessage) {
            $script:Passed++
            Write-Output "PASS $Name"
        } else {
            $script:Failed++
            Write-Output "FAIL $Name expected=[$ExpectedMessage] actual=[$($_.Exception.Message)]"
        }
    }
}

function New-Metrics {
    param([double] $FreeGB, [double] $CpuPercent)
    [pscustomobject]@{ total_gb = 16.0; free_gb = $FreeGB; used_pct = 0; cpu_percent = $CpuPercent }
}

Assert-Equal 'healthy host is GREEN' 'GREEN' `
    (Get-Sfdc24ResourceState -Metrics (New-Metrics 7.3 22))
Assert-Equal 'low headroom is AMBER before Windows thrashes' 'AMBER' `
    (Get-Sfdc24ResourceState -Metrics (New-Metrics 3.9 20))
Assert-Equal 'sustained CPU pressure is AMBER' 'AMBER' `
    (Get-Sfdc24ResourceState -Metrics (New-Metrics 8.0 75))
Assert-Equal 'critical memory is RED' 'RED' `
    (Get-Sfdc24ResourceState -Metrics (New-Metrics 2.49 10))
Assert-Equal 'critical CPU is RED' 'RED' `
    (Get-Sfdc24ResourceState -Metrics (New-Metrics 8.0 90))
Assert-Equal 'invalid threshold ordering fails closed' 'UNKNOWN' `
    (Get-Sfdc24ResourceState -Metrics (New-Metrics 8.0 10) `
        -GreenAtFreeGB 2.5 -RedBelowFreeGB 4.0)
Assert-Equal 'negative free-memory threshold fails closed' 'UNKNOWN' `
    (Get-Sfdc24ResourceState -Metrics (New-Metrics 8.0 10) `
        -RedBelowFreeGB -1)
Assert-Equal 'out-of-range CPU threshold fails closed' 'UNKNOWN' `
    (Get-Sfdc24ResourceState -Metrics (New-Metrics 8.0 10) `
        -RedAtCpuPercent 101)
Assert-Equal 'NaN threshold fails closed' 'UNKNOWN' `
    (Get-Sfdc24ResourceState -Metrics (New-Metrics 8.0 10) `
        -GreenAtFreeGB ([double]::NaN))
Assert-Equal 'infinite threshold fails closed' 'UNKNOWN' `
    (Get-Sfdc24ResourceState -Metrics (New-Metrics 8.0 10) `
        -AmberAtCpuPercent ([double]::PositiveInfinity))
Assert-Equal 'plain native arguments remain unquoted' 'alpha' `
    (ConvertTo-Sfdc24NativeArgument -Value 'alpha')
Assert-Equal 'arguments with spaces are quoted' '"two words"' `
    (ConvertTo-Sfdc24NativeArgument -Value 'two words')
Assert-Equal 'an empty argument remains present' '""' `
    (ConvertTo-Sfdc24NativeArgument -Value '')
Assert-Equal 'trailing path backslashes survive quoting' '"C:\two words\folder\\"' `
    (ConvertTo-Sfdc24NativeArgument -Value 'C:\two words\folder\')
Assert-Equal 'UNC path backslashes survive quoting' '"\\server\share name\leaf\\"' `
    (ConvertTo-Sfdc24NativeArgument -Value '\\server\share name\leaf\')
$jsonArguments = @(ConvertFrom-Sfdc24ArgumentJson -Json '["-m","two words",""]')
Assert-Equal 'argument JSON preserves its array width' 3 $jsonArguments.Count
Assert-Equal 'argument JSON preserves spaces' 'two words' $jsonArguments[1]
Assert-Equal 'argument JSON preserves an empty argument' '' $jsonArguments[2]
$emptyJsonArguments = @(ConvertFrom-Sfdc24ArgumentJson -Json '[]')
Assert-Equal 'empty argument JSON remains an empty list' 0 $emptyJsonArguments.Count
Assert-Throws 'argument JSON rejects an object' {
    ConvertFrom-Sfdc24ArgumentJson -Json '{"not":"an array"}'
} 'HOST_GOVERNOR_ARGUMENT_JSON_NOT_ARRAY'
Assert-Throws 'argument JSON rejects non-string values' {
    ConvertFrom-Sfdc24ArgumentJson -Json '["ok",7]'
} 'HOST_GOVERNOR_ARGUMENT_JSON_NON_STRING'
Assert-Throws 'a lock path cannot escape the private state root' {
    Resolve-Sfdc24StateChildPath -Root 'C:\safe-root' -Candidate 'C:\other\lane.lock'
} 'HOST_GOVERNOR_STATE_PATH_INVALID'

$stateRoot = Join-Path $env:TEMP ('host-governor-test-' + [guid]::NewGuid().ToString('N'))
$lockPath = Join-Path $stateRoot 'heavy-lane.lock'
$first = Enter-Sfdc24HeavyLane -Path $lockPath
try {
    Assert-True 'first caller acquires the heavy lane' ($null -ne $first)
    $second = Enter-Sfdc24HeavyLane -Path $lockPath
    Assert-True 'second caller cannot acquire the heavy lane' ($null -eq $second)
} finally {
    Exit-Sfdc24HeavyLane -Handle $first
}
$third = Enter-Sfdc24HeavyLane -Path $lockPath
try {
    Assert-True 'the heavy lane is reusable after release' ($null -ne $third)
} finally {
    Exit-Sfdc24HeavyLane -Handle $third
}

try {
    $blocked = Invoke-Sfdc24HeavyRun -Metrics (New-Metrics 2.0 5) `
        -LaneOwner codex -Executable 'powershell.exe' `
        -Arguments @('-NoProfile', '-Command', 'exit 0') `
        -LaneLockPath (Join-Path $stateRoot 'red.lock') -LaneStateRoot $stateRoot
    Assert-Equal 'RED pressure refuses before launching a process' 20 $blocked
    Assert-True 'a refused run writes no owner metadata' `
        (-not (Test-Path -LiteralPath (Join-Path $stateRoot 'heavy-lane.json')))

    $busyLock = Join-Path $stateRoot 'busy.lock'
    $held = Enter-Sfdc24HeavyLane -Path $busyLock
    try {
        $busy = Invoke-Sfdc24HeavyRun -Metrics (New-Metrics 8.0 5) `
            -LaneOwner claude -Executable 'powershell.exe' `
            -Arguments @('-NoProfile', '-Command', 'exit 0') `
            -LaneLockPath $busyLock -LaneStateRoot $stateRoot
        Assert-Equal 'a second heavy workload is refused' 21 $busy
    } finally {
        Exit-Sfdc24HeavyLane -Handle $held
    }

    $childScript = Join-Path $stateRoot 'exit-seven.cmd'
    [IO.File]::WriteAllText($childScript, "@ping -n 2 127.0.0.1 >nul`r`n@exit /b 7`r`n")
    $childCode = Invoke-Sfdc24HeavyRun -Metrics (New-Metrics 8.0 5) `
        -LaneOwner grok -Executable $env:ComSpec `
        -Arguments @('/d', '/c', $childScript) `
        -LaneLockPath (Join-Path $stateRoot 'child.lock') -LaneStateRoot $stateRoot
    Assert-Equal 'an accepted run returns the child exit code' 7 $childCode
    Assert-True 'metadata is removed after the child exits' `
        (-not (Test-Path -LiteralPath (Join-Path $stateRoot 'heavy-lane.json')))

    $cleanupLock = Join-Path $stateRoot 'cleanup-failure.lock'
    $cleanupObservations = @(Invoke-Sfdc24HeavyRun `
        -Metrics (New-Metrics 8.0 5) -LaneOwner codex `
        -Executable $env:ComSpec -Arguments @('/d', '/c', 'exit 0') `
        -LaneLockPath $cleanupLock -LaneStateRoot $stateRoot `
        -MetadataRemover { param($Root) throw 'TEST_CLEANUP_FAILURE' } 3>&1 6>&1)
    $cleanupCodes = @($cleanupObservations | Where-Object { $_ -is [int] })
    Assert-Equal 'metadata cleanup emits exactly one child verdict' 1 $cleanupCodes.Count
    $cleanupCode = [int] $cleanupCodes[0]
    Assert-Equal 'metadata cleanup failure preserves the child verdict' 0 $cleanupCode
    Assert-True 'metadata cleanup failure is observable' `
        (@($cleanupObservations | Where-Object {
            $_.ToString() -match 'LANE_METADATA_CLEANUP_FAILED'
        }).Count -eq 1)
    $afterCleanupFailure = Enter-Sfdc24HeavyLane -Path $cleanupLock
    try {
        Assert-True 'metadata cleanup failure still releases the lane' `
            ($null -ne $afterCleanupFailure)
    } finally {
        Exit-Sfdc24HeavyLane -Handle $afterCleanupFailure
    }
    Remove-Sfdc24LaneMetadata -Root $stateRoot

    $priorityObservations = @(Invoke-Sfdc24HeavyRun `
        -Metrics (New-Metrics 8.0 5) -LaneOwner claude `
        -Executable $env:ComSpec -Arguments @('/d', '/c', 'exit 0') `
        -LaneLockPath (Join-Path $stateRoot 'priority.lock') `
        -LaneStateRoot $stateRoot `
        -PriorityApplier { param($Process, $Target) return $false } 3>&1 6>&1)
    $priorityCodes = @($priorityObservations | Where-Object { $_ -is [int] })
    Assert-Equal 'priority refusal emits exactly one child verdict' 1 $priorityCodes.Count
    $priorityCode = [int] $priorityCodes[0]
    Assert-Equal 'priority refusal does not discard successful work' 0 $priorityCode
    Assert-True 'priority refusal is reported in the STARTED receipt' `
        (@($priorityObservations | Where-Object {
            $_.ToString() -match 'priority_target=BelowNormal priority_applied=false'
        }).Count -eq 1)
    Assert-True 'priority refusal emits one warning' `
        (@($priorityObservations | Where-Object {
            $_.ToString() -match 'CHILD_PRIORITY_NOT_APPLIED'
        }).Count -eq 1)
} finally {
    if (Test-Path -LiteralPath $stateRoot -PathType Container) {
        Remove-Item -LiteralPath $stateRoot -Recurse -Force
    }
}

Write-Output ("RESULT passed={0} failed={1}" -f $script:Passed, $script:Failed)
if ($script:Failed -ne 0 -or $script:Passed -lt 1) { exit 1 }
exit 0
