# Read-only dashboard for the private mock evidence log. PowerShell 5.1.
# Uses the Git credential already on this PC. No token is read or printed.
# Reads origin/evidence only. Does not checkout or change the evidence clone.

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

function Test-EvidenceOrigin {
    param([string]$Url)
    $text = $Url.Trim().TrimEnd("/").ToLowerInvariant()
    if ($text.EndsWith(".git")) { $text = $text.Substring(0, $text.Length - 4) }
    $https = "https://github.com/dancpts/sterling-forward-evidence"
    $ssh = "git@github.com:dancpts/sterling-forward-evidence"
    return ($text -eq $https) -or ($text -eq $ssh)
}

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
if (-not (Test-Path -LiteralPath $python)) {
    Write-Host "Python was not found: $python"
    exit 1
}
if (-not (Test-Path -LiteralPath $EvidenceRoot)) {
    Write-Host "Evidence clone not found: $EvidenceRoot"
    Write-Host "Clone the private evidence repo there with your existing Git login, then run this again."
    exit 1
}

$origin = (& git -C $EvidenceRoot config --local --get remote.origin.url).Trim()
if ($LASTEXITCODE -ne 0 -or -not (Test-EvidenceOrigin $origin)) {
    if ($origin -match 'token' -or $origin -match 'ghp_' -or $origin -match '://[^/]*@') {
        Write-Host "origin is not the private evidence repository. Refusing to read this clone."
    } else {
        Write-Host "origin is '$origin'."
        Write-Host "Expected https://github.com/DANCPTS/sterling-forward-evidence.git. Refusing to read this clone."
    }
    exit 1
}

$branch = (& git -C $EvidenceRoot rev-parse --abbrev-ref HEAD).Trim()
Write-Host "Leaving checked-out branch $branch as it is. Reading origin/evidence only."

$work = Join-Path $env:LOCALAPPDATA "SterlingTrading"
New-Item -ItemType Directory -Force -Path $work | Out-Null
$dashboard = Join-Path $work "dashboard.html"
$extracted = Join-Path ([System.IO.Path]::GetTempPath()) ("sterling-evidence-" + [guid]::NewGuid().ToString("n") + ".jsonl")

try {
    New-Item -ItemType File -Path $extracted -Force | Out-Null
    if (Get-Command icacls -ErrorAction SilentlyContinue) {
        & icacls $extracted /inheritance:r /grant:r "$($env:USERNAME):(R,W)" | Out-Null
    }

    Write-Host "Fetching origin evidence"
    & git -C $EvidenceRoot fetch origin "+refs/heads/evidence:refs/remotes/origin/evidence"
    $fetched = "fresh"
    if ($LASTEXITCODE -ne 0) {
        $fetched = "stale"
        Write-Host "OFFLINE / STALE. Fetch failed. Using the last origin/evidence ref already on this PC. This is not a new fetch."
    } else {
        Write-Host "Fetched origin/evidence this run."
    }

    & git -C $EvidenceRoot rev-parse --verify "refs/remotes/origin/evidence"
    if ($LASTEXITCODE -ne 0) {
        Write-Host "No origin/evidence ref is available. Fetch the evidence branch when you are online."
        exit 1
    }
    $sha = (& git -C $EvidenceRoot rev-parse "refs/remotes/origin/evidence").Trim()

    $relative = "evidence/invest-v2/mock/forward.jsonl"
    if ($Lane -eq "genuine") {
        Write-Host "Genuine lane requested."
        $relative = "evidence/invest-v2/forward.jsonl"
    }

    $show = Start-Process -FilePath "git" -WorkingDirectory $EvidenceRoot -ArgumentList @(
        "show", "origin/evidence:$relative"
    ) -RedirectStandardOutput $extracted -NoNewWindow -Wait -PassThru
    if ($show.ExitCode -ne 0) {
        Write-Host "CHECK FAILED. Could not read $relative from origin/evidence. The previous page was kept."
        & $python -m sterling.research.invest.import_dashboard --mark-kept --out $dashboard
        if ((Test-Path -LiteralPath $dashboard) -and -not $NoBrowser) { Start-Process -FilePath $dashboard }
        exit 1
    }

    & $python -m sterling.research.invest.import_dashboard --log $extracted --out $dashboard --lane $Lane --evidence-sha $sha --fetched $fetched
    $code = $LASTEXITCODE
    if ($code -ne 0) {
        Write-Host "OFFLINE PAGE KEPT. Import failed. The previous verified dashboard was kept and was not replaced."
        if ((Test-Path -LiteralPath $dashboard) -and -not $NoBrowser) { Start-Process -FilePath $dashboard }
        exit $code
    }
    if (-not $NoBrowser) { Start-Process -FilePath $dashboard }
    exit 0
} finally {
    if (Test-Path -LiteralPath $extracted) {
        Remove-Item -LiteralPath $extracted -Force -ErrorAction SilentlyContinue
    }
}
