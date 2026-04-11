# rsync-invoke.ps1 — cwRsync invocation helper for rsync-agentkb-to-nas.sh
#
# WHY THIS EXISTS:
#   cwRsync is a Cygwin binary. When called from Git Bash (MSYS2), the
#   dup() syscall fails due to Cygwin/MSYS2 file-descriptor incompatibility.
#   Calling cwRsync from PowerShell (native Windows handles) avoids this.
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
$sshExe   = "$cwrsyncBin\ssh.exe"

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
$remote  = '392fyc@192.168.0.254:/share/CACHEDEV1_DATA/AgentKB/'

& $rsyncExe -az --delete --omit-dir-times --no-perms --no-owner --no-group `
    "--exclude-from=$excFile" `
    -e "$sshExe -i $sshKey -o BatchMode=yes -o ConnectTimeout=10 -o StrictHostKeyChecking=accept-new" `
    $src `
    $remote

exit $LASTEXITCODE
