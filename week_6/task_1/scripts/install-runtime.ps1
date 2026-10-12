param([string]$Version = '0.40.3')
$ErrorActionPreference = 'Stop'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$taskRoot = Split-Path $PSScriptRoot -Parent
$runtimeRoot = Join-Path $taskRoot '.runtime'
$target = Join-Path $runtimeRoot "ollama-v$Version"
if (Test-Path (Join-Path $target 'ollama.exe')) {
    Write-Output "Runtime already present: $target"
    exit 0
}
New-Item -ItemType Directory -Force $runtimeRoot | Out-Null
$release = Invoke-RestMethod "https://api.github.com/repos/ollama/ollama/releases/tags/v$Version"
$asset = $release.assets | Where-Object name -eq 'ollama-windows-amd64.zip'
if (-not $asset) { throw 'Official Windows archive not found.' }
$archive = Join-Path $runtimeRoot "ollama-windows-amd64-v$Version.zip"
if (-not (Test-Path $archive)) {
    & curl.exe -L --fail --show-error $asset.browser_download_url -o $archive
    if ($LASTEXITCODE -ne 0) { throw 'Archive download failed.' }
}
$hash = (Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash.ToLowerInvariant()
if ($asset.digest -and $asset.digest -ne "sha256:$hash") { throw 'Archive checksum does not match official release digest.' }
Expand-Archive -LiteralPath $archive -DestinationPath $target
Write-Output "Installed portable Ollama v$Version; SHA256=$hash"
