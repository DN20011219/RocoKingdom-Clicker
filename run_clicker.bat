@echo off
setlocal
cd /d "%~dp0"

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
    echo 【错误】找不到 interception.dll
    echo    查找路径：%DLL%
    echo    请确认程序目录完整，或重新运行 build_release.bat 打包。
    echo.
    pause
    exit /b 1
)

if not exist "%INSTALLER%" (
    echo 【警告】未找到驱动安装程序：driver_installer\install-interception.exe
    echo    程序仍然会尝试启动，但如果驱动未安装，会再次弹出提示。
    echo.
)

rem ---- 关于驱动预检 ----
rem 能否使用驱动，取决于“能否创建上下文并注入/读取”，只有加载 DLL 后才能判断。
rem bat 无法可靠预检：Interception 并不注册名为 interception 的服务（它以 keyboard.sys /
rem mouse.sys 类过滤驱动形式安装），sc query interception 会在驱动正常时也返回 1060（假阴性）。
rem 权威检测在程序启动时进行：Clicker.py 的 probe.is_ready() 失败会弹框提示安装驱动。

title RocoKingdom Clicker
color 0A

echo.
echo ========================================
echo     RocoKingdom Clicker Release
echo         Powered by Interception
echo ========================================
echo.

rem ---- 发布包模式（有 RocoKingdom_Clicker.exe） ----
if exist "%EXE%" (
    echo 以管理员权限启动 RocoKingdom_Clicker.exe ...
    powershell -NoProfile -Command "Start-Process -FilePath '%EXE%' -ArgumentList '--gui' -Verb RunAs -WorkingDirectory '%~dp0'"
    goto :done
)

rem ---- 开发模式（有 .venv） ----
if exist "%VENV_PY%" (
    echo 使用本地虚拟环境启动 Clicker.py ...
    powershell -NoProfile -Command "Start-Process -FilePath '%VENV_PY%' -ArgumentList 'Clicker.py', '--gui' -Verb RunAs -WorkingDirectory '%~dp0'"
    goto :done
)

rem ---- 回退：系统 Python ----
rem 判断对象要和启动对象一致：下面启动的是 pythonw.exe，就检测 pythonw，
rem 否则精简版 Python 只有 python.exe 而无 pythonw.exe 时会静默失败。
where pythonw >nul 2>&1
if not errorlevel 1 (
    echo 使用系统 Python 启动 Clicker.py ...
    powershell -NoProfile -Command "Start-Process -FilePath 'pythonw.exe' -ArgumentList 'Clicker.py', '--gui' -Verb RunAs -WorkingDirectory '%~dp0'"
    goto :done
)

color 0C
echo.
echo 【错误】未找到可用的 Python 环境。
echo 请先安装 Python 3.10+，或重新下载发布包。
echo.
pause
exit /b 1

:done
endlocal
