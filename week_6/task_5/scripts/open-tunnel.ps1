param(
    [Parameter(Mandatory = $true)][ValidatePattern('^[A-Za-z0-9][A-Za-z0-9.-]*$')][string]$Server,
    [Parameter(Mandatory = $true)][ValidatePattern('^[A-Za-z_][A-Za-z0-9_.-]*$')][string]$User,
    [Parameter(Mandatory = $true)][string]$IdentityFile,
    [Parameter(Mandatory = $true)][string]$KnownHostsFile,
    [ValidateRange(1024, 65535)][int]$LocalPort = 5030
)

$ErrorActionPreference = 'Stop'
$ssh = (Get-Command ssh.exe -CommandType Application -ErrorAction Stop).Source
$identity = (Resolve-Path -LiteralPath $IdentityFile -ErrorAction Stop).Path
$knownHosts = (Resolve-Path -LiteralPath $KnownHostsFile -ErrorAction Stop).Path
$forward = "127.0.0.1:${LocalPort}:127.0.0.1:5030"

Write-Host "SSH forwarding localhost:${LocalPort} to the private API. Stop with Ctrl+C."
& $ssh -N -T -L $forward -i $identity `
    -o BatchMode=yes `
    -o ConnectTimeout=10 `
    -o ConnectionAttempts=1 `
    -o ExitOnForwardFailure=yes `
    -o IdentitiesOnly=yes `
    -o StrictHostKeyChecking=yes `
    -o "UserKnownHostsFile=$knownHosts" `
    -o UpdateHostKeys=no `
    -o ServerAliveInterval=15 `
    -o ServerAliveCountMax=2 `
    "${User}@${Server}"
exit $LASTEXITCODE
