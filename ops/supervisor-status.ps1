<#
  supervisor-status.ps1 — OL supervisor status at a glance. READ-ONLY (observation only; never
  approves/dispatches). Two parts:
    1. OS state  — is the supervisor process / NSSM service alive, kill-switch, detached orchestrators
    2. Fleet DB  — gate counts, DISPATCHABLE-NOW (0 == gated), in-flight, cumulative spend

  Usage (from anywhere):  .\ops\supervisor-status.ps1
#>
[CmdletBinding()]
param()
$ErrorActionPreference = "Stop"

$fv3      = "K:\OneDrive - EPM Solutions - Project Server- Project Online\Code_Factory\Factory_V3"
$ralph    = "$fv3\Python_Executions\ralph"
$stateDir = "$fv3\Sub_Projects\ol-build\state"
$py       = "C:\Users\Winterhalter\AppData\Local\Python\pythoncore-3.14-64\python.exe"

Write-Host "== OS state ==" -ForegroundColor Cyan
$procs = @(Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
           Where-Object { $_.CommandLine -like '*-m supervisor*' })
Write-Host ("supervisor process (python -m supervisor): {0} running" -f $procs.Count)
foreach ($p in $procs) { Write-Host ("    pid {0}  started {1}" -f $p.ProcessId, $p.CreationDate) }

$svc = Get-Service -Name "CF OL Supervisor" -ErrorAction SilentlyContinue
if ($svc) { Write-Host ("NSSM service 'CF OL Supervisor': {0}" -f $svc.Status) }
else      { Write-Host "NSSM service: not installed (on-demand mode)" }

$ks = Join-Path $stateDir "KILL_SWITCH"
if (Test-Path $ks) { Write-Host "KILL_SWITCH: PRESENT — all new dispatch refused" -ForegroundColor Yellow }
else               { Write-Host "KILL_SWITCH: absent" }

$orch = @(Get-CimInstance Win32_Process -Filter "Name='bash.exe'" |
          Where-Object { $_.CommandLine -like '*orchestrator.sh*' })
Write-Host ("detached orchestrators (bash orchestrator.sh): {0}" -f $orch.Count)

Write-Host ""
Write-Host "== fleet state (DB) ==" -ForegroundColor Cyan
if (-not (Test-Path $py)) { throw "python not found: $py" }
Push-Location $ralph
try { & $py "ops\supervisor_status.py" } finally { Pop-Location }
