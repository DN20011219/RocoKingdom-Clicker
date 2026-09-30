@echo off
setlocal EnableExtensions
cd /d "%~dp0"
title Interception Driver Installer - RocoKingdom Clicker

rem ---------------------------------------------------------------------------
rem  ASCII-ONLY FILE -- DO NOT ADD CHINESE TEXT HERE.
rem
rem  cmd.exe advances through a batch file by character count, not byte count,
rem  so a line containing multi-byte characters leaves the reader mid-line and
rem  the remaining tail is executed as a command. Every user-facing Chinese
rem  message is therefore produced by DriverInstaller.py as a native message
rem  box; this script is only a launcher.
rem ---------------------------------------------------------------------------

set "MODE=install"
if /i "%~1"=="/uninstall"   set "MODE=uninstall"
if /i "%~1"=="uninstall"    set "MODE=uninstall"
if /i "%~1"=="--uninstall"  set "MODE=uninstall"

rem ---- Preferred path: the packaged exe ----
rem It already embeds a requireAdministrator manifest (PyInstaller --uac-admin)
rem so it elevates by itself, and DriverInstaller.py shows the Chinese dialogs
rem including the "reboot now?" question. No elevation logic needed here.
set "EXE=%~dp0RocoKingdom_Clicker.exe"
if exist "%EXE%" goto :via_exe

rem ---- Source checkout: drive DriverInstaller.py with Python ----
if not exist "%~dp0DriverInstaller.py" goto :via_raw_installer
where python >nul 2>&1
if errorlevel 1 goto :via_raw_installer

echo Running: python DriverInstaller.py --%MODE%
echo Follow the dialogs; a reboot is required afterwards.
echo.
python "%~dp0DriverInstaller.py" --%MODE%
set "RC=%ERRORLEVEL%"
goto :report

:via_exe
echo Running: RocoKingdom_Clicker.exe --%MODE%-driver
echo Click Yes in the UAC prompt, then follow the dialogs.
echo.
start "" /wait "%EXE%" --%MODE%-driver
set "RC=%ERRORLEVEL%"
goto :report

rem ---- Last resort: no exe and no Python ----
rem Drive the official installer directly. There is no manifest-bearing host
rem left to elevate for us, so re-launch this script through PowerShell.
:via_raw_installer
set "INSTALLER=%~dp0driver_installer\install-interception.exe"
if exist "%INSTALLER%" goto :raw_elevate
set "INSTALLER=%~dp0third\Interception\command line installer\install-interception.exe"
if exist "%INSTALLER%" goto :raw_elevate

echo [ERROR] install-interception.exe not found. Looked in:
echo           %~dp0driver_installer\
echo           %~dp0third\Interception\command line installer\
echo         Make sure the release package was FULLY extracted; do not run it
echo         from inside the zip archive.
echo.
pause
exit /b 1

:raw_elevate
net session >nul 2>&1
if not errorlevel 1 goto :raw_run
if /i "%~2"=="elevated" goto :raw_elevate_failed

echo Requesting administrator rights, click Yes in the UAC prompt ...
powershell -NoProfile -Command "try { Start-Process -FilePath '%~f0' -ArgumentList '/%MODE%','elevated' -Verb RunAs -ErrorAction Stop } catch { exit 1 }"
if errorlevel 1 goto :uac_cancelled
exit /b

:raw_elevate_failed
echo [ERROR] still not running as administrator after elevation.
echo         Right-click this file and choose "Run as administrator".
echo.
pause
exit /b 1

:uac_cancelled
echo [CANCELLED] administrator rights were not granted; nothing was changed.
echo.
pause
exit /b 1

:raw_run
echo Running: "%INSTALLER%" /%MODE%
echo ------------------------------------------------------------
"%INSTALLER%" /%MODE%
set "RC=%ERRORLEVEL%"
echo ------------------------------------------------------------

:report
echo.
if "%RC%"=="0" (
    echo Finished successfully.
) else (
    echo Finished with exit code %RC%.
)
echo A reboot is required for the driver change to take effect.
echo.
pause
exit /b %RC%
