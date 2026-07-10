<#
  run-supervisor-foreground.ps1 — hand-start the OL supervisor in the FOREGROUND (on-demand mode;
  the non-daemonized alternative to the NSSM service). Sets the full env block (parity with
  New\OL_Supervisor_NSSM_Daemonization_Setup §1) and launches `python -m supervisor`.

  It DRAINS approved work: dispatches any lifecycle_state='candidate' project up to the ceiling.
  With the fleet gated (0 candidates) it dispatches NOTHING — safe. A pre-flight prints the gate
  state first so you see exactly what WILL dispatch before the loop starts. Ctrl-C stops it (the
  supervisor finishes its current cycle + persists). Nothing runs without an operator Approve
  (policy 2026-07-09) — this script never approves; it only drains what you already approved.

  Usage (from anywhere):
    .\ops\run-supervisor-foreground.ps1              # resident loop, 30s interval, ceiling 1
    .\ops\run-supervisor-foreground.ps1 -Once        # single cycle then exit
    .\ops\run-supervisor-foreground.ps1 -Interval 60 -Ceiling 1
#>
[CmdletBinding()]
param(
  [int]$Interval = 30,
  [int]$Ceiling = 1,
  [int]$MaxDispatchesPerCycle = 1,
  [switch]$Once
)
$ErrorActionPreference = "Stop"

$fv3   = "K:\OneDrive - EPM Solutions - Project Server- Project Online\Code_Factory\Factory_V3"
$ralph = "$fv3\Python_Executions\ralph"
$py    = "C:\Users\Winterhalter\AppData\Local\Python\pythoncore-3.14-64\python.exe"

# --- env parity (NSSM doc §1) ---
$env:OL_SUPERVISOR_WORKSPACE_ROOT           = $fv3
$env:OL_SUPERVISOR_STATE_DIR                = "$fv3\Sub_Projects\ol-build\state"
$env:OL_SUPERVISOR_ORCHESTRATOR             = "$ralph\orchestrator.sh"
$env:OL_SUPERVISOR_BASH                     = "C:\Program Files\Git\bin\bash.exe"
$env:OL_SUPERVISOR_CONCURRENCY_CEILING      = "$Ceiling"
$env:OL_SUPERVISOR_MAX_DISPATCHES_PER_CYCLE = "$MaxDispatchesPerCycle"
$env:PYTHONUNBUFFERED                       = "1"
# PROD_DB_URL / SUPABASE_ACCESS_TOKEN / F_GMAIL_SMTP_APP_PASSWORD inherit from Machine scope.
# OL_SUPERVISOR_EMERGENCY_SPEND_CEILING_USD is deliberately UNSET here — it is CUMULATIVE-all-time
# (a value below current cumulative trips the kill on cycle 1). This is a WATCHED foreground run,
# bounded by ceiling + per-call cap; set it only for the unattended NSSM service.

if (-not (Test-Path $py))    { throw "python not found: $py" }
if (-not (Test-Path $ralph)) { throw "ralph dir not found: $ralph" }

# --- warn if a supervisor is already running (avoid two loops racing the same fleet) ---
$existing = @(Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
              Where-Object { $_.CommandLine -like '*-m supervisor*' })
if ($existing.Count -gt 0) {
  Write-Warning ("A supervisor is ALREADY running (pid {0}). Two loops will race the same fleet — abort (Ctrl-C) unless intentional." -f ($existing.ProcessId -join ','))
}

# --- safety pre-flight: show what WILL dispatch before launching ---
Write-Host "== pre-flight: fleet gate ==" -ForegroundColor Cyan
Push-Location $ralph
try   { & $py "ops\supervisor_status.py" }
catch { Write-Warning "status pre-flight failed: $_" }

$modeDesc = if ($Once) { "--once (single cycle)" } else { "--interval $Interval (resident loop)" }
Write-Host ""
Write-Host "Starting supervisor: ceiling=$Ceiling, $modeDesc." -ForegroundColor Yellow
Write-Host "It will dispatch any 'candidate' shown above (0 == nothing). Ctrl-C to stop." -ForegroundColor Yellow

$mode = if ($Once) { @("--once") } else { @("--interval", "$Interval") }
try   { & $py "-m" "supervisor" @mode }
finally { Pop-Location }
