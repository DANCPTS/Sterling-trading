# Read-only dashboard for the private mock evidence log. PowerShell 5.1.
# Uses the Git credential already on this PC. No token is read or printed.
# Does not checkout or change the evidence clone.

param(
    [string]$AppRoot = "",
    [string]$EvidenceRoot = "",
    [ValidateSet("mock", "genuine")]
    [string]$Lane = "mock",
    [string]$PythonExe = "",
    [switch]$NoBrowser
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version 2.0
. (Join-Path $PSScriptRoot "sterling-python.ps1")

if ([string]::IsNullOrWhiteSpace($AppRoot)) {
    $AppRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
}
if ([string]::IsNullOrWhiteSpace($EvidenceRoot)) {
    $EvidenceRoot = Join-Path $env:USERPROFILE "sterling-forward-evidence"
}
if ([string]::IsNullOrWhiteSpace($env:LOCALAPPDATA)) {
    Write-Host "LOCALAPPDATA is not set."
    exit 1
}

$python = $PythonExe
if ([string]::IsNullOrWhiteSpace($python)) {
    $python = Get-SterlingPython -AppRoot $AppRoot
}
if ([string]::IsNullOrWhiteSpace($python) -or (Test-SterlingStoreStub $python)) {
    exit 1
}

if (-not (Test-Path -LiteralPath $EvidenceRoot)) {
    Write-Host "Evidence clone not found: $EvidenceRoot"
    Write-Host "Clone the private evidence repo there with your existing Git login, then run this again."
    exit 1
}

$work = Join-Path $env:LOCALAPPDATA "SterlingTrading"
New-Item -ItemType Directory -Force -Path $work | Out-Null
$dashboard = Join-Path $work "dashboard.html"
$extracted = Join-Path $work "forward.jsonl"

Write-Host "Fetching origin evidence"
& git -C $EvidenceRoot fetch origin evidence
if ($LASTEXITCODE -ne 0) {
    Write-Host "Fetch failed. Using the last fetched origin/evidence ref."
}

& git -C $EvidenceRoot rev-parse --verify origin/evidence
if ($LASTEXITCODE -ne 0) {
    Write-Host "No origin/evidence ref is available. Fetch the evidence branch when you are online."
    exit 1
}
$sha = (& git -C $EvidenceRoot rev-parse origin/evidence).Trim()

$relative = "evidence/invest-v2/mock/forward.jsonl"
if ($Lane -eq "genuine") {
    Write-Host "Genuine lane requested."
    $relative = "evidence/invest-v2/forward.jsonl"
}

$show = Start-Process -FilePath "git" -WorkingDirectory $EvidenceRoot -ArgumentList @(
    "show", "origin/evidence:$relative"
) -RedirectStandardOutput $extracted -NoNewWindow -Wait -PassThru
if ($show.ExitCode -ne 0) {
    Write-Host "Could not read $relative from origin/evidence. The dashboard was not replaced."
    if ((Test-Path -LiteralPath $dashboard) -and -not $NoBrowser) { Start-Process -FilePath $dashboard }
    exit 1
}

& $python -m sterling.research.invest.import_dashboard --log $extracted --out $dashboard --lane $Lane --evidence-sha $sha
$code = $LASTEXITCODE
if ($code -ne 0) {
    Write-Host "Import failed. The previous verified dashboard was kept."
    if ((Test-Path -LiteralPath $dashboard) -and -not $NoBrowser) { Start-Process -FilePath $dashboard }
    exit $code
}
if (-not $NoBrowser) { Start-Process -FilePath $dashboard }
exit 0
