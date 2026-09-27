Option Explicit
Dim objShell, objShellApp, fso, scriptPath, scriptDir
Dim exePath, venvPythonw, installerPath, dllPath
Dim msg, launchOK

Set objShell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
scriptPath = WScript.ScriptFullName
scriptDir = fso.GetParentFolderName(scriptPath)

exePath = scriptDir & "\RocoKingdom_Clicker.exe"
venvPythonw = scriptDir & "\.venv\Scripts\pythonw.exe"
installerPath = scriptDir & "\driver_installer\install-interception.exe"
dllPath = scriptDir & "\interception.dll"

If Not fso.FileExists(installerPath) Then
    Dim fallbackInstaller
    fallbackInstaller = scriptDir & "\third\Interception\command line installer\install-interception.exe"
    If fso.FileExists(fallbackInstaller) Then installerPath = fallbackInstaller
End If
If Not fso.FileExists(dllPath) Then
    Dim fallbackDll
    fallbackDll = scriptDir & "\third\Interception\library\x64\interception.dll"
    If fso.FileExists(fallbackDll) Then dllPath = fallbackDll
End If

If Not fso.FileExists(dllPath) Then
    msg = "interception.dll not found." & vbCrLf & _
          "Please ensure the DLL is in the program directory." & vbCrLf & vbCrLf & _
          "Searched: " & vbCrLf & scriptDir & "\interception.dll"
    objShell.Popup msg, 0, "Missing DLL - RocoKingdom Clicker", 16
    WScript.Quit 1
End If

If Not fso.FileExists(installerPath) Then
    msg = "Driver installer not found (driver_installer\install-interception.exe)." & vbCrLf & _
          "The program will still try to start, but may show another prompt if driver is not installed."
    objShell.Popup msg, 0, "Notice - RocoKingdom Clicker", 48
End If

' ---- 关于驱动预检 ----
' 能否使用驱动，取决于“能否创建上下文并注入/读取”，只有加载 DLL 后才能判断。
' vbs 无法可靠预检：Interception 并不注册名为 interception 的服务（它以 keyboard.sys /
' mouse.sys 类过滤驱动形式安装），按服务名查询会在驱动正常时误判为未安装（假阴性）。
' 权威检测在程序启动时：Clicker.py 的 probe.is_ready() 失败会弹框提示安装驱动。

Set objShellApp = CreateObject("Shell.Application")

launchOK = False

If fso.FileExists(exePath) Then
    objShellApp.ShellExecute exePath, "--gui", scriptDir, "runas", 1
    launchOK = True
ElseIf fso.FileExists(venvPythonw) Then
    objShellApp.ShellExecute venvPythonw, "Clicker.py --gui", scriptDir, "runas", 1
    launchOK = True
Else
    ' 系统 Python 回退：不能无条件置 launchOK=True。pythonw.exe 若不存在，
    ' ShellExecute 会抛错；因为用的是 pythonw（无控制台），用户看不到任何反应，
    ' 下面的“启动失败”提示也永不触发。用错误处理让 launchOK 反映真实结果。
    On Error Resume Next
    objShellApp.ShellExecute "pythonw.exe", "Clicker.py --gui", scriptDir, "runas", 1
    If Err.Number = 0 Then
        launchOK = True
    Else
        launchOK = False
        Err.Clear
    End If
    On Error GoTo 0
End If

If Not launchOK Then
    objShell.Popup "Failed to launch the program. Please check Python or redownload.", 0, "Launch Failed - RocoKingdom Clicker", 16
    WScript.Quit 2
End If
