"""Interception 驱动的一键安装 / 卸载与就绪诊断。

为什么需要这个模块
------------------
1. Windows 11 25H2 起 VBScript 被列为「按需功能」并默认关闭，双击 `.vbs`
   不再可靠，原来靠 `run_clicker.vbs` 弹 UAC 的启动方式必须换掉。发布包的
   exe 改由 PyInstaller `--uac-admin` 内嵌 `requireAdministrator` 清单，双击
   即提权；本模块因此通常直接继承管理员权限，无需二次弹窗。
2. 官方 `install-interception.exe /install` 要求「以管理员身份打开终端再手敲
   命令」，大量用户卡在这一步。这里把整个流程收进程序内部：
   检测注册表 → 弹「是/否」对话框 → 静默跑安装器 → 提示重启。

关于驱动检测
------------
Interception 以「键盘/鼠标类过滤驱动」形式安装，服务名就叫 `keyboard` /
`mouse`，**并没有**名为 `interception` 的服务，所以 `sc query interception`
永远返回 1060（假阴性）。这里改用三个可靠信号（读 HKLM 不需要管理员权限）：
  - 类键 `UpperFilters` 是否含 `mouse` / `keyboard`
  - `Services\\mouse` / `Services\\keyboard` 键是否存在
  - `%SystemRoot%\\System32\\drivers\\mouse.sys` 是否落地
"""

from __future__ import annotations

import argparse
import ctypes
import logging
import os
import subprocess
import sys
import tempfile
from ctypes import wintypes
from pathlib import Path

logger = logging.getLogger("driver_installer")

# ---- 注册表位置（Interception 官方安装器写入的三处痕迹） ----
KEYBOARD_CLASS_KEY = (
    r"SYSTEM\CurrentControlSet\Control\Class\{4D36E96B-E325-11CE-BFC1-08002BE10318}"
)
MOUSE_CLASS_KEY = (
    r"SYSTEM\CurrentControlSet\Control\Class\{4D36E96F-E325-11CE-BFC1-08002BE10318}"
)

# ---- 安装器查找路径（发布包 / 源码仓库两种布局） ----
INSTALLER_NAME = "install-interception.exe"
INSTALLER_SUBDIRS = (
    Path("driver_installer"),
    Path("third") / "Interception" / "command line installer",
)

# ---- run_installer / offer_one_click_install 的返回状态 ----
INSTALL_OK = "ok"                    # 安装器执行成功（仍需重启才生效）
INSTALL_FAILED = "failed"            # 安装器执行失败
INSTALL_CANCELLED = "cancelled"      # 用户在 UAC 弹窗里点了「否」
INSTALL_NO_INSTALLER = "no_installer"  # 找不到 install-interception.exe
INSTALL_DECLINED = "declined"        # 用户在「是否现在安装」里点了「否」

# ---- Win32 常量 ----
ERROR_CANCELLED = 1223
SEE_MASK_NOCLOSEPROCESS = 0x00000040
SW_HIDE = 0
INFINITE = 0xFFFFFFFF

MB_YESNO = 0x04
MB_ICONERROR = 0x10
MB_ICONWARNING = 0x30
MB_ICONINFORMATION = 0x40
MB_DEFBUTTON2 = 0x100
MB_SETFOREGROUND = 0x10000
MB_TOPMOST = 0x40000
IDYES = 6

_REBOOT_DELAY_SECONDS = 15


# ──────────────────────────────────────────────
# 路径与权限
# ──────────────────────────────────────────────
def get_app_dir() -> Path:
    """程序所在目录：打包后取 exe 同级目录，源码运行取本文件同级目录。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def find_installer(base_dir: Path | str | None = None) -> Path | None:
    """按发布包 / 源码仓库两种布局查找官方安装器，找不到返回 None。"""
    roots: list[Path] = []
    if base_dir:
        roots.append(Path(base_dir))
    roots.append(get_app_dir())
    try:
        roots.append(Path.cwd())
    except OSError:
        pass

    seen: set[str] = set()
    for root in roots:
        for sub in INSTALLER_SUBDIRS:
            candidate = root / sub / INSTALLER_NAME
            key = str(candidate).lower()
            if key in seen:
                continue
            seen.add(key)
            try:
                if candidate.is_file():
                    return candidate
            except OSError:
                continue
    return None


def is_admin() -> bool:
    """当前进程是否已提权。"""
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


# ──────────────────────────────────────────────
# 驱动安装痕迹检测
# ──────────────────────────────────────────────
def _read_upper_filters(sub_key: str) -> list[str]:
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, sub_key) as key:
            value, _ = winreg.QueryValueEx(key, "UpperFilters")
    except OSError:
        return []
    if isinstance(value, str):
        value = [value]
    return [str(item).strip().lower() for item in value if str(item).strip()]


def _service_key_exists(name: str) -> bool:
    import winreg

    try:
        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            rf"SYSTEM\CurrentControlSet\Services\{name}",
        ):
            return True
    except OSError:
        return False


def _driver_file_exists(name: str) -> bool:
    system_root = os.environ.get("SystemRoot", r"C:\Windows")
    return Path(system_root, "System32", "drivers", name).is_file()


def driver_registry_state() -> dict[str, bool]:
    """返回驱动在系统里的安装痕迹（全部为只读查询，不需要管理员权限）。"""
    keyboard_filters = _read_upper_filters(KEYBOARD_CLASS_KEY)
    mouse_filters = _read_upper_filters(MOUSE_CLASS_KEY)
    return {
        "keyboard_filter": "keyboard" in keyboard_filters,
        "mouse_filter": "mouse" in mouse_filters,
        "keyboard_service": _service_key_exists("keyboard"),
        "mouse_service": _service_key_exists("mouse"),
        "mouse_driver_file": _driver_file_exists("mouse.sys"),
    }


def is_driver_installed() -> bool:
    """鼠标类过滤驱动已登记即视为「已安装」（本程序只走鼠标通道）。

    注意：这只能说明「装过」，不代表「已生效」—— 安装后必须重启。
    真正能否使用仍由 InterceptionCore.is_ready() 决定。
    """
    state = driver_registry_state()
    return bool(state["mouse_filter"] and state["mouse_service"])


# ──────────────────────────────────────────────
# 消息框（不依赖 tkinter，静默启动模式下也可见）
# ──────────────────────────────────────────────
def show_message_box(
    text: str,
    title: str = "RocoKingdom Clicker",
    *,
    error: bool = False,
    info: bool = False,
) -> int:
    """弹一个只有「确定」的消息框。"""
    if error:
        icon = MB_ICONERROR
    elif info:
        icon = MB_ICONINFORMATION
    else:
        icon = MB_ICONWARNING
    try:
        return int(
            ctypes.windll.user32.MessageBoxW(
                0, str(text), str(title), icon | MB_TOPMOST | MB_SETFOREGROUND
            )
        )
    except Exception:
        print(f"\n[{title}]\n{text}")
        return 0


def ask_yes_no(
    text: str,
    title: str = "RocoKingdom Clicker",
    *,
    default_yes: bool = True,
    error: bool = False,
) -> bool:
    """弹「是/否」对话框。default_yes=False 时把默认焦点放到「否」，防误触重启。"""
    flags = MB_YESNO | (MB_ICONERROR if error else MB_ICONWARNING)
    flags |= MB_TOPMOST | MB_SETFOREGROUND
    if not default_yes:
        flags |= MB_DEFBUTTON2
    try:
        result = int(ctypes.windll.user32.MessageBoxW(0, str(text), str(title), flags))
        return result == IDYES
    except Exception:
        # 弹不出对话框时不要擅自安装/重启，一律按「否」处理
        print(f"\n[{title}]\n{text}")
        return False


# ──────────────────────────────────────────────
# 提权执行
# ──────────────────────────────────────────────
class _SHELLEXECUTEINFOW(ctypes.Structure):
    """SHELLEXECUTEINFOW（hIcon 与 hMonitor 是联合体，用 HANDLE 占位即可）。"""

    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("fMask", ctypes.c_ulong),
        ("hwnd", wintypes.HWND),
        ("lpVerb", wintypes.LPCWSTR),
        ("lpFile", wintypes.LPCWSTR),
        ("lpParameters", wintypes.LPCWSTR),
        ("lpDirectory", wintypes.LPCWSTR),
        ("nShow", ctypes.c_int),
        ("hInstApp", wintypes.HINSTANCE),
        ("lpIDList", ctypes.c_void_p),
        ("lpClass", wintypes.LPCWSTR),
        ("hkeyClass", wintypes.HKEY),
        ("dwHotKey", wintypes.DWORD),
        ("hIconOrMonitor", wintypes.HANDLE),
        ("hProcess", wintypes.HANDLE),
    ]


def _run_elevated_hidden(
    file_: str, parameters: str, directory: str
) -> tuple[bool, int]:
    """以 runas 静默启动进程并等待退出，返回 (是否成功启动, 退出码或 GetLastError)。"""
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    shell32.ShellExecuteExW.argtypes = [ctypes.POINTER(_SHELLEXECUTEINFOW)]
    shell32.ShellExecuteExW.restype = wintypes.BOOL
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.GetExitCodeProcess.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.DWORD),
    ]

    info = _SHELLEXECUTEINFOW()
    info.cbSize = ctypes.sizeof(info)
    info.fMask = SEE_MASK_NOCLOSEPROCESS
    info.lpVerb = "runas"
    info.lpFile = str(file_)
    info.lpParameters = str(parameters)
    info.lpDirectory = str(directory)
    info.nShow = SW_HIDE

    if not shell32.ShellExecuteExW(ctypes.byref(info)):
        return False, int(ctypes.get_last_error())

    handle = info.hProcess
    if not handle:
        # 拿不到进程句柄也无从等待，按「已启动、退出码未知」处理
        return True, 0
    try:
        handle = wintypes.HANDLE(handle)
        kernel32.WaitForSingleObject(handle, INFINITE)
        code = wintypes.DWORD(0)
        if kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return True, int(code.value)
        return True, 0
    finally:
        kernel32.CloseHandle(handle)


def _install_succeeded(exit_code: int, output: str) -> bool:
    if exit_code == 0:
        return True
    # 个别版本的安装器退出码不规范，用官方成功文案兜底
    return "successfully installed" in (output or "").lower()


def run_installer(
    installer: Path | str, action: str = "/install", timeout: int = 300
) -> tuple[str, str]:
    """执行官方安装器，返回 (状态, 输出文本)。

    已是管理员 → 直接 subprocess 捕获输出；
    不是管理员 → 套一层 cmd 把输出重定向到临时日志，再整体 runas 提权。
    """
    installer = Path(installer)
    workdir = str(installer.parent)
    no_window = getattr(subprocess, "CREATE_NO_WINDOW", 0)

    if is_admin():
        try:
            proc = subprocess.run(
                [str(installer), action],
                cwd=workdir,
                capture_output=True,
                text=True,
                timeout=timeout,
                creationflags=no_window,
            )
        except Exception as exc:
            logger.error("安装器执行异常: %s", exc)
            return INSTALL_FAILED, f"无法启动安装程序：{exc}"
        output = f"{proc.stdout or ''}{proc.stderr or ''}".strip()
        status = INSTALL_OK if _install_succeeded(proc.returncode, output) else INSTALL_FAILED
        logger.info("安装器 %s 退出码=%s 状态=%s", action, proc.returncode, status)
        return status, output or f"（安装程序退出码 {proc.returncode}，无输出）"

    # 非管理员分支：日志路径必须是绝对路径，这样即使 UAC 里换了一个管理员账号
    # 提权，安装器也仍然把输出写回当前用户能读到的位置。
    log_path = Path(tempfile.gettempdir()) / "roco_interception_driver.log"
    try:
        log_path.unlink()
    except OSError:
        pass

    params = '/c ""{exe}" {act} > "{log}" 2>&1"'.format(
        exe=installer, act=action, log=log_path
    )
    started, code = _run_elevated_hidden("cmd.exe", params, workdir)
    if not started:
        if code == ERROR_CANCELLED:
            logger.info("用户取消了 UAC 提权")
            return INSTALL_CANCELLED, "已在 UAC 提示中取消，驱动未安装。"
        logger.error("提权启动安装器失败，GetLastError=%s", code)
        return INSTALL_FAILED, f"提权启动安装程序失败（错误码 {code}）。"

    output = ""
    try:
        output = log_path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        pass
    status = INSTALL_OK if _install_succeeded(code, output) else INSTALL_FAILED
    logger.info("提权安装器 %s 退出码=%s 状态=%s", action, code, status)
    return status, output or f"（安装程序退出码 {code}，无输出）"


def request_reboot(
    delay_seconds: int = _REBOOT_DELAY_SECONDS,
    reason: str = "Interception 驱动已安装，系统即将重启以生效",
) -> bool:
    """调用 shutdown 计划重启；失败时返回 False，由调用方提示手动重启。"""
    try:
        subprocess.run(
            ["shutdown", "/r", "/t", str(int(delay_seconds)), "/c", reason],
            capture_output=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        logger.info("已计划在 %s 秒后重启", delay_seconds)
        return True
    except Exception as exc:
        logger.error("调用 shutdown 失败: %s", exc)
        return False


# ──────────────────────────────────────────────
# 交互式一键安装流程
# ──────────────────────────────────────────────
def _manual_steps(installer: Path | None) -> str:
    if installer is None:
        return (
            "手动安装步骤：\n"
            "  1) 以【管理员身份】打开 PowerShell 或 CMD\n"
            "  2) 进入 driver_installer 目录并执行：\n"
            "     install-interception.exe /install\n"
            "  3) 重启电脑"
        )
    return (
        "手动安装步骤：\n"
        "  1) 以【管理员身份】打开 PowerShell 或 CMD\n"
        f"  2) 执行：\"{installer}\" /install\n"
        "  3) 重启电脑"
    )


def _do_install(installer: Path, title: str) -> str:
    """真正跑安装器并处理重启提示。"""
    status, output = run_installer(installer, "/install")

    if status == INSTALL_CANCELLED:
        show_message_box(
            "已取消提权，驱动没有安装。\n\n"
            "本程序需要 Interception 驱动才能模拟鼠标点击。\n"
            "想装的时候可以再次运行本程序，或直接双击目录里的 install_driver.bat。\n\n"
            f"{_manual_steps(installer)}",
            title,
            info=True,
        )
        return status

    if status != INSTALL_OK:
        show_message_box(
            f"驱动安装失败：\n\n{output}\n\n{_manual_steps(installer)}",
            title,
            error=True,
        )
        return status

    logger.info("Interception 驱动安装成功：%s", output.replace("\n", " | "))
    reboot_now = ask_yes_no(
        "驱动安装成功！\n\n"
        "必须重启电脑才能生效。\n"
        f"是否立即重启？（{_REBOOT_DELAY_SECONDS} 秒倒计时，期间可用 shutdown /a 取消）\n\n"
        "选「否」请先保存好其它工作，稍后自行重启。",
        "安装完成 - RocoKingdom Clicker",
        default_yes=False,
    )
    if reboot_now and not request_reboot():
        show_message_box(
            "自动重启调用失败，请手动重启电脑后再运行本程序。",
            title,
            error=True,
        )
    elif not reboot_now:
        show_message_box(
            "驱动已安装完成。\n\n请手动重启电脑，然后再运行本程序。",
            title,
            info=True,
        )
    return INSTALL_OK


def offer_one_click_install(
    init_error: str | None = None,
    *,
    title: str = "驱动未就绪 - RocoKingdom Clicker",
    force: bool = False,
) -> str:
    """检测到驱动未就绪时调用，走「询问 → 安装 → 重启」全流程。

    返回 INSTALL_* 状态字符串，调用方据此决定是否退出进程。
    force=True 时跳过「是否安装」的询问直接装（供 install_driver.bat / CLI 用）。
    """
    installer = find_installer()
    already_installed = is_driver_installed()

    if installer is None:
        logger.error("找不到 %s", INSTALLER_NAME)
        show_message_box(
            "找不到驱动安装程序 install-interception.exe，程序文件可能不完整。\n\n"
            "请重新下载并完整解压发布包，或检查以下位置是否存在该文件：\n"
            f"  {get_app_dir() / 'driver_installer' / INSTALLER_NAME}\n\n"
            f"{_manual_steps(None)}",
            title,
            error=True,
        )
        return INSTALL_NO_INSTALLER

    detail = f"\n诊断信息：\n{init_error}\n" if init_error else ""

    if force:
        return _do_install(installer, title)

    if already_installed:
        # 注册表里已经有鼠标类过滤驱动 —— 大概率是「装了但没重启」，
        # 也可能是驱动文件被安全软件清掉了。这时不该无脑重装，先说清楚。
        state = driver_registry_state()
        proceed = ask_yes_no(
            "检测到 Interception 驱动【已经安装】，但仍然无法使用。\n\n"
            "最常见的原因是安装后还没有重启电脑。\n"
            "是否要重新安装一次驱动？（选「否」则只提示重启）\n"
            f"{detail}\n"
            f"注册表状态：{state}",
            title,
            default_yes=False,
        )
        if proceed:
            return _do_install(installer, title)
        if ask_yes_no(
            "是否立即重启电脑？\n\n重启后驱动才会生效。",
            title,
            default_yes=False,
        ):
            request_reboot()
        return INSTALL_DECLINED

    proceed = ask_yes_no(
        "RocoKingdom Clicker 需要 Interception 驱动才能模拟鼠标点击。\n\n"
        "检测到驱动【尚未安装】，是否现在自动安装？\n\n"
        "点「是」后：\n"
        "  · 可能弹出一次 UAC 提权窗口，请点「是」\n"
        "  · 安装过程约几秒钟，全程无需输入命令\n"
        "  · 安装完成后需要重启电脑\n"
        f"{detail}",
        title,
        default_yes=True,
    )
    if not proceed:
        show_message_box(
            "已跳过驱动安装。\n\n"
            "在装好驱动并重启之前，所有点击 / 录制 / 回放操作都会被拒绝。\n"
            "之后可以随时双击程序目录里的 install_driver.bat 重新安装。\n\n"
            f"{_manual_steps(installer)}",
            title,
            info=True,
        )
        return INSTALL_DECLINED

    return _do_install(installer, title)


# ──────────────────────────────────────────────
# 命令行入口（install_driver.bat / 开发者调试用）
# ──────────────────────────────────────────────
def _print_status() -> None:
    state = driver_registry_state()
    print("Interception 驱动状态：")
    print(f"  已安装（注册表痕迹）: {is_driver_installed()}")
    for key, value in state.items():
        print(f"    {key:<18} = {value}")
    print(f"  当前进程管理员权限  : {is_admin()}")
    installer = find_installer()
    print(f"  安装器路径          : {installer or '未找到'}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Interception 驱动一键安装工具")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--install", action="store_true", help="安装驱动（默认）")
    group.add_argument("--uninstall", action="store_true", help="卸载驱动")
    group.add_argument("--status", action="store_true", help="只打印检测状态")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s | %(levelname)-8s | %(message)s"
    )

    if args.status:
        _print_status()
        return 0

    installer = find_installer()
    if installer is None:
        print(f"【错误】找不到 {INSTALLER_NAME}")
        print(f"  已查找：{get_app_dir()}")
        return 1

    action = "/uninstall" if args.uninstall else "/install"
    print(f"正在执行：{installer} {action}")
    status, output = run_installer(installer, action)
    if output:
        print("-" * 40)
        print(output)
        print("-" * 40)

    if status == INSTALL_OK:
        print("成功。必须重启电脑才能生效。")
        return 0
    if status == INSTALL_CANCELLED:
        print("已取消（UAC 未通过）。")
        return 2
    print(f"失败：{status}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
