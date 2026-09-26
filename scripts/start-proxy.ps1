<#
    Starts muse-coder-proxy in the background.

    Why this exists: Bert (an external Muse agent) reaches Coder workspaces
    on this machine only through muse-coder-proxy (see ../DESIGN.md) - it is
    the trust boundary between Bert and this homelab. The proxy needs to be
    running any time this machine is logged in, so this script is invoked at
    Windows login by a stub in the Startup folder:
        %APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\muse-coder-proxy.cmd
    That stub is NOT version controlled (the Startup folder is a per-machine
    OS artifact) - it just calls this script. This script IS version
    controlled, so it's what to read after a rebuild.

    If this machine is rebuilt, to bring the proxy back:
      1. Re-clone this repo.
      2. Copy config.example.toml to config.toml and edit it (workspace
         allowlist, ports, etc. - see DESIGN.md).
      3. Set the two secrets as persistent per-user environment variables
         (never put these in a file in the repo):
             setx MUSE_PROXY_BEARER_TOKEN "<token Bert authenticates with>"
             setx MUSE_CODER_TOKEN "<a Coder session token - see DESIGN.md's
                 'Coder token for the proxy' section for why this is a
                 dedicated token, not the interactive user's own>"
         setx writes to the registry; a new login session is needed to pick
         it up (that's fine, since this script only ever runs at login).
      4. Recreate the Startup folder stub - copy scripts\muse-coder-proxy.cmd
         from this repo into the Startup folder above.
#>

$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
$logDir = Join-Path $repoRoot "logs"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
# The running proxy process holds $stdout/$stderr open for its own
# redirected output, so this script logs its own messages (start attempts,
# the dupe-guard below) to a separate file - writing to $stdout here would
# collide with that open handle and fail.
$launcherLog = Join-Path $logDir "launcher.log"
$stdout = Join-Path $logDir "proxy.out.log"
$stderr = Join-Path $logDir "proxy.err.log"

# Guard against starting a second copy - e.g. if this script is re-run by
# hand while the login-time instance is still up. Requires BOTH a python
# process name AND "proxy.py" on its command line - matching on command
# line text alone is a false-positive trap, since anything that merely
# *mentions* "proxy.py" (like this very query, run from a shell) matches
# itself. The name check is a prefix match ("python*") since the real
# process name varies by interpreter build (e.g. python3.13.exe).
$already = Get-CimInstance Win32_Process |
    Where-Object { $_.Name -like "python*" -and $_.CommandLine -like "*proxy.py*" }
if ($already) {
    "$(Get-Date -Format o) already running (pid $($already.ProcessId -join ',')), not starting another" |
        Out-File -FilePath $launcherLog -Append -Encoding utf8
    return
}

"$(Get-Date -Format o) starting muse-coder-proxy" | Out-File -FilePath $launcherLog -Append -Encoding utf8

# -u: unbuffered, so these logs aren't stuck in Python's stdio buffer indefinitely.
Start-Process -FilePath "python" `
    -ArgumentList @("-u", "proxy.py") `
    -WorkingDirectory $repoRoot `
    -WindowStyle Hidden `
    -RedirectStandardOutput $stdout `
    -RedirectStandardError $stderr
