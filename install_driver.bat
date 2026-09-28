@echo off
setlocal EnableExtensions
chcp 65001 >nul
cd /d "%~dp0"
title Interception 驱动一键安装 - RocoKingdom Clicker

set "ACTION=/install"
if /i "%~1"=="/uninstall" set "ACTION=/uninstall"
if /i "%~1"=="uninstall" set "ACTION=/uninstall"

rem ============================================================
rem  自提权
rem  Windows 11 25H2 起 VBScript 被列为「按需功能」并默认关闭，双击 .vbs 不再
rem  可靠，所以这里不用 .vbs，而是用 PowerShell 的 Start-Process -Verb RunAs
rem  把自己重新拉起一次；第二个参数 elevated 是标记，防止无限自我提权。
rem ============================================================
net session >nul 2>&1
if not errorlevel 1 goto :elevated
if /i "%~2"=="elevated" goto :elevate_failed

echo.
echo 正在请求管理员权限，请在弹出的 UAC 窗口中点「是」...
powershell -NoProfile -Command "try { Start-Process -FilePath '%~f0' -ArgumentList '%ACTION%','elevated' -Verb RunAs -ErrorAction Stop } catch { exit 1 }"
if errorlevel 1 goto :uac_cancelled
exit /b

:uac_cancelled
echo.
echo 【已取消】没有获得管理员权限，驱动未做任何改动。
echo.
pause
exit /b 1

:elevate_failed
echo.
echo 【错误】提权后仍然不是管理员身份。
echo   请手动右键本文件，选择「以管理员身份运行」。
echo.
pause
exit /b 1

:elevated
echo.
echo ========================================
echo   Interception 驱动安装工具
echo   操作：%ACTION%
echo ========================================
echo.

rem ---- 定位官方安装器（发布包 / 源码仓库两种布局） ----
set "INSTALLER=%~dp0driver_installer\install-interception.exe"
if exist "%INSTALLER%" goto :found_installer
set "INSTALLER=%~dp0third\Interception\command line installer\install-interception.exe"
if exist "%INSTALLER%" goto :found_installer
set "INSTALLER=%~dp0install-interception.exe"
if exist "%INSTALLER%" goto :found_installer

echo 【错误】找不到驱动安装程序 install-interception.exe
echo   已查找以下位置：
echo     %~dp0driver_installer\install-interception.exe
echo     %~dp0third\Interception\command line installer\install-interception.exe
echo     %~dp0install-interception.exe
echo   请确认发布包已【完整解压】，不要直接在压缩包里双击运行。
echo.
pause
exit /b 1

:found_installer
echo 安装程序：%INSTALLER%
echo.

rem ---- 先报一下当前状态，方便判断「装了但没重启」 ----
rem Interception 是键盘/鼠标「类过滤驱动」，服务名就叫 keyboard / mouse，
rem 并没有名为 interception 的服务，所以 sc query interception 永远查不到。
set "DRIVER_STATE=未安装"
reg query "HKLM\SYSTEM\CurrentControlSet\Control\Class\{4D36E96F-E325-11CE-BFC1-08002BE10318}" /v UpperFilters 2>nul | find "mouse\0" >nul
if not errorlevel 1 set "DRIVER_STATE=已安装（若程序仍报驱动未就绪，通常只是还没重启电脑）"
echo 当前驱动状态：%DRIVER_STATE%
echo.

set "LOG=%TEMP%\roco_driver_install.log"
if exist "%LOG%" del /f /q "%LOG%" >nul 2>&1

echo 正在执行：%INSTALLER% %ACTION%
echo ------------------------------------------------------------
"%INSTALLER%" %ACTION% > "%LOG%" 2>&1
set "RC=%ERRORLEVEL%"
if exist "%LOG%" type "%LOG%"
echo ------------------------------------------------------------
echo.

if %RC% equ 0 goto :done_ok
rem 个别版本的安装器返回码不规范，用官方成功文案兜底判断
findstr /i /c:"successfully" "%LOG%" >nul 2>&1
if not errorlevel 1 goto :done_ok

echo 【失败】安装程序返回码：%RC%
echo   常见原因：
echo     1) 没有真正的管理员权限（被 UAC 拦下）
echo     2) 安全软件阻止了驱动写入
echo     3) 发布包不完整，安装器本体损坏
echo   完整输出见：%LOG%
echo.
pause
exit /b 1

:done_ok
if /i "%ACTION%"=="/uninstall" goto :done_uninstall

echo 【成功】驱动已安装，必须重启电脑才能生效。
echo.
choice /c YN /m "是否立即重启电脑？"
if errorlevel 2 goto :skip_reboot
echo.
echo 系统将在 15 秒后重启；如需取消请立刻在命令行执行：shutdown /a
shutdown /r /t 15 /c "Interception 驱动已安装，系统即将重启以生效"
exit /b 0

:skip_reboot
echo.
echo 好的，请稍后手动重启电脑，重启后再双击 RocoKingdom_Clicker.exe 即可。
echo.
pause
exit /b 0

:done_uninstall
echo 【成功】驱动已卸载，重启电脑后彻底移除。
echo.
pause
exit /b 0
