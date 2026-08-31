Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Get-LdStateRoot {
    param([string]$Override)

    $candidate = $Override
    if ([string]::IsNullOrWhiteSpace($candidate)) {
        $candidate = $env:LOCAL_DELEGATE_HOME
    }
    if ([string]::IsNullOrWhiteSpace($candidate)) {
        $candidate = Join-Path ([Environment]::GetFolderPath('UserProfile')) '.cache/codex-local-delegation'
    }
    return [System.IO.Path]::GetFullPath($candidate)
}

function Initialize-LdStateRoot {
    param([Parameter(Mandatory)][string]$StateRoot)

    foreach ($relative in @('config', 'logs', 'codex-home', 'run', 'locks', 'tmp')) {
        [void](New-Item -ItemType Directory -Force -Path (Join-Path $StateRoot $relative))
    }
}

function Assert-LdLoopbackUri {
    param([Parameter(Mandatory)][string]$Url)

    $uri = $null
    if (-not [Uri]::TryCreate($Url, [UriKind]::Absolute, [ref]$uri)) {
        throw "Invalid absolute provider URL: $Url"
    }
    if ($uri.Scheme -ne 'http') {
        throw 'Version 1 accepts only http:// loopback provider URLs.'
    }
    $allowedHosts = @('127.0.0.1', 'localhost', '::1')
    if ($allowedHosts -notcontains $uri.Host.ToLowerInvariant()) {
        throw "Version 1 rejects non-loopback provider host '$($uri.Host)'."
    }
    if (-not [string]::IsNullOrEmpty($uri.Query) -or -not [string]::IsNullOrEmpty($uri.Fragment)) {
        throw 'Provider URLs must not contain a query string or fragment.'
    }
    return $uri
}

function Get-LdOriginAndResponsesBase {
    param([Parameter(Mandatory)][string]$BaseUrl)

    $uri = Assert-LdLoopbackUri -Url $BaseUrl
    $trimmed = $uri.AbsoluteUri.TrimEnd('/')
    $origin = $trimmed
    if ($uri.AbsolutePath.TrimEnd('/') -eq '/v1') {
        $origin = $trimmed.Substring(0, $trimmed.Length - 3)
    } elseif ($uri.AbsolutePath -ne '/') {
        throw 'Provider URL paths may be empty or /v1 only.'
    }
    [pscustomobject]@{
        Origin = $origin.TrimEnd('/')
        ResponsesBaseUrl = "$($origin.TrimEnd('/'))/v1"
    }
}

function New-LdHttpClient {
    param([int]$TimeoutSeconds = 10)

    $handler = [System.Net.Http.HttpClientHandler]::new()
    $handler.UseProxy = $false
    $client = [System.Net.Http.HttpClient]::new($handler)
    $client.Timeout = [TimeSpan]::FromSeconds($TimeoutSeconds)
    $client.DefaultRequestHeaders.Accept.ParseAdd('application/json')
    return $client
}

function Invoke-LdHttp {
    param(
        [Parameter(Mandatory)][ValidateSet('GET', 'POST')][string]$Method,
        [Parameter(Mandatory)][string]$Url,
        [object]$Body,
        [int]$TimeoutSeconds = 10
    )

    [void](Assert-LdLoopbackUri -Url $Url)
    $client = New-LdHttpClient -TimeoutSeconds $TimeoutSeconds
    $request = $null
    $response = $null
    try {
        $request = [System.Net.Http.HttpRequestMessage]::new(
            [System.Net.Http.HttpMethod]::new($Method),
            $Url
        )
        if ($PSBoundParameters.ContainsKey('Body')) {
            $json = $Body | ConvertTo-Json -Depth 32 -Compress
            $request.Content = [System.Net.Http.StringContent]::new(
                $json,
                [Text.Encoding]::UTF8,
                'application/json'
            )
        }
        $response = $client.Send($request)
        $content = $response.Content.ReadAsStringAsync().GetAwaiter().GetResult()
        [pscustomobject]@{
            IsSuccess = $response.IsSuccessStatusCode
            StatusCode = [int]$response.StatusCode
            ContentType = $response.Content.Headers.ContentType.MediaType
            Content = $content
        }
    } finally {
        if ($null -ne $response) { $response.Dispose() }
        if ($null -ne $request) { $request.Dispose() }
        $client.Dispose()
    }
}

function ConvertFrom-LdJsonResponse {
    param([Parameter(Mandatory)]$Response)

    if (-not $Response.IsSuccess) {
        $excerpt = $Response.Content
        if ($excerpt.Length -gt 300) { $excerpt = $excerpt.Substring(0, 300) }
        throw "HTTP $($Response.StatusCode): $excerpt"
    }
    try {
        return $Response.Content | ConvertFrom-Json -Depth 64
    } catch {
        throw "Provider returned invalid JSON: $($_.Exception.Message)"
    }
}

function Get-LdProviderProbe {
    param(
        [Parameter(Mandatory)][ValidateSet('Ollama', 'LlamaCpp', 'Custom')][string]$Provider,
        [Parameter(Mandatory)][string]$BaseUrl,
        [int]$TimeoutSeconds = 5
    )

    $urls = Get-LdOriginAndResponsesBase -BaseUrl $BaseUrl
    $models = @()
    if ($Provider -eq 'Ollama') {
        $version = ConvertFrom-LdJsonResponse (Invoke-LdHttp -Method GET -Url "$($urls.Origin)/api/version" -TimeoutSeconds $TimeoutSeconds)
        if ([string]::IsNullOrWhiteSpace([string]$version.version)) {
            throw 'Ollama identity response did not contain a version.'
        }
        $tags = ConvertFrom-LdJsonResponse (Invoke-LdHttp -Method GET -Url "$($urls.Origin)/api/tags" -TimeoutSeconds $TimeoutSeconds)
        $models = @($tags.models | ForEach-Object { [string]$_.name } | Where-Object { $_ })
        $identity = "Ollama $($version.version)"
    } else {
        if ($Provider -eq 'LlamaCpp') {
            $health = Invoke-LdHttp -Method GET -Url "$($urls.Origin)/health" -TimeoutSeconds $TimeoutSeconds
            if (-not $health.IsSuccess) {
                throw "llama.cpp health check returned HTTP $($health.StatusCode)."
            }
            $identity = 'llama.cpp-compatible server'
        } else {
            $identity = 'custom Responses-compatible server'
        }
        $listing = ConvertFrom-LdJsonResponse (Invoke-LdHttp -Method GET -Url "$($urls.ResponsesBaseUrl)/models" -TimeoutSeconds $TimeoutSeconds)
        $models = @($listing.data | ForEach-Object { [string]$_.id } | Where-Object { $_ })
    }

    [pscustomobject]@{
        Provider = $Provider
        Origin = $urls.Origin
        ResponsesBaseUrl = $urls.ResponsesBaseUrl
        Identity = $identity
        Models = @($models | Sort-Object -Unique)
    }
}

function ConvertTo-LdTomlString {
    param([Parameter(Mandatory)][string]$Value)
    return '"' + $Value.Replace('\', '\\').Replace('"', '\"') + '"'
}

function Write-LdUtf8File {
    param(
        [Parameter(Mandatory)][string]$Path,
        [Parameter(Mandatory)][AllowEmptyString()][string]$Content
    )

    $directory = Split-Path -Parent $Path
    [void](New-Item -ItemType Directory -Force -Path $directory)
    $temporary = Join-Path $directory ('.' + [IO.Path]::GetFileName($Path) + '.' + [Guid]::NewGuid().ToString('N') + '.tmp')
    try {
        [IO.File]::WriteAllText($temporary, $Content, [Text.UTF8Encoding]::new($false))
        Move-Item -LiteralPath $temporary -Destination $Path -Force
    } finally {
        if (Test-Path -LiteralPath $temporary) {
            Remove-Item -LiteralPath $temporary -Force
        }
    }
}

function Get-LdProviderConfigPath {
    param([Parameter(Mandatory)][string]$StateRoot)
    return Join-Path $StateRoot 'config/provider.json'
}

function Read-LdProviderConfig {
    param([Parameter(Mandatory)][string]$StateRoot)

    $path = Get-LdProviderConfigPath -StateRoot $StateRoot
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
        throw "Provider configuration not found: $path. Run configure-provider.ps1 first."
    }
    return Get-Content -Raw -LiteralPath $path | ConvertFrom-Json -Depth 32
}

function Get-LdSha256Text {
    param([Parameter(Mandatory)][string]$Value)
    $bytes = [Text.Encoding]::UTF8.GetBytes($Value)
    $hash = [Security.Cryptography.SHA256]::HashData($bytes)
    return [Convert]::ToHexString($hash).ToLowerInvariant()
}
