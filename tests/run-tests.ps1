[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$projectRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$failures = [Collections.Generic.List[string]]::new()
$passes = 0

function Test-Condition {
    param(
        [Parameter(Mandatory)][bool]$Condition,
        [Parameter(Mandatory)][string]$Message
    )
    if ($Condition) {
        $script:passes++
        Write-Host "PASS $Message"
    } else {
        $script:failures.Add($Message)
        Write-Host "FAIL $Message" -ForegroundColor Red
    }
}

function Test-Throws {
    param(
        [Parameter(Mandatory)][scriptblock]$Action,
        [Parameter(Mandatory)][string]$Message
    )
    try {
        & $Action
        Test-Condition -Condition $false -Message $Message
    } catch {
        Test-Condition -Condition $true -Message $Message
    }
}

function Start-LdTestProcess {
    param([Parameter(Mandatory)][string]$Command)

    $start = [Diagnostics.ProcessStartInfo]::new()
    $start.FileName = (Get-Command pwsh).Source
    $start.UseShellExecute = $false
    $start.RedirectStandardOutput = $true
    $start.RedirectStandardError = $true
    foreach ($argument in @('-NoLogo', '-NoProfile', '-Command', $Command)) {
        [void]$start.ArgumentList.Add($argument)
    }
    $process = [Diagnostics.Process]::new()
    $process.StartInfo = $start
    [void]$process.Start()
    return $process
}

foreach ($scriptFile in Get-ChildItem -Path (Join-Path $projectRoot 'scripts') -Filter '*.ps1' -Recurse) {
    $tokens = $null
    $parseErrors = $null
    [void][Management.Automation.Language.Parser]::ParseFile($scriptFile.FullName, [ref]$tokens, [ref]$parseErrors)
    Test-Condition -Condition ($parseErrors.Count -eq 0) -Message "PowerShell parses: $($scriptFile.Name)"
}

$manifestPath = Join-Path $projectRoot '.codex-plugin/plugin.json'
$manifest = Get-Content -Raw -LiteralPath $manifestPath | ConvertFrom-Json -Depth 32
Test-Condition -Condition ($manifest.name -eq 'codex-local-delegation') -Message 'Manifest name matches plugin directory'
Test-Condition -Condition ($manifest.version -match '^\d+\.\d+\.\d+$') -Message 'Manifest uses semantic versioning'
Test-Condition -Condition ($manifest.skills -eq './skills/') -Message 'Manifest exposes the skills directory'
Test-Condition -Condition ($manifest.repository -eq 'https://github.com/rainier-brioso/codex-local-delegation') -Message 'Manifest identifies the public source repository'

$marketplacePath = Join-Path $projectRoot '.agents/plugins/marketplace.json'
$marketplace = Get-Content -Raw -LiteralPath $marketplacePath | ConvertFrom-Json -Depth 32
Test-Condition -Condition ($marketplace.name -eq 'rainier-local-tools') -Message 'Repository marketplace has a stable identifier'
Test-Condition -Condition ($marketplace.plugins.Count -eq 1) -Message 'Repository marketplace exposes one plugin'
Test-Condition -Condition ($marketplace.plugins[0].name -eq $manifest.name) -Message 'Marketplace and manifest plugin names match'
Test-Condition -Condition ($marketplace.plugins[0].source.source -eq 'url') -Message 'Marketplace uses the repository-root URL source'
Test-Condition -Condition ($marketplace.plugins[0].policy.installation -eq 'AVAILABLE') -Message 'Marketplace plugin is available for installation'

$schemaPath = Join-Path $projectRoot 'schemas/developer-result.schema.json'
$null = Get-Content -Raw -LiteralPath $schemaPath | ConvertFrom-Json -Depth 64
$sampleResult = @{
    status = 'completed'
    summary = 'Implemented the bounded task.'
    changed_files = @('src/example.txt')
    commands = @(@{ command = 'test'; outcome = 'passed' })
    known_limitations = @()
    follow_up_needs = @()
} | ConvertTo-Json -Depth 16
Test-Condition -Condition ($sampleResult | Test-Json -SchemaFile $schemaPath) -Message 'Developer result schema accepts a valid result'

$skillPath = Join-Path $projectRoot 'skills/local-delegate/SKILL.md'
$skillText = Get-Content -Raw -LiteralPath $skillPath
Test-Condition -Condition ($skillText -match '(?m)^name: local-delegate\r?$') -Message 'Skill name is valid'
Test-Condition -Condition ($skillText -notmatch '\[TODO:') -Message 'Skill contains no scaffold placeholders'
Test-Condition -Condition ($skillText -match '<plugin-root>/scripts/run-local-developer\.ps1') -Message 'Skill resolves the runner from the installed plugin root'
Test-Condition -Condition ($skillText -notmatch '(?m)(?<!<plugin-root>/)`scripts/(doctor|run-local-developer)\.ps1`') -Message 'Skill has no workspace-relative runner references'

. (Join-Path $projectRoot 'scripts/lib/LocalDelegation.Common.ps1')
$ollamaUrls = Get-LdOriginAndResponsesBase -BaseUrl 'http://127.0.0.1:11434'
Test-Condition -Condition ($ollamaUrls.Origin -eq 'http://127.0.0.1:11434') -Message 'Ollama origin is normalized'
Test-Condition -Condition ($ollamaUrls.ResponsesBaseUrl -eq 'http://127.0.0.1:11434/v1') -Message 'Ollama Responses base URL is derived'
$llamaUrls = Get-LdOriginAndResponsesBase -BaseUrl 'http://localhost:8080/v1/'
Test-Condition -Condition ($llamaUrls.Origin -eq 'http://localhost:8080') -Message 'Existing /v1 suffix is normalized once'
Test-Throws -Action { Assert-LdLoopbackUri -Url 'http://192.168.1.20:8080' } -Message 'LAN provider URLs are rejected'
Test-Throws -Action { Assert-LdLoopbackUri -Url 'https://127.0.0.1:8080' } -Message 'HTTPS is rejected by the v1 loopback contract'
Test-Throws -Action { Get-LdOriginAndResponsesBase -BaseUrl 'http://127.0.0.1:8080/custom' } -Message 'Unexpected provider URL paths are rejected'
Test-Condition -Condition ((ConvertTo-LdTomlString 'a\b"c') -eq '"a\\b\"c"') -Message 'TOML strings escape slash and quote characters'

$integrationRoot = Join-Path $projectRoot ('.test-tmp/' + [Guid]::NewGuid().ToString('N'))
$mockProcess = $null
try {
    [void](New-Item -ItemType Directory -Force -Path $integrationRoot)
    $configRepository = Join-Path $integrationRoot 'config-repository'
    [void](New-Item -ItemType Directory -Force -Path (Join-Path $configRepository '.codex'))
    Write-LdUtf8File -Path (Join-Path $configRepository '.codex/local-delegate.toml') -Content @"
timeout_minutes = 90
inactivity_timeout_minutes = 12
"@
    $timeoutSettings = Read-LdRepositoryTimeoutConfig -RepositoryRoot $configRepository
    Test-Condition -Condition ($timeoutSettings.TimeoutMinutes -eq 90) -Message 'Repository config supplies the hard timeout'
    Test-Condition -Condition ($timeoutSettings.InactivityTimeoutMinutes -eq 12) -Message 'Repository config supplies the inactivity timeout'
    Write-LdUtf8File -Path (Join-Path $configRepository '.codex/local-delegate.toml') -Content "inactivity_timeout_minutes = 1441`n"
    Test-Throws -Action { Read-LdRepositoryTimeoutConfig -RepositoryRoot $configRepository } -Message 'Repository config rejects an excessive inactivity timeout'

    $idleProcess = Start-LdTestProcess -Command 'Start-Sleep -Seconds 4'
    try {
        $idleResult = Wait-LdProcessWithActivityTimeout `
            -Process $idleProcess `
            -StandardOutputPath (Join-Path $integrationRoot 'idle-events.jsonl') `
            -StandardErrorPath (Join-Path $integrationRoot 'idle-stderr.log') `
            -HardTimeout ([TimeSpan]::FromSeconds(15)) `
            -InactivityTimeout ([TimeSpan]::FromSeconds(2)) `
            -PollMilliseconds 50
        Test-Condition -Condition (-not $idleResult.Completed -and $idleResult.TerminationReason -eq 'inactivity-timeout') -Message 'Silent developer process reaches the inactivity timeout'
    } finally { $idleProcess.Dispose() }

    $activeProcess = Start-LdTestProcess -Command '1..5 | ForEach-Object { Write-Output $_; Start-Sleep -Milliseconds 750 }'
    try {
        $activeResult = Wait-LdProcessWithActivityTimeout `
            -Process $activeProcess `
            -StandardOutputPath (Join-Path $integrationRoot 'active-events.jsonl') `
            -StandardErrorPath (Join-Path $integrationRoot 'active-stderr.log') `
            -HardTimeout ([TimeSpan]::FromSeconds(15)) `
            -InactivityTimeout ([TimeSpan]::FromSeconds(2)) `
            -PollMilliseconds 50
        Test-Condition -Condition ($activeResult.Completed -and $activeResult.ExitCode -eq 0) -Message 'Periodic developer output keeps the process active'
    } finally { $activeProcess.Dispose() }

    $disabledProcess = Start-LdTestProcess -Command 'Start-Sleep -Milliseconds 400'
    try {
        $disabledResult = Wait-LdProcessWithActivityTimeout `
            -Process $disabledProcess `
            -StandardOutputPath (Join-Path $integrationRoot 'disabled-events.jsonl') `
            -StandardErrorPath (Join-Path $integrationRoot 'disabled-stderr.log') `
            -HardTimeout ([TimeSpan]::FromSeconds(10)) `
            -InactivityTimeout ([TimeSpan]::Zero) `
            -PollMilliseconds 25
        Test-Condition -Condition ($disabledResult.Completed -and $disabledResult.ExitCode -eq 0) -Message 'Zero disables the inactivity timeout'
    } finally { $disabledProcess.Dispose() }

    $portPicker = [Net.Sockets.TcpListener]::new([Net.IPAddress]::Loopback, 0)
    $portPicker.Start()
    $port = ([Net.IPEndPoint]$portPicker.LocalEndpoint).Port
    $portPicker.Stop()
    $readyFile = Join-Path $integrationRoot 'ready'
    $mockProcess = Start-Process -FilePath (Get-Command pwsh).Source -WindowStyle Hidden -PassThru -ArgumentList @(
        '-NoLogo', '-NoProfile', '-File', (Join-Path $PSScriptRoot 'mock-provider.ps1'),
        '-Port', [string]$port, '-ReadyFile', $readyFile
    )
    $deadline = [DateTime]::UtcNow.AddSeconds(10)
    while (-not (Test-Path -LiteralPath $readyFile) -and [DateTime]::UtcNow -lt $deadline) {
        Start-Sleep -Milliseconds 50
    }
    Test-Condition -Condition (Test-Path -LiteralPath $readyFile) -Message 'Mock Responses provider starts'

    $configureOutput = & pwsh -NoLogo -NoProfile -File (Join-Path $projectRoot 'scripts/configure-provider.ps1') `
        -Provider Custom -BaseUrl "http://127.0.0.1:$port" -Model local-test-model -StateRoot $integrationRoot 2>&1
    Test-Condition -Condition ($LASTEXITCODE -eq 0) -Message 'Explicit custom provider configuration succeeds'
    $profileText = Get-Content -Raw -LiteralPath (Join-Path $integrationRoot 'codex-home/local-developer.config.toml')
    Test-Condition -Condition ($profileText -match 'web_search = "disabled"') -Message 'Worker profile disables native web search'
    $runnerText = Get-Content -Raw -LiteralPath (Join-Path $projectRoot 'scripts/run-local-developer.ps1')
    Test-Condition -Condition ($runnerText -match "'--approve-for-me'") -Message 'Runner uses automatic workspace-write approval review'
    Test-Condition -Condition ($runnerText -notmatch "'--sandbox', 'workspace-write'") -Message 'Runner avoids the conflicting explicit sandbox flag'
    Test-Condition -Condition ($runnerText -match '\[int\]\$InactivityTimeoutMinutes = 15') -Message 'Runner exposes the inactivity timeout flag'
    $catalog = Get-Content -Raw -LiteralPath (Join-Path $integrationRoot 'codex-home/model-catalog.json') | ConvertFrom-Json -Depth 32
    Test-Condition -Condition ($catalog.models[0].shell_type -eq 'shell_command') -Message 'Model catalog selects function-based shell tools'
    Test-Condition -Condition ($catalog.models[0].slug -eq 'local-test-model') -Message 'Model catalog records the selected local model'
    Test-Condition -Condition ($catalog.models[0].context_window -eq 32768) -Message 'Model catalog uses the conservative default context window'

    $mockBin = Join-Path $integrationRoot 'mock-bin'
    [void](New-Item -ItemType Directory -Force -Path $mockBin)
    $mockCodexPath = Join-Path $mockBin 'codex.cmd'
    Write-LdUtf8File -Path $mockCodexPath -Content @'
@echo off
echo --profile --strict-config --sandbox --ephemeral --json --output-last-message --cd
'@
    $previousPath = $env:PATH
    try {
        $env:PATH = "$mockBin$([IO.Path]::PathSeparator)$previousPath"
        $resolvedCodex = Get-Command codex -ErrorAction Stop
        Test-Condition -Condition ($resolvedCodex.Source -eq $mockCodexPath) -Message 'Doctor test uses the isolated Codex CLI fixture'
        $doctorOutput = & pwsh -NoLogo -NoProfile -File (Join-Path $projectRoot 'scripts/doctor.ps1') `
            -StateRoot $integrationRoot -TimeoutSeconds 20 2>&1
        $doctorExitCode = $LASTEXITCODE
    } finally {
        $env:PATH = $previousPath
    }
    if ($doctorExitCode -ne 0) { $doctorOutput | ForEach-Object { Write-Host "DOCTOR $_" } }
    Test-Condition -Condition ($doctorExitCode -eq 0) -Message 'Doctor completes Responses and tool-call probes'
    $integrationConfig = Get-Content -Raw -LiteralPath (Join-Path $integrationRoot 'config/provider.json') | ConvertFrom-Json -Depth 32
    $doctorRecordedSuccess = $integrationConfig.PSObject.Properties.Name -contains 'lastDoctor'
    if ($doctorRecordedSuccess) { $doctorRecordedSuccess = $integrationConfig.lastDoctor.status -eq 'passed' }
    Test-Condition -Condition $doctorRecordedSuccess -Message 'Doctor records a successful compatibility result'
} finally {
    if ($null -ne $mockProcess -and -not $mockProcess.HasExited) {
        $mockProcess.Kill($true)
        $mockProcess.WaitForExit()
    }
    $resolvedIntegrationRoot = [IO.Path]::GetFullPath($integrationRoot)
    $expectedTestRoot = [IO.Path]::GetFullPath((Join-Path $projectRoot '.test-tmp'))
    if ($resolvedIntegrationRoot.StartsWith($expectedTestRoot + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase) -and (Test-Path -LiteralPath $resolvedIntegrationRoot)) {
        Remove-Item -LiteralPath $resolvedIntegrationRoot -Recurse -Force
    }
}

if ($failures.Count -gt 0) {
    Write-Error "$($failures.Count) test(s) failed: $($failures -join '; ')"
    exit 1
}
Write-Host "$passes tests passed."
exit 0
