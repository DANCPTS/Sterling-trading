# One-time setup for the local dashboard. PowerShell 5.1.
# Creates .venv and installs the sterling package.
# On Windows, pip also installs tzdata, the time-zone database used for
# Europe/London. It does not install pytest, mypy, ruff, or hypothesis.

param(
    [string]$AppRoot = ""
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version 2.0
. (Join-Path $PSScriptRoot "sterling-python.ps1")

if ([string]::IsNullOrWhiteSpace($AppRoot)) {
    $AppRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
}

$project = Join-Path $AppRoot "pyproject.toml"
if (-not (Test-Path -LiteralPath $project)) {
    Write-Host "This folder is not a Sterling-trading checkout."
    Write-Host "Clone the public repo and check out the dashboard branch:"
    Write-Host "  git clone https://github.com/DANCPTS/Sterling-trading.git"
    Write-Host "  cd Sterling-trading"
    Write-Host "  git checkout cursor/invest-dashboard-mvp-5b63"
    Write-Host "Then run this script again from that checkout."
    exit 1
}

$python = Get-SterlingPython -AppRoot $AppRoot
if ([string]::IsNullOrWhiteSpace($python) -or (Test-SterlingStoreStub $python)) {
    exit 1
}

$venvPython = Join-Path $AppRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $venvPython)) {
    Write-Host "Creating .venv"
    & $python -m venv (Join-Path $AppRoot ".venv")
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}

Write-Host "Installing sterling and tzdata. tzdata supplies Europe/London on Windows. pytest, mypy, ruff, and hypothesis are not installed."
& $venvPython -m pip install -e $AppRoot
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
Write-Host "Setup finished. Double-click deploy\windows\Sterling-Dashboard.cmd"
exit 0
