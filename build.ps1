#Requires -Version 7
<#
.SYNOPSIS
    Build QuickVRCScaler.exe locally — mirrors what .github/workflows/build.yml does.

.DESCRIPTION
    Uses the .\venv interpreter, ensures pyinstaller is installed, wipes prior
    build/dist output, runs PyInstaller with the same flags CI uses, then
    reports the resulting EXE size.

.PARAMETER SkipTests
    Skip the unit-test pre-flight (CI always runs them; default here is to run them too).

.EXAMPLE
    .\build.ps1
    .\build.ps1 -SkipTests
#>
[CmdletBinding()]
param(
    [switch]$SkipTests
)

$ErrorActionPreference = 'Stop'
$repo = $PSScriptRoot
Set-Location $repo

$python = Join-Path $repo 'venv\Scripts\python.exe'
if (-not (Test-Path $python)) {
    throw "venv not found at $python — run 'python -m venv venv; .\venv\Scripts\pip install -r requirements.txt' first."
}

if (-not $SkipTests) {
    Write-Host '==> Running unit tests' -ForegroundColor Cyan
    & $python -W error::ResourceWarning -m unittest discover -s tests -v
    if ($LASTEXITCODE -ne 0) { throw "Tests failed (exit $LASTEXITCODE)" }
}

Write-Host '==> Ensuring pyinstaller is installed' -ForegroundColor Cyan
& $python -m pip show pyinstaller > $null 2>&1
if ($LASTEXITCODE -ne 0) {
    & $python -m pip install pyinstaller
    if ($LASTEXITCODE -ne 0) { throw 'pyinstaller install failed' }
}

Write-Host '==> Cleaning prior build artifacts' -ForegroundColor Cyan
# Kill any stray QuickVRCScaler.exe still holding the output file open.
Get-Process -ErrorAction SilentlyContinue |
    Where-Object { $_.Path -like (Join-Path $repo 'dist\QuickVRCScaler.exe') } |
    Stop-Process -Force -ErrorAction SilentlyContinue

foreach ($path in 'build', 'dist', 'QuickVRCScaler.spec') {
    if (Test-Path $path) {
        try {
            Remove-Item -Recurse -Force $path
        } catch {
            throw "Could not remove '$path' — is something holding it open? Underlying error: $($_.Exception.Message)"
        }
    }
}

Write-Host '==> Running PyInstaller' -ForegroundColor Cyan
& $python -m PyInstaller `
    --onefile `
    --windowed `
    --name QuickVRCScaler `
    --collect-all zeroconf `
    --collect-all tinyoscquery `
    quickvrcscaler.py
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed (exit $LASTEXITCODE)" }

$exe = Join-Path $repo 'dist\QuickVRCScaler.exe'
if (-not (Test-Path $exe)) { throw "Expected $exe was not produced" }

$size = (Get-Item $exe).Length
Write-Host ''
Write-Host "OK  Built $exe ($([math]::Round($size / 1MB, 2)) MB)" -ForegroundColor Green
