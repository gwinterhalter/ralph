<#
.SYNOPSIS
  Verify the control panel the way the OPERATOR launches it: `python -m webui.server` in PowerShell,
  then HTTP probes against http://127.0.0.1:<port>/ — the real process + the real DB, not TestClient.

.DESCRIPTION
  Reproduces the operator's exact launch path (the gap that hid the earlier "/ -> 404" and
  "site can't be reached" failures). Starts the server, waits for readiness, asserts the UI is served
  at / and the API endpoints respond over the real Registry/DB, then stops the server. Exit 0 = pass.

  Run:  pwsh -File webui\verify_launch.ps1            # uses Machine/User-scope OL_SUPERVISOR_DB_URL
        pwsh -File webui\verify_launch.ps1 -Port 8795
#>
param([int]$Port = 8787)

$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot           # ...\Ralph-dev
$base = "http://127.0.0.1:$Port"

# Resolve the DB URL (process env, else Machine, else User) and refuse the production ref.
if (-not $env:OL_SUPERVISOR_DB_URL) {
  $env:OL_SUPERVISOR_DB_URL = [Environment]::GetEnvironmentVariable("OL_SUPERVISOR_DB_URL","Machine")
  if (-not $env:OL_SUPERVISOR_DB_URL) {
    $env:OL_SUPERVISOR_DB_URL = [Environment]::GetEnvironmentVariable("OL_SUPERVISOR_DB_URL","User")
  }
}
if (-not $env:OL_SUPERVISOR_DB_URL) { Write-Error "OL_SUPERVISOR_DB_URL is not set."; exit 1 }
if ($env:OL_SUPERVISOR_DB_URL -match "eybdbshxswutgaaylpol") { Write-Error "REFUSING: production DSN."; exit 1 }

# Free the port if something is already bound (e.g. a prior run).
Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue |
  ForEach-Object { Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue }

$env:PYTHONPATH = $repo
$env:PYTHONIOENCODING = "utf-8"
$log = Join-Path $env:TEMP "webui_verify_$Port.log"
$proc = Start-Process -FilePath "python" -ArgumentList @("-m","webui.server","--port","$Port") `
  -WorkingDirectory $repo -RedirectStandardOutput $log -RedirectStandardError "$log.err" `
  -PassThru -WindowStyle Hidden

function Stop-Server { if ($proc -and -not $proc.HasExited) { Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue } }

try {
  # Wait for readiness.
  $up = $false
  for ($i = 0; $i -lt 20; $i++) {
    try { if ((Invoke-WebRequest "$base/api/health" -UseBasicParsing -TimeoutSec 2).StatusCode -eq 200) { $up = $true; break } }
    catch { Start-Sleep -Milliseconds 500 }
  }
  if (-not $up) { Write-Error "server never became ready; log:`n$(Get-Content $log,"$log.err" -Raw -ErrorAction SilentlyContinue)"; exit 1 }

  $fail = 0
  function Check($name, $path, [scriptblock]$assert) {
    try {
      $r = Invoke-WebRequest "$base$path" -UseBasicParsing -TimeoutSec 5
      if (& $assert $r) { "PASS  $name" } else { "FAIL  $name (unexpected body/status)"; $script:fail++ }
    } catch { "FAIL  $name ($($_.Exception.Message))"; $script:fail++ }
  }

  Check "GET /        serves the UI"      "/"            { param($r) $r.StatusCode -eq 200 -and $r.Content -match 'id="root"' }
  Check "GET /api/health"                 "/api/health"  { param($r) $r.StatusCode -eq 200 -and $r.Content -match '"ok":true' }
  Check "GET /api/inbox"                  "/api/inbox"   { param($r) $r.StatusCode -eq 200 -and $r.Content -match '"cards"' }
  Check "GET /api/projects (all)"         "/api/projects" { param($r) $r.StatusCode -eq 200 -and $r.Content -match '"projects"' }
  Check "GET /api/runs (history)"         "/api/runs"    { param($r) $r.StatusCode -eq 200 -and $r.Content -match '"runs"' }
  Check "GET /api/loop-status"            "/api/loop-status" { param($r) $r.StatusCode -eq 200 -and $r.Content -match 'active_guess' }

  if ($fail -gt 0) { Write-Host "`n$fail check(s) FAILED" -ForegroundColor Red; exit 1 }
  Write-Host "`nLAUNCH VERIFY OK — $base/ serves the UI + API over the real DB." -ForegroundColor Green
  exit 0
}
finally { Stop-Server }
