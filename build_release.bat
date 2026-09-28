@echo off
setlocal enabledelayedexpansion
cd /d "%~dp0"
chcp 65001 >nul

echo ========================================
echo     RocoKingdom Clicker - Release Builder
echo ========================================

rem 1) Locate Python
set "PY="

where py >nul 2>&1
if not errorlevel 1 (
    py -3.10 -c "import sys; sys.exit(0)" >nul 2>&1
    if not errorlevel 1 set "PY=py -3.10"

    if not defined PY (
        py -3 -c "import sys; sys.exit(0)" >nul 2>&1
        if not errorlevel 1 set "PY=py -3"
    )
)

if not defined PY (
    where python >nul 2>&1
    if not errorlevel 1 set "PY=python"
)

if not defined PY (
    if exist "%LocalAppData%\Programs\Python\Python310\python.exe" set "PY=%LocalAppData%\Programs\Python\Python310\python.exe"
)

if not defined PY (
    echo Failed to locate a usable Python interpreter.
    exit /b 1
)

echo Using Python: %PY%

rem 2) Install build deps
echo Upgrading pip and installing PyInstaller...
%PY% -m pip install --user --upgrade pip setuptools wheel pyinstaller >nul 2>&1
if errorlevel 1 (
    echo Failed to install PyInstaller.
    exit /b 1
)

rem 3) Run PyInstaller
echo Running PyInstaller (windowed)...
rem --uac-admin 会把 requireAdministrator 清单内嵌进 exe，双击即弹 UAC 提权。
rem 这是 run_clicker.vbs 的替代品：Windows 11 25H2 起 VBScript 默认被禁用，
rem 不能再依赖 .vbs 去申请管理员权限。
%PY% -m PyInstaller --noconfirm --clean --distpath "%~dp0dist" --workpath "%~dp0build_pyinstaller" --specpath "%~dp0build_pyinstaller" --name "RocoKingdom_Clicker" --onedir --windowed --uac-admin --hidden-import gui --hidden-import webview --hidden-import DriverInstaller --add-data "%~dp0docs;docs" Clicker.py
if errorlevel 1 (
    echo PyInstaller build failed.
    exit /b 1
)

rem 4) All copy / license / packaging steps delegated to PowerShell so we avoid
rem    cmd/PowerShell quoting issues and execution policy surprises inside .bat
echo Running post-build copy (data, driver, licenses)...
powershell -ExecutionPolicy Bypass -NoProfile -File "%~dp0_release_copy.ps1"
if errorlevel 1 (
    echo Post-build copy failed.
    exit /b 1
)

echo Build complete. Release artifact: release\RocoKingdom_Clicker.zip
echo To run the release, extract and double-click RocoKingdom_Clicker.exe (UAC prompt is built in).
endlocal
exit /b 0
