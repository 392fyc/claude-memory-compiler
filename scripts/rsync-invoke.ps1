# rsync-invoke.ps1 — cwRsync invocation helper for rsync-agentkb-to-nas.sh
#
# WHY THIS EXISTS:
#   cwRsync is a Cygwin binary. When called from Git Bash (MSYS2), the
#   dup() syscall fails due to Cygwin/MSYS2 file-descriptor incompatibility.
#   Calling cwRsync from PowerShell (native Windows handles) avoids this.
#
# OFF-LAN FALLBACK:
#   If 192.168.0.254:22 is unreachable (LAN not available), falls back to
#   Windows system ssh.exe with ssh.fyc-space.uk (Cloudflare Tunnel).
#   Requires cloudflared in PATH and ~/.ssh/config ProxyCommand entry for
#   ssh.fyc-space.uk.
#
# HOW IT'S CALLED:
#   MSYS_NO_PATHCONV=1 powershell.exe -NonInteractive -ExecutionPolicy Bypass \
#     -File "$AGENTKB_DIR/scripts/rsync-invoke.ps1"
#   stdout+stderr are redirected by the caller to rsync.log.
#
# Returns cwRsync exit code (0 = success).

param()

# Derive AgentKB root from this script's location
$agentKBDir = Split-Path -Parent $PSScriptRoot

# Find cwRsync binary (scoop installs to apps\cwrsync\current\bin\)
$cwrsyncBin = $env:USERPROFILE + '\scoop\apps\cwrsync\current\bin'
if (-not (Test-Path "$cwrsyncBin\rsync.exe")) {
    # Fallback: search versioned dir
    $found = Get-ChildItem "$env:USERPROFILE\scoop\apps\cwrsync" -Recurse -Filter 'rsync.exe' |
             Select-Object -First 1
    if ($found) { $cwrsyncBin = $found.DirectoryName }
    else {
        Write-Error "cwRsync rsync.exe not found under $env:USERPROFILE\scoop\apps\cwrsync"
        exit 1
    }
}
$rsyncExe = "$cwrsyncBin\rsync.exe"
$cwSshExe = "$cwrsyncBin\ssh.exe"

# Convert Windows path to Cygwin /cygdrive/ format
# e.g. C:\Users\foo -> /cygdrive/c/Users/foo
function ConvertTo-CygwinPath([string]$p) {
    $p = $p -replace '\\', '/'
    if ($p -match '^([A-Za-z]):(.*)') {
        return '/cygdrive/' + $Matches[1].ToLower() + $Matches[2]
    }
    return $p
}

$src     = (ConvertTo-CygwinPath $agentKBDir) + '/'
$excFile = ConvertTo-CygwinPath ($agentKBDir + '\scripts\rsync-exclude.list')
$sshKey  = ConvertTo-CygwinPath ($env:USERPROFILE + '\.ssh\id_ed25519')

# ── LAN probe ─────────────────────────────────────────────────────────────────
# Test-NetConnection suppresses its own warning output to keep logs clean.
$lanReachable = $false
try {
    $probe = Test-NetConnection -ComputerName 192.168.0.254 -Port 22 `
             -InformationLevel Quiet -WarningAction SilentlyContinue `
             -ErrorAction SilentlyContinue
    $lanReachable = [bool]$probe
} catch {
    $lanReachable = $false
}

if ($lanReachable) {
    Write-Host "rsync-invoke: LAN reachable — direct connection to 192.168.0.254"
    # Forward slashes + quotes for consistency with off-LAN branch.
    $cwSshFwd     = $cwSshExe -replace '\\', '/'
    $remote       = '392fyc@192.168.0.254:/share/CACHEDEV1_DATA/AgentKB/'
    $sshTransport = "`"$cwSshFwd`" -i `"$sshKey`" -o BatchMode=yes -o ConnectTimeout=10 -o StrictHostKeyChecking=accept-new"
} else {
    Write-Host "rsync-invoke: LAN not reachable — off-LAN via ssh.fyc-space.uk (CF Tunnel)"
    $winSsh = "$env:SystemRoot\System32\OpenSSH\ssh.exe"
    if (-not (Test-Path $winSsh)) {
        Write-Error "Windows OpenSSH not found at $winSsh — cannot sync off-LAN"
        exit 1
    }
    if (-not (Get-Command cloudflared -ErrorAction SilentlyContinue)) {
        Write-Warning "cloudflared not found in PATH — CF Tunnel ProxyCommand for ssh.fyc-space.uk will fail"
    }
    # Windows ssh.exe reads ~/.ssh/config natively, which holds the
    # ProxyCommand for ssh.fyc-space.uk (Cloudflare Tunnel via cloudflared).
    # Forward slashes prevent rsync from treating backslashes as escape sequences
    # when it tokenizes the -e string. Paths are quoted for space-safety.
    $winSshFwd    = $winSsh -replace '\\', '/'
    $sshKeyWin    = (($env:USERPROFILE + '\.ssh\id_ed25519') -replace '\\', '/')
    $remote       = '392fyc@ssh.fyc-space.uk:/share/CACHEDEV1_DATA/AgentKB/'
    $sshTransport = "`"$winSshFwd`" -i `"$sshKeyWin`" -o BatchMode=yes -o ConnectTimeout=30 -o StrictHostKeyChecking=accept-new"
}

& $rsyncExe -az --delete --omit-dir-times --no-perms --no-owner --no-group `
    "--exclude-from=$excFile" `
    -e $sshTransport `
    $src `
    $remote

exit $LASTEXITCODE
