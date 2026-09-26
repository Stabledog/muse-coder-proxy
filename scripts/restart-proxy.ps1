<#
    Restarts muse-coder-proxy so it picks up a changed config.toml.

    Why this exists: the proxy reads config.toml once, at startup, and
    start-proxy.ps1 deliberately refuses to launch a second copy while one is
    running. So "apply a config change" means: stop the running proxy, then
    run start-proxy.ps1. This script does both and shows the result, so
    nobody has to remember the process-matching incantation.

    Run it from a shell that has MUSE_PROXY_BEARER_TOKEN and MUSE_CODER_TOKEN
    in its environment (i.e. a shell opened after the setx - see the rebuild
    notes in start-proxy.ps1). The restarted proxy inherits this shell's
    environment.

    Caveat: stopping the proxy kills any /exec call in flight; the client
    sees a dropped connection. Pick a quiet moment.
#>

$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
$stdout = Join-Path $repoRoot "logs\proxy.out.log"
$stderr = Join-Path $repoRoot "logs\proxy.err.log"

# Same match rule as the dupe-guard in start-proxy.ps1: a python process AND
# "proxy.py" on its command line. Name alone would hit unrelated pythons;
# command line alone would match this very query.
function Get-ProxyProcess {
    Get-CimInstance Win32_Process |
        Where-Object { $_.Name -like "python*" -and $_.CommandLine -like "*proxy.py*" }
}

# Fail before stopping anything if the secrets are missing, so a bad shell
# can't leave the proxy down.
foreach ($name in "MUSE_PROXY_BEARER_TOKEN", "MUSE_CODER_TOKEN") {
    if (-not [Environment]::GetEnvironmentVariable($name, "Process")) {
        throw "$name is not set in this shell's environment; open a new shell (or refresh the environment) and retry. Proxy left untouched."
    }
}

$old = @(Get-ProxyProcess)
if ($old.Count -gt 0) {
    Write-Host "Stopping proxy (pid $($old.ProcessId -join ','))..."
    $old | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
    # The port and log files are freed only once the process is really gone.
    $deadline = (Get-Date).AddSeconds(10)
    while ((Get-ProxyProcess) -and (Get-Date) -lt $deadline) { Start-Sleep -Milliseconds 200 }
    if (Get-ProxyProcess) { throw "Old proxy process did not exit within 10s." }
} else {
    Write-Host "No proxy running; starting one."
}

& (Join-Path $PSScriptRoot "start-proxy.ps1")

# Give it a moment: config errors and port-in-use failures happen right away.
Start-Sleep -Seconds 2

$new = @(Get-ProxyProcess)
if ($new.Count -eq 0) {
    Write-Host "PROXY DID NOT START. stderr:" -ForegroundColor Red
    if (Test-Path $stderr) { Get-Content $stderr -Tail 20 }
    exit 1
}

Write-Host "Proxy running (pid $($new.ProcessId -join ','))." -ForegroundColor Green
Get-Content $stdout
