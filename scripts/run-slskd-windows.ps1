<#
.SYNOPSIS
    Install (if needed) and run slskd on Windows (x64).

.DESCRIPTION
    slskd is the Soulseek client that download_wishlist.py talks to. On first run
    this downloads the binary; on later runs it just launches it. Either way it
    prompts for your Soulseek username/password, starts slskd with them, and
    verifies the login works before handing off.

    The same username/password are used for BOTH the Soulseek network login and
    the slskd web login — so pass them to the wishlist script as
    --slskd-username / --slskd-password.

.PARAMETER Version
    Release tag to (re)install. Defaults to the latest release (or $env:SLSKD_VERSION).

.PARAMETER InstallDir
    Where the binary lives. Defaults to $env:USERPROFILE\slskd (or $env:SLSKD_INSTALL_DIR).

.PARAMETER Port
    slskd web port. Defaults to 5030 (or $env:SLSKD_PORT).

.EXAMPLE
    .\run-slskd-windows.ps1

.NOTES
    If PowerShell blocks the script, allow it for this session with:
        Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
#>
[CmdletBinding()]
param(
    [string]$Version = $env:SLSKD_VERSION,
    [string]$InstallDir = $(if ($env:SLSKD_INSTALL_DIR) { $env:SLSKD_INSTALL_DIR } else { Join-Path $env:USERPROFILE 'slskd' }),
    [int]$Port = $(if ($env:SLSKD_PORT) { [int]$env:SLSKD_PORT } else { 5030 })
)

$ErrorActionPreference = 'Stop'
$repo = 'slskd/slskd'
$archTag = 'win-x64'
$bin = Join-Path $InstallDir 'slskd.exe'
# IPv4 loopback, not "localhost": slskd binds IPv6 dual-stack, and connecting over the
# IPv6 loopback (::1, which "localhost" can resolve to) gets reset. Using 127.0.0.1 keeps
# both the login check and the printed --slskd-url reliable.
$baseUrl = "http://127.0.0.1:$Port"

# Where slskd writes finished downloads. We point slskd at this dir so the user
# configures ONE directory (their Subbox watch dir) and finished tracks land straight in
# it - the wishlist script never needs it. Resolved highest-priority-first:
# the SLSKD_DOWNLOADS_DIR env override, the cached value, or an interactive prompt that
# defaults to <install-dir>\downloads. Finalised by the credential flow below.
$defaultDownloadsDir = Join-Path $InstallDir 'downloads'
$downloadsDir = $env:SLSKD_DOWNLOADS_DIR

# Which directory slskd SHARES back to the Soulseek network. Soulseek peers commonly
# refuse to queue downloads for users who share nothing, so we prompt for a folder to
# share. Resolved like $downloadsDir: the SLSKD_SHARE_DIR env override, the cached value,
# or an interactive prompt that defaults to the downloads dir. Finalised below.
$shareDir = $env:SLSKD_SHARE_DIR

# Credentials are cached here (alongside the script) so later runs skip the prompt.
# Plaintext, so it's locked down to the current user and git-ignored; removed if a
# login is rejected.
$credFile = $(if ($env:SLSKD_CRED_FILE) { $env:SLSKD_CRED_FILE } else { Join-Path $PSScriptRoot 'slskd-credentials.env' })

function Import-SavedCredentials {
    if (-not (Test-Path $credFile)) { return $null }
    $u = $null; $p = $null; $d = $null; $s = $null
    foreach ($line in Get-Content $credFile) {
        if ($line -match '^\s*SLSKD_RUN_USERNAME=(.*)$') { $u = $Matches[1] }
        elseif ($line -match '^\s*SLSKD_RUN_PASSWORD=(.*)$') { $p = $Matches[1] }
        elseif ($line -match '^\s*SLSKD_RUN_DOWNLOADS_DIR=(.*)$') { $d = $Matches[1] }
        elseif ($line -match '^\s*SLSKD_RUN_SHARE_DIR=(.*)$') { $s = $Matches[1] }
    }
    if ($u -and $p) { return @{ Username = $u; Password = $p; DownloadsDir = $d; ShareDir = $s } }
    return $null
}

function Save-Credentials {
    param($u, $p, $d, $s)
    @("SLSKD_RUN_USERNAME=$u", "SLSKD_RUN_PASSWORD=$p", "SLSKD_RUN_DOWNLOADS_DIR=$d", "SLSKD_RUN_SHARE_DIR=$s") |
        Set-Content -Path $credFile -Encoding ascii
    # Best-effort: restrict to the current user only.
    try { icacls $credFile /inheritance:r /grant:r "$($env:USERNAME):F" | Out-Null } catch {}
}

# Prompt for the directory finished downloads go into — your Subbox watch dir, so tracks
# import automatically. Defaults to $default (the cached dir, else <install-dir>\downloads).
function Read-DownloadsDir {
    param($default)
    Write-Host ''
    Write-Host 'Where should finished downloads go? Use your Subbox watch directory so tracks'
    Write-Host 'import automatically - slskd will download straight into it.'
    $d = Read-Host "  Download dir [$default]"
    if (-not $d) { $d = $default }
    return $d
}

# Prompt for a directory to SHARE on Soulseek. Sharing is effectively required: many peers
# won't queue downloads for users who share nothing. Defaults to $default (the cached dir,
# else the downloads dir) so finished tracks are shared back automatically.
function Read-ShareDir {
    param($default)
    Write-Host ''
    Write-Host 'Which directory should you SHARE on Soulseek? Soulseek often blocks or throttles'
    Write-Host 'downloads for users who do not share anything back, so pick a folder of music to'
    Write-Host 'share - your downloads dir is a fine default.'
    $s = Read-Host "  Share dir [$default]"
    if (-not $s) { $s = $default }
    return $s
}

function Install-Slskd {
    if (-not $Version) {
        Write-Host 'Resolving latest slskd release...'
        $script:Version = (Invoke-RestMethod "https://api.github.com/repos/$repo/releases/latest").tag_name
    }
    if (-not $Version) {
        throw "could not determine slskd version (pass -Version, e.g. -Version 0.25.1)"
    }

    $asset = "slskd-$Version-$archTag.zip"
    $url = "https://github.com/$repo/releases/download/$Version/$asset"

    Write-Host "Installing slskd $Version ($archTag) into $InstallDir"
    New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
    $tmpZip = Join-Path $env:TEMP $asset

    Write-Host "Downloading $url"
    Invoke-WebRequest -Uri $url -OutFile $tmpZip
    Write-Host 'Unzipping...'
    Expand-Archive -Path $tmpZip -DestinationPath $InstallDir -Force
    Remove-Item $tmpZip -Force

    if (-not (Test-Path $bin)) { throw "slskd.exe not found after unzip in $InstallDir" }
    # Clear the Mark-of-the-Web so SmartScreen doesn't block the downloaded binary.
    Unblock-File -Path $bin
    Write-Host "OK  Installed slskd $Version at $bin"
}

function Read-Credentials {
    Write-Host ''
    Write-Host 'Enter your Soulseek credentials (a free account - if it does not exist yet,'
    Write-Host 'slskd registers it on first connect). These are reused as the slskd web login.'
    do {
        $u = Read-Host '  Username'
        if (-not $u) { Write-Host '  (username can''t be empty)' }
    } while (-not $u)
    do {
        $sec = Read-Host '  Password' -AsSecureString
        $p = [System.Net.NetworkCredential]::new('', $sec).Password
        if (-not $p) { Write-Host '  (password can''t be empty)' }
    } while (-not $p)
    return @{ Username = $u; Password = $p }
}

# Returns the HTTP status of a login attempt: 200 ok, 401 rejected, 0 not up yet.
function Get-LoginStatus {
    param($base, $u, $p)
    try {
        $body = @{ username = $u; password = $p } | ConvertTo-Json
        $r = Invoke-WebRequest -Uri "$base/api/v0/session" -Method Post `
            -ContentType 'application/json' -Body $body -TimeoutSec 5 -UseBasicParsing
        return [int]$r.StatusCode
    } catch {
        if ($_.Exception.Response) { return [int]$_.Exception.Response.StatusCode }
        return 0
    }
}

function Start-SlskdAndVerify {
    param($u, $p)
    $log = Join-Path $InstallDir 'slskd-run.log'
    $errLog = Join-Path $InstallDir 'slskd-run.err.log'
    $env:SLSKD_SLSK_USERNAME = $u; $env:SLSKD_SLSK_PASSWORD = $p
    $env:SLSKD_USERNAME = $u;      $env:SLSKD_PASSWORD = $p

    New-Item -ItemType Directory -Force -Path $downloadsDir | Out-Null
    New-Item -ItemType Directory -Force -Path $shareDir | Out-Null

    Write-Host ''
    Write-Host 'Starting slskd...'
    $proc = Start-Process -FilePath $bin -PassThru -NoNewWindow `
        -ArgumentList '--downloads', $downloadsDir, '--shared', $shareDir `
        -RedirectStandardOutput $log -RedirectStandardError $errLog

    try {
        $ok = $false
        for ($i = 0; $i -lt 30; $i++) {
            if ($proc.HasExited) {
                Write-Error "slskd exited unexpectedly. Last log lines:"
                Get-Content $log, $errLog -Tail 20 -ErrorAction SilentlyContinue
                return
            }
            $code = Get-LoginStatus $baseUrl $u $p
            if ($code -eq 200) { $ok = $true; break }
            if ($code -eq 401) {
                Remove-Item $credFile -ErrorAction SilentlyContinue
                Write-Error "slskd rejected that username/password (HTTP 401). Removed saved credentials ($credFile). Re-run to try again."
                return
            }
            Start-Sleep -Seconds 1
        }
        if (-not $ok) {
            Write-Error 'slskd did not become ready in time. Last log lines:'
            Get-Content $log, $errLog -Tail 20 -ErrorAction SilentlyContinue
            return
        }

        Write-Host 'OK  slskd is running and your login works.'
        Write-Host "    Web UI:    $baseUrl"
        Write-Host "    Logs:      $log"
        Write-Host "    Downloads: $downloadsDir"
        Write-Host "    Sharing:   $shareDir"
        Write-Host "    Wishlist: python download_wishlist.py --slskd-url $baseUrl ``"
        Write-Host "                  --slskd-username '$u' --slskd-password <same password>"
        Write-Host "              (finished tracks land in $downloadsDir - slskd writes them; no dir flag needed)"
        Write-Host ''
        Write-Host "Watch the log for 'logged in' to confirm the Soulseek connection."
        Write-Host 'Leave this window open. Press Ctrl-C to stop slskd.'
        Write-Host '------------------------------------------------------------------------'
        Get-Content $log -Wait
    }
    finally {
        if (-not $proc.HasExited) { Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue }
    }
}

# --- main ---
if (Test-Path $bin) {
    Write-Host "slskd already installed at $bin (set -Version + delete it to upgrade)."
} else {
    Install-Slskd
}

$cred = Import-SavedCredentials
if ($cred) {
    Write-Host "Using saved credentials for '$($cred.Username)' from $credFile"
    Write-Host "  (delete that file to re-enter; it's auto-removed if the login is rejected)"
    # Older caches predate the download-dir / share-dir prompts; ask for whichever is
    # missing and re-save (unless the matching env override is in play, which wins).
    $needSave = $false
    if (-not $downloadsDir -and -not $cred.DownloadsDir) {
        $cred.DownloadsDir = Read-DownloadsDir $defaultDownloadsDir
        $needSave = $true
    }
    if (-not $shareDir -and -not $cred.ShareDir) {
        $shareDefault = if ($downloadsDir) { $downloadsDir } elseif ($cred.DownloadsDir) { $cred.DownloadsDir } else { $defaultDownloadsDir }
        $cred.ShareDir = Read-ShareDir $shareDefault
        $needSave = $true
    }
    if ($needSave) { Save-Credentials $cred.Username $cred.Password $cred.DownloadsDir $cred.ShareDir }
} else {
    $cred = Read-Credentials
    $cred.DownloadsDir = Read-DownloadsDir $defaultDownloadsDir
    $shareDefault = if ($downloadsDir) { $downloadsDir } else { $cred.DownloadsDir }
    $cred.ShareDir = Read-ShareDir $shareDefault
    Save-Credentials $cred.Username $cred.Password $cred.DownloadsDir $cred.ShareDir
    Write-Host "Saved credentials to $credFile - future runs won't prompt."
}
# Env override always wins; otherwise the cached/prompted dir, then the default.
if (-not $downloadsDir) {
    $downloadsDir = if ($cred.DownloadsDir) { $cred.DownloadsDir } else { $defaultDownloadsDir }
}
# Share dir falls back to the downloads dir so the user always shares something.
if (-not $shareDir) {
    $shareDir = if ($cred.ShareDir) { $cred.ShareDir } else { $downloadsDir }
}
Start-SlskdAndVerify $cred.Username $cred.Password
