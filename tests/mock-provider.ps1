[CmdletBinding()]
param(
    [Parameter(Mandatory)][int]$Port,
    [Parameter(Mandatory)][string]$ReadyFile,
    [int]$MaxRequests = 5
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$listener = [Net.Sockets.TcpListener]::new([Net.IPAddress]::Loopback, $Port)
try {
    $listener.Start()
    [IO.File]::WriteAllText($ReadyFile, 'ready', [Text.UTF8Encoding]::new($false))
    for ($requestNumber = 0; $requestNumber -lt $MaxRequests; $requestNumber++) {
        $client = $listener.AcceptTcpClient()
        try {
            $stream = $client.GetStream()
            $reader = [IO.StreamReader]::new($stream, [Text.UTF8Encoding]::new($false), $false, 4096, $true)
            $requestLine = $reader.ReadLine()
            if ([string]::IsNullOrWhiteSpace($requestLine)) { continue }
            $parts = $requestLine.Split(' ')
            $method = $parts[0]
            $path = $parts[1]
            $contentLength = 0
            while ($true) {
                $header = $reader.ReadLine()
                if ([string]::IsNullOrEmpty($header)) { break }
                if ($header -match '^Content-Length:\s*(\d+)$') {
                    $contentLength = [int]$Matches[1]
                }
            }
            $body = ''
            if ($contentLength -gt 0) {
                $buffer = [char[]]::new($contentLength)
                $offset = 0
                while ($offset -lt $contentLength) {
                    $read = $reader.Read($buffer, $offset, $contentLength - $offset)
                    if ($read -le 0) { break }
                    $offset += $read
                }
                $body = [string]::new($buffer, 0, $offset)
            }

            $contentType = 'application/json'
            if ($method -eq 'GET' -and $path -eq '/v1/models') {
                $responseBody = '{"object":"list","data":[{"id":"local-test-model","object":"model"}]}'
            } elseif ($method -eq 'POST' -and $path -eq '/v1/responses') {
                $payload = $body | ConvertFrom-Json -Depth 32
                if ($payload.stream -eq $true) {
                    $contentType = 'text/event-stream'
                    $responseBody = "event: response.output_text.delta`ndata: {`"type`":`"response.output_text.delta`",`"delta`":`"ready`"}`n`ndata: [DONE]`n`n"
                } elseif (@($payload.input | Where-Object {
                    $_ -isnot [string] -and
                    $_.PSObject.Properties.Name -contains 'type' -and
                    $_.type -eq 'function_call_output'
                }).Count -gt 0) {
                    $responseBody = '{"id":"resp-2","object":"response","status":"completed","output":[{"type":"message","role":"assistant","content":[{"type":"output_text","text":"pong"}]}]}'
                } else {
                    $responseBody = '{"id":"resp-1","object":"response","status":"completed","output":[{"type":"function_call","name":"local_delegate_doctor_echo","call_id":"call-1","arguments":"{\"value\":\"ping\"}"}]}'
                }
            } else {
                $responseBody = '{"error":"not found"}'
            }

            $responseBytes = [Text.Encoding]::UTF8.GetBytes($responseBody)
            $statusLine = if ($responseBody -eq '{"error":"not found"}') { 'HTTP/1.1 404 Not Found' } else { 'HTTP/1.1 200 OK' }
            $headers = "$statusLine`r`nContent-Type: $contentType`r`nContent-Length: $($responseBytes.Length)`r`nConnection: close`r`n`r`n"
            $headerBytes = [Text.Encoding]::ASCII.GetBytes($headers)
            $stream.Write($headerBytes, 0, $headerBytes.Length)
            $stream.Write($responseBytes, 0, $responseBytes.Length)
            $stream.Flush()
            $reader.Dispose()
        } finally {
            $client.Dispose()
        }
    }
} finally {
    $listener.Stop()
}
