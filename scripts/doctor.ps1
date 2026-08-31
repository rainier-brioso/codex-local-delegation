[CmdletBinding()]
param(
    [string]$StateRoot,
    [int]$TimeoutSeconds = 120
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'lib/LocalDelegation.Common.ps1')

function Test-LdStreamingResponse {
    param(
        [Parameter(Mandatory)][string]$Url,
        [Parameter(Mandatory)][string]$Model,
        [Parameter(Mandatory)][int]$TimeoutSeconds
    )

    $client = New-LdHttpClient -TimeoutSeconds $TimeoutSeconds
    $request = $null
    try {
        $payload = [ordered]@{
            model = $Model
            input = 'Reply with the single word ready.'
            stream = $true
            max_output_tokens = 16
        }
        $request = [Net.Http.HttpRequestMessage]::new([Net.Http.HttpMethod]::Post, $Url)
        $request.Content = [Net.Http.StringContent]::new(
            ($payload | ConvertTo-Json -Depth 16 -Compress),
            [Text.Encoding]::UTF8,
            'application/json'
        )
        $response = $client.Send($request, [Net.Http.HttpCompletionOption]::ResponseHeadersRead)
        if (-not $response.IsSuccessStatusCode) {
            $body = $response.Content.ReadAsStringAsync().GetAwaiter().GetResult()
            throw "Streaming Responses probe returned HTTP $([int]$response.StatusCode): $body"
        }
        $contentType = $response.Content.Headers.ContentType.MediaType
        if ($contentType -ne 'text/event-stream') {
            throw "Streaming Responses probe returned '$contentType' instead of text/event-stream."
        }
        $stream = $response.Content.ReadAsStream()
        $reader = [IO.StreamReader]::new($stream)
        $sawData = $false
        try {
            while (-not $reader.EndOfStream) {
                $line = $reader.ReadLine()
                if ($line.StartsWith('data:') -and $line.Substring(5).Trim().Length -gt 0) {
                    $sawData = $true
                }
            }
        } finally {
            $reader.Dispose()
        }
        if (-not $sawData) {
            throw 'Streaming Responses probe returned no SSE data events.'
        }
    } finally {
        if ($null -ne $request) { $request.Dispose() }
        $client.Dispose()
    }
}

function Test-LdToolRoundTrip {
    param(
        [Parameter(Mandatory)][string]$Url,
        [Parameter(Mandatory)][string]$Model,
        [Parameter(Mandatory)][int]$TimeoutSeconds
    )

    $firstPayload = [ordered]@{
        model = $Model
        input = 'Call local_delegate_doctor_echo exactly once with value set to ping. Do not answer normally.'
        stream = $false
        tool_choice = [ordered]@{ type = 'function'; name = 'local_delegate_doctor_echo' }
        tools = @(
            [ordered]@{
                type = 'function'
                name = 'local_delegate_doctor_echo'
                description = 'A harmless compatibility probe.'
                strict = $true
                parameters = [ordered]@{
                    type = 'object'
                    additionalProperties = $false
                    required = @('value')
                    properties = [ordered]@{
                        value = [ordered]@{ type = 'string' }
                    }
                }
            }
        )
    }
    $first = ConvertFrom-LdJsonResponse (Invoke-LdHttp -Method POST -Url $Url -Body $firstPayload -TimeoutSeconds $TimeoutSeconds)
    $call = @($first.output | Where-Object { $_.type -eq 'function_call' -and $_.name -eq 'local_delegate_doctor_echo' }) | Select-Object -First 1
    if ($null -eq $call -or [string]::IsNullOrWhiteSpace([string]$call.call_id)) {
        throw 'Provider did not return the required Responses function_call with a call_id.'
    }
    $secondPayload = [ordered]@{
        model = $Model
        input = @(
            [ordered]@{
                type = 'function_call'
                name = [string]$call.name
                call_id = [string]$call.call_id
                arguments = [string]$call.arguments
            }
            [ordered]@{
                type = 'function_call_output'
                call_id = [string]$call.call_id
                output = '{"value":"pong"}'
            }
        )
        stream = $false
    }
    $second = ConvertFrom-LdJsonResponse (Invoke-LdHttp -Method POST -Url $Url -Body $secondPayload -TimeoutSeconds $TimeoutSeconds)
    if ([string]::IsNullOrWhiteSpace([string]$second.id) -or @($second.output).Count -eq 0) {
        throw 'Provider did not complete the tool-output continuation.'
    }
}

try {
    $resolvedStateRoot = Get-LdStateRoot -Override $StateRoot
    Initialize-LdStateRoot -StateRoot $resolvedStateRoot

    $codex = Get-Command codex -ErrorAction SilentlyContinue
    if ($null -eq $codex) {
        throw 'Codex CLI was not found on PATH.'
    }
    $codexHelp = (& $codex.Source exec --help 2>&1) -join "`n"
    foreach ($requiredFlag in @('--profile', '--strict-config', '--sandbox', '--ephemeral', '--json', '--output-last-message', '--cd')) {
        if ($codexHelp -notmatch [regex]::Escape($requiredFlag)) {
            throw "Codex CLI does not expose required option $requiredFlag. Upgrade Codex CLI."
        }
    }
    $profilePath = Join-Path $resolvedStateRoot 'codex-home/local-developer.config.toml'
    if (-not (Test-Path -LiteralPath $profilePath -PathType Leaf)) {
        throw "Isolated local-developer profile not found: $profilePath"
    }

    $configuration = Read-LdProviderConfig -StateRoot $resolvedStateRoot
    $probe = Get-LdProviderProbe -Provider $configuration.provider -BaseUrl $configuration.origin -TimeoutSeconds ([Math]::Min($TimeoutSeconds, 15))
    if ($probe.Models -notcontains [string]$configuration.model) {
        throw "Configured model '$($configuration.model)' is no longer reported by the provider."
    }

    $responsesUrl = "$($configuration.responsesBaseUrl.TrimEnd('/'))/responses"
    Write-Host "Provider: $($probe.Identity)"
    Write-Host "Model: $($configuration.model)"
    Write-Host 'Checking streamed Responses output...'
    Test-LdStreamingResponse -Url $responsesUrl -Model $configuration.model -TimeoutSeconds $TimeoutSeconds
    Write-Host 'Checking tool-call/tool-output continuation...'
    Test-LdToolRoundTrip -Url $responsesUrl -Model $configuration.model -TimeoutSeconds $TimeoutSeconds

    $configuration.lastDoctor = [ordered]@{
        status = 'passed'
        checkedAt = [DateTimeOffset]::UtcNow.ToString('o')
        codexPath = [string]$codex.Source
        providerIdentity = $probe.Identity
    }
    $configPath = Get-LdProviderConfigPath -StateRoot $resolvedStateRoot
    Write-LdUtf8File -Path $configPath -Content (($configuration | ConvertTo-Json -Depth 32) + "`n")
    Write-Host 'Doctor passed. Local provider is ready for delegation.'
    exit 0
} catch {
    Write-Error $_.Exception.Message
    exit 20
}
