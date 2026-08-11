#Requires -Version 5.1
<#
.SYNOPSIS
    Build atomcode-proxy into a single-file exe, artifacts go to release\.
.DESCRIPTION
    Usage:
        .\scripts\build.ps1 [-Version <version>]
    When -Version is non-empty, it overwrites __version__ in atomcode_proxy/__init__.py.
#>
param(
    [string]$Version = ""
)

$ErrorActionPreference = "Stop"

# Project root = parent of scripts\
$Root = Split-Path -Parent $PSScriptRoot
$InitPy = Join-Path $Root "atomcode_proxy\__init__.py"

function Get-VersionFromInit {
    $content = Get-Content $InitPy -Raw -Encoding UTF8
    if ($content -match '__version__\s*=\s*"([^"]+)"') {
        return $Matches[1]
    }
    throw "Cannot parse __version__ from atomcode_proxy/__init__.py"
}

# 1) Optional: overwrite version
if ($Version -ne "") {
    Write-Host "[1/5] Setting version $Version in atomcode_proxy/__init__.py"
    $content = Get-Content $InitPy -Raw -Encoding UTF8
    if ($content -notmatch '__version__\s*=\s*"[^"]+"') {
        throw "No __version__ assignment found in atomcode_proxy/__init__.py, refusing to overwrite"
    }
    $updated = $content -replace '__version__\s*=\s*"[^"]+"', ('__version__ = "' + $Version + '"')
    Set-Content -Path $InitPy -Value $updated -Encoding UTF8 -NoNewline
} else {
    Write-Host "[1/5] No -Version given, using existing version from __init__.py"
}

$BuildVersion = Get-VersionFromInit
Write-Host "      Build version: $BuildVersion"

# 2) Install PyInstaller (idempotent, skips if satisfied)
Write-Host "[2/5] Installing/verifying pyinstaller >= 6.6"
pip install "pyinstaller>=6.6"
if ($LASTEXITCODE -ne 0) {
    throw "pip install pyinstaller failed (exit code $LASTEXITCODE)"
}

# 3) Run the build
Write-Host "[3/5] Running pyinstaller atomcode-proxy.spec --clean --noconfirm"
Push-Location $Root
try {
    pyinstaller atomcode-proxy.spec --clean --noconfirm
    if ($LASTEXITCODE -ne 0) {
        throw "pyinstaller build failed (exit code $LASTEXITCODE)"
    }
} finally {
    Pop-Location
}

# 4) Collect artifacts into release\
Write-Host "[4/5] Collecting artifacts into the release directory"
$DistExe = Join-Path $Root "dist\atomcode-proxy.exe"
if (-not (Test-Path $DistExe)) {
    throw "Build artifact not found: $DistExe"
}

$ReleaseDir = Join-Path $Root "release"
if (-not (Test-Path $ReleaseDir)) {
    New-Item -ItemType Directory -Path $ReleaseDir | Out-Null
}

$ReleaseName = "atomcode-proxy-$BuildVersion-windows-x64.exe"
$ReleaseExe = Join-Path $ReleaseDir $ReleaseName
Copy-Item $DistExe $ReleaseExe -Force

$EnvExample = Join-Path $Root ".env.example"
if (Test-Path $EnvExample) {
    Copy-Item $EnvExample $ReleaseDir -Force
} else {
    Write-Warning ".env.example not found, skipping copy"
}

# 5) Report artifacts
Write-Host "[5/5] Build finished, artifacts:"
Get-ChildItem $ReleaseDir | ForEach-Object {
    $sizeKB = [math]::Round($_.Length / 1KB, 1)
    $sizeMB = [math]::Round($_.Length / 1MB, 2)
    Write-Host ("      {0}  ({1} MB / {2} KB)" -f $_.FullName, $sizeMB, $sizeKB)
}
