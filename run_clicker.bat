@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"

rem ---------------------------------------------------------------------------
rem  ASCII-ONLY FILE -- DO NOT ADD CHINESE TEXT HERE.
rem
rem  cmd.exe advances through a batch file by character count, not byte count.
rem  A line containing multi-byte characters therefore leaves the reader
rem  mid-line, and the remaining tail gets executed as a command. Verified on
rem  Windows 11 25H2: a Chinese "rem" line in this file produced
rem      'venv' is not recognized as an internal or external command
rem  even though the line was a pure comment.
rem
rem  User-facing Chinese messages belong in Python (DriverInstaller.py shows
rem  native message boxes), not in .bat echo statements.
rem ---------------------------------------------------------------------------

rem Release-package users do not need this script: RocoKingdom_Clicker.exe is
rem built with PyInstaller --uac-admin and already carries a
rem requireAdministrator manifest, so double clicking it elevates by itself.
rem This script is mainly for source/dev setups (.venv or system Python).

set "EXE=%~dp0RocoKingdom_Clicker.exe"
set "VENV_PY=%~dp0.venv\Scripts\pythonw.exe"
set "DLL=%~dp0interception.dll"
set "INSTALLER=%~dp0driver_installer\install-interception.exe"
set "FALLBACK_DLL=%~dp0third\Interception\library\x64\interception.dll"
set "FALLBACK_INSTALLER=%~dp0third\Interception\command line installer\install-interception.exe"

if not exist "%DLL%" (
    if exist "%FALLBACK_DLL%" set "DLL=%FALLBACK_DLL%"
)
if not exist "%INSTALLER%" (
    if exist "%FALLBACK_INSTALLER%" set "INSTALLER=%FALLBACK_INSTALLER%"
)

if not exist "%DLL%" (
    echo [ERROR] interception.dll not found
    echo         searched: %DLL%
    echo         Make sure the program folder is complete, or re-run build_release.bat.
    echo.
    pause
    exit /b 1
)

if not exist "%INSTALLER%" (
    echo [WARN] driver installer not found: driver_installer\install-interception.exe
    echo        The program will still start. If the driver is missing it will show
    echo        a one-click install dialog. You can also run install_driver.bat.
    echo.
)

rem ---- About the driver pre-check ----
rem Whether the driver works can only be told by creating a context and
rem injecting/reading strokes, which requires the DLL to be loaded first.
rem A .bat cannot pre-check this reliably: Interception does not register a
rem service named "interception" (it installs as the keyboard.sys / mouse.sys
rem class filter drivers), so "sc query interception" returns 1060 even when
rem the driver is fine (false negative).
rem The authoritative check happens at program start: Clicker.py probes with
rem probe.is_ready() and pops the one-click install dialog on failure.
rem See DriverInstaller.py for the detection logic.

title RocoKingdom Clicker
color 0A

echo.
echo ========================================
echo     RocoKingdom Clicker Release
echo         Powered by Interception
echo ========================================
echo.

rem ---- Release-package mode (RocoKingdom_Clicker.exe present) ----
if exist "%EXE%" (
    echo Starting RocoKingdom_Clicker.exe as administrator ...
    powershell -NoProfile -Command "Start-Process -FilePath '%EXE%' -ArgumentList '--gui' -Verb RunAs -WorkingDirectory '%~dp0'"
    goto :done
)

rem ---- Dev mode (.venv present) ----
if exist "%VENV_PY%" (
    echo Starting Clicker.py with the local virtual environment ...
    powershell -NoProfile -Command "Start-Process -FilePath '%VENV_PY%' -ArgumentList 'Clicker.py', '--gui' -Verb RunAs -WorkingDirectory '%~dp0'"
    goto :done
)

rem ---- Fallback: system Python ----
rem Probe exactly what you are about to launch: the command below starts
rem pythonw.exe, so test for pythonw. Otherwise a trimmed Python install that
rem ships only python.exe fails silently.
where pythonw >nul 2>&1
if not errorlevel 1 (
    echo Starting Clicker.py with system Python ...
    powershell -NoProfile -Command "Start-Process -FilePath 'pythonw.exe' -ArgumentList 'Clicker.py', '--gui' -Verb RunAs -WorkingDirectory '%~dp0'"
    goto :done
)

color 0C
echo.
echo [ERROR] no usable Python environment found.
echo         Install Python 3.10+ or re-download the release package.
echo.
pause
exit /b 1

:done
endlocal
