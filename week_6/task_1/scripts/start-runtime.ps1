param([string]$Version = '0.40.3', [ValidateRange(1024,65535)][int]$Port = 11434)
$ErrorActionPreference = 'Stop'
$taskRoot = Split-Path $PSScriptRoot -Parent
$exe = Join-Path $taskRoot ".runtime/ollama-v$Version/ollama.exe"
if (-not (Test-Path $exe)) { throw 'Run scripts/install-runtime.ps1 first.' }
$env:OLLAMA_HOST = "127.0.0.1:$Port"
$env:OLLAMA_MODELS = Join-Path $taskRoot '.models'
$env:OLLAMA_NO_CLOUD = '1'
$env:OLLAMA_CONTEXT_LENGTH = '8192'
$env:OLLAMA_NUM_PARALLEL = '1'
$env:OLLAMA_MAX_QUEUE = '4'
$env:OLLAMA_KEEP_ALIVE = '10m'
$stateRoot = Join-Path $taskRoot 'data'
if ($Port -ne 11434) { $stateRoot = Join-Path $stateRoot "runtime-$Port" }
New-Item -ItemType Directory -Force $stateRoot,$env:OLLAMA_MODELS | Out-Null
$listener = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Loopback, $Port)
$listener.Server.ExclusiveAddressUse = $true
try { $listener.Start() }
catch { throw "Port $Port is unavailable. Stop or verify its existing service before starting this runtime." }
finally { $listener.Stop() }
$process = Start-Process -FilePath $exe -ArgumentList 'serve' -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $stateRoot 'ollama.stdout.log') -RedirectStandardError (Join-Path $stateRoot 'ollama.stderr.log')
$endpoint = "http://127.0.0.1:$Port"
$deadline = [DateTime]::UtcNow.AddSeconds(30)
$ready = $false
while ([DateTime]::UtcNow -lt $deadline) {
    $process.Refresh()
    if ($process.HasExited) { throw "Ollama exited before readiness (exit $($process.ExitCode)). See data/ollama.stderr.log." }
    try {
        $v = Invoke-RestMethod -Uri "$endpoint/api/version" -TimeoutSec 2
        if ($v.version -eq $Version) { $ready = $true; break }
    } catch { }
    Start-Sleep -Milliseconds 200
}
if (-not $ready) {
    $process.Refresh()
    if (-not $process.HasExited) { $process.Kill(); $process.WaitForExit() }
    throw 'Ollama readiness timed out. See data/ollama.stderr.log.'
}
$process.Refresh()
if ($process.HasExited) { throw 'Ollama exited during readiness. See data/ollama.stderr.log.' }
$process.Id | Set-Content -LiteralPath (Join-Path $stateRoot 'ollama.pid') -Encoding ASCII
Write-Output "Ollama ready; PID=$($process.Id); API=$endpoint; cloud disabled; models=$env:OLLAMA_MODELS"
