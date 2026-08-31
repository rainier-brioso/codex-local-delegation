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
Test-Condition -Condition ($skillText -match '(?m)^name: local-delegate$') -Message 'Skill name is valid'
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
    $catalog = Get-Content -Raw -LiteralPath (Join-Path $integrationRoot 'codex-home/model-catalog.json') | ConvertFrom-Json -Depth 32
    Test-Condition -Condition ($catalog.models[0].shell_type -eq 'shell_command') -Message 'Model catalog selects function-based shell tools'
    Test-Condition -Condition ($catalog.models[0].slug -eq 'local-test-model') -Message 'Model catalog records the selected local model'
    Test-Condition -Condition ($catalog.models[0].context_window -eq 32768) -Message 'Model catalog uses the conservative default context window'
    $doctorOutput = & pwsh -NoLogo -NoProfile -File (Join-Path $projectRoot 'scripts/doctor.ps1') `
        -StateRoot $integrationRoot -TimeoutSeconds 20 2>&1
    if ($LASTEXITCODE -ne 0) { $doctorOutput | ForEach-Object { Write-Host "DOCTOR $_" } }
    Test-Condition -Condition ($LASTEXITCODE -eq 0) -Message 'Doctor completes Responses and tool-call probes'
    $integrationConfig = Get-Content -Raw -LiteralPath (Join-Path $integrationRoot 'config/provider.json') | ConvertFrom-Json -Depth 32
    Test-Condition -Condition ($integrationConfig.lastDoctor.status -eq 'passed') -Message 'Doctor records a successful compatibility result'
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
