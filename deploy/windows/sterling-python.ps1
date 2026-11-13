# Shared by the dashboard launcher and setup. PowerShell 5.1.
# Never runs the Microsoft Store python stub in WindowsApps.

function Test-SterlingStoreStub {
    param([string]$Path)
    if ([string]::IsNullOrWhiteSpace($Path)) { return $true }
    return $Path -like '*\WindowsApps\*'
}

function Get-SterlingPython {
    param([string]$AppRoot)

    $venv = Join-Path $AppRoot '.venv\Scripts\python.exe'
    if (Test-Path -LiteralPath $venv) {
        if (Test-SterlingStoreStub $venv) {
            Write-Host "The virtual environment points at the Microsoft Store Python stub. It will not be used."
        } else {
            return $venv
        }
    }

    $py = Get-Command py -ErrorAction SilentlyContinue
    if ($py -and -not (Test-SterlingStoreStub $py.Source)) {
        foreach ($flag in @('-3.12', '-3.11', '-3')) {
            & py $flag -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)"
            if ($LASTEXITCODE -eq 0) {
                $exe = & py $flag -c "import sys; print(sys.executable)"
                if ($exe) { $exe = $exe.Trim() }
                if (-not (Test-SterlingStoreStub $exe)) { return $exe }
            }
        }
    }

    Write-Host "Python 3.11 or 3.12 is not installed."
    Write-Host "The python command in WindowsApps is the Microsoft Store stub and will not be run."
    Write-Host "Install Python with:"
    Write-Host "  winget install -e --id Python.Python.3.12"
    Write-Host "Then, in the Sterling-trading checkout:"
    Write-Host "  powershell -ExecutionPolicy Bypass -File deploy\windows\setup.ps1"
    return $null
}
