[CmdletBinding()]
param(
    [ValidateSet('Auto', 'Ollama', 'LlamaCpp', 'Custom')]
    [string]$Provider = 'Auto',
    [string]$BaseUrl,
    [string]$Model,
    [ValidateRange(4096, 1048576)][int]$ContextWindow = 32768,
    [string]$StateRoot,
    [int]$TimeoutSeconds = 5
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'lib/LocalDelegation.Common.ps1')

try {
    $resolvedStateRoot = Get-LdStateRoot -Override $StateRoot
    Initialize-LdStateRoot -StateRoot $resolvedStateRoot

    $explicit = -not [string]::IsNullOrWhiteSpace($BaseUrl) -or $Provider -ne 'Auto'
    $probes = [Collections.Generic.List[object]]::new()

    if (-not [string]::IsNullOrWhiteSpace($BaseUrl)) {
        if ($Provider -eq 'Auto') {
            throw '-Provider is required when -BaseUrl is supplied. Use Ollama, LlamaCpp, or Custom.'
        }
        $probes.Add((Get-LdProviderProbe -Provider $Provider -BaseUrl $BaseUrl -TimeoutSeconds $TimeoutSeconds))
    } elseif ($Provider -ne 'Auto') {
        $defaultUrl = switch ($Provider) {
            'Ollama' { 'http://127.0.0.1:11434' }
            'LlamaCpp' { 'http://127.0.0.1:8080' }
            default { throw '-BaseUrl is required for the Custom provider.' }
        }
        $probes.Add((Get-LdProviderProbe -Provider $Provider -BaseUrl $defaultUrl -TimeoutSeconds $TimeoutSeconds))
    } else {
        foreach ($candidate in @(
            @{ Provider = 'Ollama'; BaseUrl = 'http://127.0.0.1:11434' },
            @{ Provider = 'LlamaCpp'; BaseUrl = 'http://127.0.0.1:8080' }
        )) {
            try {
                $probes.Add((Get-LdProviderProbe -Provider $candidate.Provider -BaseUrl $candidate.BaseUrl -TimeoutSeconds $TimeoutSeconds))
            } catch {
                Write-Verbose "$($candidate.Provider) was not compatible at $($candidate.BaseUrl): $($_.Exception.Message)"
            }
        }
    }

    if ($probes.Count -eq 0) {
        throw 'No compatible provider found. Start Ollama on 127.0.0.1:11434, llama.cpp on 127.0.0.1:8080, or supply -Provider and -BaseUrl.'
    }
    if ($probes.Count -gt 1) {
        $choices = ($probes | ForEach-Object { "$($_.Provider) at $($_.Origin)" }) -join ', '
        throw "Multiple compatible providers found: $choices. Select one explicitly with -Provider."
    }

    $probe = $probes[0]
    $selectedModel = $Model
    if (-not [string]::IsNullOrWhiteSpace($selectedModel)) {
        if ($probe.Models -notcontains $selectedModel) {
            throw "Model '$selectedModel' is not reported by $($probe.Provider). Available models: $($probe.Models -join ', ')"
        }
    } else {
        $reportedModels = @($probe.Models)
        if ($reportedModels.Count -ne 1) {
            throw "Model selection is ambiguous. Supply -Model. Reported models: $($probe.Models -join ', ')"
        }
        $selectedModel = $reportedModels[0]
    }

    $configuration = [ordered]@{
        schemaVersion = 1
        provider = $probe.Provider
        origin = $probe.Origin
        responsesBaseUrl = $probe.ResponsesBaseUrl
        model = $selectedModel
        contextWindow = $ContextWindow
        selection = if ($explicit) { 'explicit' } else { 'discovered' }
        configuredAt = [DateTimeOffset]::UtcNow.ToString('o')
        lastDoctor = $null
    }
    $configPath = Get-LdProviderConfigPath -StateRoot $resolvedStateRoot
    Write-LdUtf8File -Path $configPath -Content (($configuration | ConvertTo-Json -Depth 16) + "`n")

    $catalogPath = Join-Path $resolvedStateRoot 'codex-home/model-catalog.json'
    $catalog = [ordered]@{
        models = @(
            [ordered]@{
                slug = $selectedModel
                display_name = $selectedModel
                context_window = $ContextWindow
                truncation_policy = [ordered]@{ mode = 'tokens'; limit = [int][Math]::Floor($ContextWindow * 0.9) }
                shell_type = 'shell_command'
                visibility = 'list'
                supported_in_api = $true
                priority = 0
                base_instructions = 'You are a local coding implementation agent. Follow the supplied repository instructions and task handoff. Use the shell_command function for repository inspection, edits, and verification. Do not delegate work.'
                supports_parallel_tool_calls = $false
                experimental_supported_tools = @()
                supports_reasoning_summaries = $false
                support_verbosity = $false
                supported_reasoning_levels = @()
            }
        )
    }
    Write-LdUtf8File -Path $catalogPath -Content (($catalog | ConvertTo-Json -Depth 16) + "`n")

    $profile = @(
        'model_provider = "local-developer"'
        'model = ' + (ConvertTo-LdTomlString $selectedModel)
        'model_catalog_json = ' + (ConvertTo-LdTomlString $catalogPath)
        'model_reasoning_summary = "none"'
        'web_search = "disabled"'
        ''
        '[features]'
        'apps = false'
        'remote_plugin = false'
        'multi_agent = false'
        'plugins = false'
        'skill_search = false'
        ''
        '[model_providers.local-developer]'
        'name = "Local model provider"'
        'base_url = ' + (ConvertTo-LdTomlString $probe.ResponsesBaseUrl)
        'wire_api = "responses"'
        'requires_openai_auth = false'
        ''
        '[sandbox_workspace_write]'
        'network_access = false'
        ''
    ) -join "`n"
    $profilePath = Join-Path $resolvedStateRoot 'codex-home/local-developer.config.toml'
    Write-LdUtf8File -Path $profilePath -Content $profile

    Write-Host "Configured $($probe.Identity)"
    Write-Host "Model: $selectedModel"
    Write-Host "Context window: $ContextWindow"
    Write-Host "Responses base URL: $($probe.ResponsesBaseUrl)"
    Write-Host "State: $resolvedStateRoot"
    Write-Host 'Run doctor.ps1 before delegation.'
    exit 0
} catch {
    Write-Error $_.Exception.Message
    exit 10
}
