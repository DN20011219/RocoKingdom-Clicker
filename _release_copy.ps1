# Post-build copy + packaging (called by build_release.bat)
$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Dist = Join-Path $Root 'dist\RocoKingdom_Clicker'

if (-not (Test-Path (Join-Path $Dist 'RocoKingdom_Clicker.exe'))) {
    Write-Error "RocoKingdom_Clicker.exe not found at $Dist"
    exit 1
}

Write-Host "Copying top-level files (run scripts, README, LICENSE, GPL, LGPL)..."
@('run_clicker.bat', 'run_clicker.vbs', 'README.md', 'LICENSE', 'COPYING', 'COPYING.LESSER') | ForEach-Object {
    $src = Join-Path $Root $_
    if (Test-Path $src) {
        Copy-Item -Path $src -Destination $Dist -Force
    } else {
        Write-Host "  (skipped: $_ not found)"
    }
}

Write-Host "Copying data/ folder..."
$dataSrc = Join-Path $Root 'data'
$dataDst = Join-Path $Dist 'data'
if (Test-Path $dataSrc) {
    if (-not (Test-Path $dataDst)) { New-Item -ItemType Directory -Path $dataDst -Force | Out-Null }
    Copy-Item -Path (Join-Path $dataSrc '*') -Destination $dataDst -Recurse -Force
}

Write-Host "Copying interception.dll..."
$dll = Join-Path $Root 'third\Interception\library\x64\interception.dll'
if (-not (Test-Path $dll)) { $dll = Join-Path $Root 'interception.dll' }
if (Test-Path $dll) {
    Copy-Item -Path $dll -Destination $Dist -Force
} else {
    Write-Warning "interception.dll not found (tried third\Interception\library\x64 and project root)"
}

Write-Host "Copying driver installer (driver_installer\)..."
$installer = Join-Path $Root 'third\Interception\command line installer\install-interception.exe'
$driverDst = Join-Path $Dist 'driver_installer'
if (Test-Path $installer) {
    if (-not (Test-Path $driverDst)) { New-Item -ItemType Directory -Path $driverDst -Force | Out-Null }
    Copy-Item -Path $installer -Destination $driverDst -Force
} else {
    Write-Warning "install-interception.exe not found"
}

Write-Host "Copying docs/ folder (from _internal/)..."
$docsInternal = Join-Path $Dist '_internal\docs'
$docsDst = Join-Path $Dist 'docs'
if (Test-Path $docsInternal) {
    if (Test-Path $docsDst) { Remove-Item -Path $docsDst -Recurse -Force }
    Copy-Item -Path $docsInternal -Destination $docsDst -Recurse -Force
} else {
    Write-Warning "_internal\docs not found (PyInstaller may not have included it)"
}

Write-Host "Copying third-party licenses (third_party_licenses\Interception\)..."
$licSrc = Join-Path $Root 'third\Interception\licenses'
$licDst = Join-Path $Dist 'third_party_licenses\Interception'
if (Test-Path $licSrc) {
    if (-not (Test-Path $licDst)) { New-Item -ItemType Directory -Path $licDst -Force | Out-Null }
    Copy-Item -Path (Join-Path $licSrc '*') -Destination $licDst -Recurse -Force
} else {
    Write-Warning "third\Interception\licenses not found"
}

Write-Host "Creating release archive (release\RocoKingdom_Clicker.zip)..."
$releaseDir = Join-Path $Root 'release'
if (-not (Test-Path $releaseDir)) { New-Item -ItemType Directory -Path $releaseDir -Force | Out-Null }
$zipPath = Join-Path $releaseDir 'RocoKingdom_Clicker.zip'

Add-Type -AssemblyName System.IO.Compression.FileSystem
if (Test-Path $zipPath) { Remove-Item -Path $zipPath -Force }
# Use the 2-argument overload (source, destination). Output is not flattened.
[System.IO.Compression.ZipFile]::CreateFromDirectory($Dist, $zipPath)

Write-Host ""
Write-Host "Done. Archive size:"
Get-Item -Path $zipPath | Select-Object Name,@{Name='SizeKB';Expression={[int]($_.Length/1024)}}, LastWriteTime | Format-List

Write-Host ""
Write-Host "Contents of dist\RocoKingdom_Clicker :"
Get-ChildItem -Path $Dist | Format-Table Name,Length -AutoSize
