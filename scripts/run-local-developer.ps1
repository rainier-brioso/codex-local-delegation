[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$Repository,
    [Parameter(Mandatory)][string]$HandoffPath,
    [Parameter(Mandatory)][ValidateNotNullOrEmpty()][string[]]$AllowedPath,
    [string[]]$ProtectedPath = @(),
    [string[]]$TestCommand = @(),
    [ValidateRange(1, 1440)][int]$TimeoutMinutes = 60,
    [ValidateRange(0, 1440)][int]$InactivityTimeoutMinutes = 15,
    [string]$StateRoot,
    [string]$CodexBin
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'lib/LocalDelegation.Common.ps1')

function Invoke-LdNative {
    param(
        [Parameter(Mandatory)][string]$FileName,
        [Parameter(Mandatory)][string[]]$ArgumentList,
        [string]$WorkingDirectory
    )

    $start = [Diagnostics.ProcessStartInfo]::new()
    $start.FileName = $FileName
    $start.UseShellExecute = $false
    $start.RedirectStandardOutput = $true
    $start.RedirectStandardError = $true
    if (-not [string]::IsNullOrWhiteSpace($WorkingDirectory)) {
        $start.WorkingDirectory = $WorkingDirectory
    }
    foreach ($argument in $ArgumentList) { [void]$start.ArgumentList.Add($argument) }
    $process = [Diagnostics.Process]::new()
    $process.StartInfo = $start
    [void]$process.Start()
    $stdout = $process.StandardOutput.ReadToEndAsync()
    $stderr = $process.StandardError.ReadToEndAsync()
    $process.WaitForExit()
    $result = [pscustomobject]@{
        ExitCode = $process.ExitCode
        Stdout = $stdout.GetAwaiter().GetResult()
        Stderr = $stderr.GetAwaiter().GetResult()
    }
    $process.Dispose()
    if ($result.ExitCode -ne 0) {
        throw "$FileName failed with exit code $($result.ExitCode): $($result.Stderr.Trim())"
    }
    return $result.Stdout
}

function Invoke-LdGit {
    param(
        [Parameter(Mandatory)][string]$RepositoryRoot,
        [Parameter(Mandatory)][string[]]$Arguments
    )
    return Invoke-LdNative -FileName 'git' -ArgumentList (@('-C', $RepositoryRoot) + $Arguments)
}

function ConvertTo-LdRelativePath {
    param([Parameter(Mandatory)][string]$Path)

    if ([IO.Path]::IsPathRooted($Path)) { throw "Path must be repository-relative: $Path" }
    $normal = $Path.Replace('\', '/').Trim('/')
    if ([string]::IsNullOrWhiteSpace($normal) -or $normal -eq '.') { return '.' }
    $segments = $normal.Split('/')
    if ($segments -contains '..' -or $segments -contains '.') {
        throw "Path contains a traversal segment: $Path"
    }
    return $normal
}

function Assert-LdPathInsideRepository {
    param(
        [Parameter(Mandatory)][string]$RepositoryRoot,
        [Parameter(Mandatory)][string]$RelativePath
    )

    $rootWithSeparator = $RepositoryRoot.TrimEnd([IO.Path]::DirectorySeparatorChar, [IO.Path]::AltDirectorySeparatorChar) + [IO.Path]::DirectorySeparatorChar
    $candidate = if ($RelativePath -eq '.') { $RepositoryRoot } else { [IO.Path]::GetFullPath((Join-Path $RepositoryRoot $RelativePath)) }
    if ($candidate -ne $RepositoryRoot -and -not $candidate.StartsWith($rootWithSeparator, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Resolved path escapes the repository: $RelativePath"
    }

    $cursor = $candidate
    while ($cursor.StartsWith($rootWithSeparator, [StringComparison]::OrdinalIgnoreCase)) {
        if (Test-Path -LiteralPath $cursor) {
            $item = Get-Item -Force -LiteralPath $cursor
            if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
                throw "Allowed or protected path traverses a symlink or junction: $RelativePath"
            }
        }
        $parent = Split-Path -Parent $cursor
        if ($parent -eq $cursor -or $parent.Length -lt $RepositoryRoot.Length) { break }
        $cursor = $parent
    }
    return $candidate
}

function Test-LdPathPrefix {
    param(
        [Parameter(Mandatory)][string]$Path,
        [Parameter(Mandatory)][string]$Prefix
    )
    return $Prefix -eq '.' -or $Path -eq $Prefix -or $Path.StartsWith("$Prefix/", [StringComparison]::OrdinalIgnoreCase)
}

function Get-LdWorkspaceInventory {
    param([Parameter(Mandatory)][string]$RepositoryRoot)

    $rawPaths = Invoke-LdGit -RepositoryRoot $RepositoryRoot -Arguments @('ls-files', '-z', '--cached', '--others', '--exclude-standard')
    $paths = @($rawPaths.Split([char]0, [StringSplitOptions]::RemoveEmptyEntries))
    $inventory = [ordered]@{}
    foreach ($path in $paths) {
        $relative = $path.Replace('\', '/')
        $full = [IO.Path]::GetFullPath((Join-Path $RepositoryRoot $relative))
        if (-not (Test-Path -LiteralPath $full)) {
            $inventory[$relative] = 'missing'
            continue
        }
        $item = Get-Item -Force -LiteralPath $full
        if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
            $inventory[$relative] = "reparse:$($item.LinkTarget)"
        } elseif ($item.PSIsContainer) {
            try {
                $submoduleHead = (Invoke-LdNative -FileName 'git' -ArgumentList @('-C', $full, 'rev-parse', 'HEAD')).Trim()
                $submoduleStatus = Invoke-LdNative -FileName 'git' -ArgumentList @('-C', $full, 'status', '--porcelain=v2', '-z', '--untracked-files=all')
                $inventory[$relative] = "submodule:$submoduleHead`n$submoduleStatus"
            } catch {
                $inventory[$relative] = 'directory'
            }
        } else {
            $inventory[$relative] = (Get-FileHash -Algorithm SHA256 -LiteralPath $full).Hash.ToLowerInvariant()
        }
    }
    return $inventory
}

function Compare-LdInventory {
    param(
        [Parameter(Mandatory)]$Before,
        [Parameter(Mandatory)]$After
    )

    $all = @($Before.Keys) + @($After.Keys) | Sort-Object -Unique
    return @($all | Where-Object {
        $beforeValue = if ($Before.Contains($_)) { [string]$Before[$_] } else { '<absent>' }
        $afterValue = if ($After.Contains($_)) { [string]$After[$_] } else { '<absent>' }
        $beforeValue -ne $afterValue
    })
}

function Copy-LdBaselineUntrackedFiles {
    param(
        [Parameter(Mandatory)][string]$RepositoryRoot,
        [Parameter(Mandatory)][string]$Destination
    )

    $raw = Invoke-LdGit -RepositoryRoot $RepositoryRoot -Arguments @('ls-files', '-z', '--others', '--exclude-standard')
    foreach ($relative in @($raw.Split([char]0, [StringSplitOptions]::RemoveEmptyEntries))) {
        $source = Join-Path $RepositoryRoot $relative
        if (-not (Test-Path -LiteralPath $source -PathType Leaf)) { continue }
        $item = Get-Item -Force -LiteralPath $source
        if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) { continue }
        $target = Join-Path $Destination $relative
        [void](New-Item -ItemType Directory -Force -Path (Split-Path -Parent $target))
        Copy-Item -LiteralPath $source -Destination $target
    }
}

function Invoke-LdCodexDeveloper {
    param(
        [Parameter(Mandatory)][string]$Executable,
        [Parameter(Mandatory)][string]$RepositoryRoot,
        [Parameter(Mandatory)][string]$CodexHome,
        [Parameter(Mandatory)][string]$ResultPath,
        [Parameter(Mandatory)][string]$EventsPath,
        [Parameter(Mandatory)][string]$StderrPath,
        [Parameter(Mandatory)][string]$Prompt,
        [Parameter(Mandatory)][int]$TimeoutMinutes,
        [Parameter(Mandatory)][int]$InactivityTimeoutMinutes
    )

    $start = [Diagnostics.ProcessStartInfo]::new()
    $start.FileName = $Executable
    $start.UseShellExecute = $false
    $start.RedirectStandardOutput = $true
    $start.RedirectStandardError = $true
    $start.RedirectStandardInput = $true
    $start.WorkingDirectory = $RepositoryRoot
    $start.Environment['CODEX_HOME'] = $CodexHome
    $start.Environment['LOCAL_DELEGATION_ACTIVE'] = '1'
    foreach ($argument in @(
        'exec', '--profile', 'local-developer', '--strict-config',
        '--approve-for-me', '--ephemeral', '--json',
        '--output-last-message', $ResultPath,
        '--cd', $RepositoryRoot, '-'
    )) {
        [void]$start.ArgumentList.Add($argument)
    }

    $process = [Diagnostics.Process]::new()
    $process.StartInfo = $start
    [void]$process.Start()
    $process.StandardInput.Write($Prompt)
    $process.StandardInput.Close()
    $result = Wait-LdProcessWithActivityTimeout `
        -Process $process `
        -StandardOutputPath $EventsPath `
        -StandardErrorPath $StderrPath `
        -HardTimeout ([TimeSpan]::FromMinutes($TimeoutMinutes)) `
        -InactivityTimeout ([TimeSpan]::FromMinutes($InactivityTimeoutMinutes))
    $process.Dispose()
    return $result
}

$lockStream = $null
$runDirectory = $null
$handoffFullPath = $null
$runnerRecord = [ordered]@{
    schemaVersion = 1
    status = 'starting'
    exitCode = 10
    startedAt = [DateTimeOffset]::UtcNow.ToString('o')
}

try {
    if ($env:LOCAL_DELEGATION_ACTIVE -eq '1') {
        throw 'Recursive delegation is forbidden when LOCAL_DELEGATION_ACTIVE=1.'
    }
    $resolvedStateRoot = Get-LdStateRoot -Override $StateRoot
    Initialize-LdStateRoot -StateRoot $resolvedStateRoot
    $configuration = Read-LdProviderConfig -StateRoot $resolvedStateRoot
    if ($null -eq $configuration.lastDoctor -or $configuration.lastDoctor.status -ne 'passed') {
        $exception = [InvalidOperationException]::new('Provider doctor has not passed. Run doctor.ps1 before delegation.')
        $exception.Data['LdExitCode'] = 20
        throw $exception
    }
    $profilePath = Join-Path $resolvedStateRoot 'codex-home/local-developer.config.toml'
    if (-not (Test-Path -LiteralPath $profilePath -PathType Leaf)) {
        $exception = [InvalidOperationException]::new("Isolated local-developer profile not found: $profilePath")
        $exception.Data['LdExitCode'] = 20
        throw $exception
    }
    try {
        $providerProbe = Get-LdProviderProbe -Provider $configuration.provider -BaseUrl $configuration.origin -TimeoutSeconds 10
        if ($providerProbe.Models -notcontains [string]$configuration.model) {
            throw "Configured model '$($configuration.model)' is not reported by the provider."
        }
    } catch {
        $exception = [InvalidOperationException]::new("Provider preflight failed: $($_.Exception.Message)")
        $exception.Data['LdExitCode'] = 20
        throw $exception
    }

    $requestedRepository = [IO.Path]::GetFullPath($Repository)
    $repositoryRoot = (Invoke-LdNative -FileName 'git' -ArgumentList @('-C', $requestedRepository, 'rev-parse', '--show-toplevel')).Trim()
    $repositoryRoot = [IO.Path]::GetFullPath($repositoryRoot)
    $repositoryTimeouts = Read-LdRepositoryTimeoutConfig -RepositoryRoot $repositoryRoot
    $effectiveTimeoutMinutes = if ($PSBoundParameters.ContainsKey('TimeoutMinutes')) {
        $TimeoutMinutes
    } elseif ($null -ne $repositoryTimeouts.TimeoutMinutes) {
        [int]$repositoryTimeouts.TimeoutMinutes
    } else { 60 }
    $effectiveInactivityTimeoutMinutes = if ($PSBoundParameters.ContainsKey('InactivityTimeoutMinutes')) {
        $InactivityTimeoutMinutes
    } elseif ($null -ne $repositoryTimeouts.InactivityTimeoutMinutes) {
        [int]$repositoryTimeouts.InactivityTimeoutMinutes
    } else { 15 }
    $handoffFullPath = [IO.Path]::GetFullPath($HandoffPath)
    $rootPrefix = $repositoryRoot.TrimEnd([IO.Path]::DirectorySeparatorChar, [IO.Path]::AltDirectorySeparatorChar) + [IO.Path]::DirectorySeparatorChar
    if (-not $handoffFullPath.StartsWith($rootPrefix, [StringComparison]::OrdinalIgnoreCase) -or -not (Test-Path -LiteralPath $handoffFullPath -PathType Leaf)) {
        throw 'Handoff must be an existing file beneath the repository root.'
    }
    $taskId = Split-Path -Leaf (Split-Path -Parent $handoffFullPath)
    if ($taskId -cnotmatch '^[a-z0-9][a-z0-9-]{0,63}$') {
        throw "Invalid task id '$taskId'."
    }

    $allowed = @($AllowedPath | ForEach-Object { ConvertTo-LdRelativePath $_ } | Sort-Object -Unique)
    foreach ($path in $allowed) { [void](Assert-LdPathInsideRepository -RepositoryRoot $repositoryRoot -RelativePath $path) }
    $handoffRelativeDirectory = [IO.Path]::GetRelativePath($repositoryRoot, (Split-Path -Parent $handoffFullPath)).Replace('\', '/')
    $protected = @('.git', $handoffRelativeDirectory) + @($ProtectedPath | ForEach-Object { ConvertTo-LdRelativePath $_ }) | Sort-Object -Unique
    foreach ($path in $protected) { [void](Assert-LdPathInsideRepository -RepositoryRoot $repositoryRoot -RelativePath $path) }

    $repositoryId = (Get-LdSha256Text -Value $repositoryRoot).Substring(0, 24)
    $lockPath = Join-Path $resolvedStateRoot "locks/$repositoryId.lock"
    try {
        $lockStream = [IO.File]::Open($lockPath, [IO.FileMode]::CreateNew, [IO.FileAccess]::ReadWrite, [IO.FileShare]::None)
    } catch [IO.IOException] {
        throw "Another delegation is already active for this repository: $lockPath"
    }

    $candidateRunDirectory = Join-Path $resolvedStateRoot "run/$repositoryId/$taskId"
    if (Test-Path -LiteralPath $candidateRunDirectory) {
        throw "Run directory already exists; choose a new task id: $candidateRunDirectory"
    }
    $runDirectory = $candidateRunDirectory
    [void](New-Item -ItemType Directory -Path $runDirectory)
    [void](New-Item -ItemType Directory -Path (Join-Path $runDirectory 'baseline-files'))
    Copy-Item -LiteralPath $handoffFullPath -Destination (Join-Path $runDirectory 'request.md')

    $headBefore = (Invoke-LdGit -RepositoryRoot $repositoryRoot -Arguments @('rev-parse', 'HEAD')).Trim()
    $refsBefore = Invoke-LdGit -RepositoryRoot $repositoryRoot -Arguments @('for-each-ref', '--format=%(refname)%00%(objectname)%00')
    $statusBefore = Invoke-LdGit -RepositoryRoot $repositoryRoot -Arguments @('status', '--porcelain=v2', '-z', '--untracked-files=all')
    $inventoryBefore = Get-LdWorkspaceInventory -RepositoryRoot $repositoryRoot
    $baselineDiff = Invoke-LdGit -RepositoryRoot $repositoryRoot -Arguments @('diff', '--binary', 'HEAD', '--')
    Write-LdUtf8File -Path (Join-Path $runDirectory 'baseline.diff') -Content $baselineDiff
    Copy-LdBaselineUntrackedFiles -RepositoryRoot $repositoryRoot -Destination (Join-Path $runDirectory 'baseline-files')
    $baseline = [ordered]@{
        repository = $repositoryRoot
        head = $headBefore
        refs = $refsBefore
        status = $statusBefore
        inventory = $inventoryBefore
        allowedPaths = $allowed
        protectedPaths = $protected
    }
    Write-LdUtf8File -Path (Join-Path $runDirectory 'baseline.json') -Content (($baseline | ConvertTo-Json -Depth 32) + "`n")

    $codexExecutable = $CodexBin
    if ([string]::IsNullOrWhiteSpace($codexExecutable)) { $codexExecutable = $env:LOCAL_DELEGATE_CODEX_BIN }
    if ([string]::IsNullOrWhiteSpace($codexExecutable)) {
        $command = Get-Command codex -ErrorAction SilentlyContinue
        if ($null -eq $command) { throw 'Codex CLI was not found.' }
        $codexExecutable = $command.Source
    }
    $schemaPath = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '../schemas/developer-result.schema.json'))
    $resultPath = Join-Path $runDirectory 'result.json'
    $eventsPath = Join-Path $runDirectory 'events.jsonl'
    $stderrPath = Join-Path $runDirectory 'stderr.log'
    $handoffText = Get-Content -Raw -LiteralPath $handoffFullPath
    $tests = if ($TestCommand.Count -gt 0) { $TestCommand -join "`n" } else { 'Use only the verification commands stated in the handoff.' }
    $prompt = @"
You are the delegated local developer. LOCAL_DELEGATION_ACTIVE=1 is set.
Never invoke local-delegate, another delegation workflow, or a subagent.
Do not commit, push, change Git refs, install dependencies, use the network,
run migrations, or perform external actions. Change only these repository-relative
paths: $($allowed -join ', '). Do not modify protected paths: $($protected -join ', ').
Read and implement this handoff and run only its prescribed verification.
Your final response MUST be only one JSON object, with no markdown fences or other
text. It must have exactly these fields:
{"status":"completed|partial|failed","summary":"string","changed_files":["path"],"commands":[{"command":"string","outcome":"string"}],"known_limitations":["string"],"follow_up_needs":["string"]}

Verification commands supplied by the analyst:
$tests

Handoff:
$handoffText
"@

    $runnerRecord.repository = $repositoryRoot
    $runnerRecord.repositoryId = $repositoryId
    $runnerRecord.taskId = $taskId
    $runnerRecord.runDirectory = $runDirectory
    $runnerRecord.timeoutMinutes = $effectiveTimeoutMinutes
    $runnerRecord.inactivityTimeoutMinutes = $effectiveInactivityTimeoutMinutes
    $runnerRecord.status = 'running'
    $processResult = Invoke-LdCodexDeveloper `
        -Executable $codexExecutable `
        -RepositoryRoot $repositoryRoot `
        -CodexHome (Join-Path $resolvedStateRoot 'codex-home') `
        -ResultPath $resultPath `
        -EventsPath $eventsPath `
        -StderrPath $stderrPath `
        -Prompt $prompt `
        -TimeoutMinutes $effectiveTimeoutMinutes `
        -InactivityTimeoutMinutes $effectiveInactivityTimeoutMinutes

    $inventoryAfter = Get-LdWorkspaceInventory -RepositoryRoot $repositoryRoot
    $changedDuringRun = Compare-LdInventory -Before $inventoryBefore -After $inventoryAfter
    $headAfter = (Invoke-LdGit -RepositoryRoot $repositoryRoot -Arguments @('rev-parse', 'HEAD')).Trim()
    $refsAfter = Invoke-LdGit -RepositoryRoot $repositoryRoot -Arguments @('for-each-ref', '--format=%(refname)%00%(objectname)%00')
    $policyViolations = [Collections.Generic.List[string]]::new()
    foreach ($path in $changedDuringRun) {
        if ($inventoryAfter.Contains($path) -and [string]$inventoryAfter[$path] -like 'reparse:*') {
            $policyViolations.Add("Changed path is a symlink or junction: $path")
        } elseif ($protected | Where-Object { Test-LdPathPrefix -Path $path -Prefix $_ }) {
            $policyViolations.Add("Protected path changed: $path")
        } elseif (-not ($allowed | Where-Object { Test-LdPathPrefix -Path $path -Prefix $_ })) {
            $policyViolations.Add("Out-of-scope path changed: $path")
        }
    }
    if ($headBefore -ne $headAfter -or $refsBefore -ne $refsAfter) {
        $policyViolations.Add('HEAD or Git refs changed during delegation.')
    }

    $runnerRecord.changedDuringRun = $changedDuringRun
    $runnerRecord.policyViolations = @($policyViolations)
    $runnerRecord.developerExitCode = $processResult.ExitCode
    $runnerRecord.lastDeveloperActivityAt = $processResult.LastActivityAt
    if (-not $processResult.Completed) {
        $runnerRecord.status = 'timeout'
        $runnerRecord.exitCode = 31
        $runnerRecord.timeoutReason = $processResult.TerminationReason
    } elseif ($policyViolations.Count -gt 0) {
        $runnerRecord.status = 'policy-failure'
        $runnerRecord.exitCode = 50
    } elseif ($processResult.ExitCode -ne 0) {
        $runnerRecord.status = 'developer-failure'
        $runnerRecord.exitCode = 30
    } elseif (-not (Test-Path -LiteralPath $resultPath -PathType Leaf)) {
        $runnerRecord.status = 'verification-failure'
        $runnerRecord.exitCode = 40
        $runnerRecord.verificationError = 'Codex did not produce result.json.'
    } else {
        try {
            $resultText = (Get-Content -Raw -LiteralPath $resultPath).Trim()
            if ($resultText -match '(?s)^```(?:json)?\s*(?<json>\{.*\})\s*```$') {
                $resultText = $Matches.json.Trim()
            }
            $developerResult = $resultText | ConvertFrom-Json -Depth 32
            if (-not ($resultText | Test-Json -SchemaFile $schemaPath)) {
                throw 'result does not conform to developer-result.schema.json'
            }
            Write-LdUtf8File -Path $resultPath -Content ($resultText + "`n")
            $runnerRecord.developerReportedStatus = [string]$developerResult.status
            if ($developerResult.status -eq 'completed') {
                $runnerRecord.status = 'completed'
                $runnerRecord.exitCode = 0
            } else {
                $runnerRecord.status = 'developer-failure'
                $runnerRecord.exitCode = 30
            }
        } catch {
            $runnerRecord.status = 'verification-failure'
            $runnerRecord.exitCode = 40
            $runnerRecord.verificationError = "result.json is invalid JSON: $($_.Exception.Message)"
        }
    }
} catch {
    $requestedExitCode = if ($_.Exception.Data.Contains('LdExitCode')) { [int]$_.Exception.Data['LdExitCode'] } else { 10 }
    $runnerRecord.status = if ($requestedExitCode -eq 20) { 'endpoint-profile-failure' } else { 'configuration-failure' }
    $runnerRecord.exitCode = $requestedExitCode
    $runnerRecord.error = $_.Exception.Message
} finally {
    $runnerRecord.finishedAt = [DateTimeOffset]::UtcNow.ToString('o')
    if ($null -ne $runDirectory -and (Test-Path -LiteralPath $runDirectory)) {
        $runnerPath = Join-Path $runDirectory 'runner.json'
        Write-LdUtf8File -Path $runnerPath -Content (($runnerRecord | ConvertTo-Json -Depth 32) + "`n")
        if ($null -ne $handoffFullPath -and (Test-Path -LiteralPath (Split-Path -Parent $handoffFullPath))) {
            Copy-Item -LiteralPath $runnerPath -Destination (Join-Path (Split-Path -Parent $handoffFullPath) 'runner.json') -Force
            $resultSource = Join-Path $runDirectory 'result.json'
            if (Test-Path -LiteralPath $resultSource -PathType Leaf) {
                Copy-Item -LiteralPath $resultSource -Destination (Join-Path (Split-Path -Parent $handoffFullPath) 'result.json') -Force
            }
        }
    }
    if ($null -ne $lockStream) {
        $lockName = $lockStream.Name
        $lockStream.Dispose()
        Remove-Item -LiteralPath $lockName -Force -ErrorAction SilentlyContinue
    }
}

if ($runnerRecord.exitCode -ne 0) {
    Write-Error ($runnerRecord | ConvertTo-Json -Depth 8 -Compress) -ErrorAction Continue
}
exit $runnerRecord.exitCode
