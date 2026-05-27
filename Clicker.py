"""
连点器主程序 - Interception 版本
使用标准库实现全局按键轮询和日志记录。
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import queue
import logging
import sys
import time
from pathlib import Path
import threading

from InterceptionCore import InterceptionCore
from ConfigManager import ConfigManager
from ActionScript import (
    ActionScriptManager,
    ActionExecutor,
    ClickAction,
    MoveAction,
    KeyAction,
    WaitAction,
    TargetWindowBinding,
    list_windows_for_process,
)


VK_F1 = 0x70
VK_F2 = 0x71
VK_F3 = 0x72
VK_F4 = 0x73
VK_DELETE = 0x2E
VK_0 = 0x30
VK_1 = 0x31
VK_2 = 0x32
VK_3 = 0x33
VK_4 = 0x34
VK_5 = 0x35
VK_6 = 0x36
VK_7 = 0x37
VK_8 = 0x38
VK_9 = 0x39

WH_KEYBOARD_LL = 13
WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101
WM_SYSKEYDOWN = 0x0104
WM_SYSKEYUP = 0x0105
WM_QUIT = 0x0012
HC_ACTION = 0


class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("vkCode", ctypes.c_uint32),
        ("scanCode", ctypes.c_uint32),
        ("flags", ctypes.c_uint32),
        ("time", ctypes.c_uint32),
        ("dwExtraInfo", ctypes.c_void_p),
    ]


def get_app_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def setup_logger():
    """配置日志。"""
    data_dir = get_app_dir() / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-8s | %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(data_dir / "clicker.log", encoding="utf-8"),
        ],
    )


def key_pressed(vk_code: int) -> bool:
    """检测虚拟按键是否被按下。"""
    return bool(ctypes.windll.user32.GetAsyncKeyState(vk_code) & 0x8000)


class GlobalHotkeyListener:
    """全局热键监听器。

    仅记录键盘事件，不会拦截按键，所以游戏原本热键功能仍然保留。
    """

    def __init__(self):
        self._queue: queue.Queue[tuple[str, int]] = queue.Queue()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._thread_id: int | None = None
        self._hook = None
        self._proc = None
        self._pressed_keys: set[int] = set()
        self._logger = logging.getLogger("hotkey_listener")

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return

        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread_id is not None:
            ctypes.windll.user32.PostThreadMessageW(self._thread_id, WM_QUIT, 0, 0)
        if self._thread:
            self._thread.join(timeout=1.0)
        self._thread = None
        self._thread_id = None

    def clear(self) -> None:
        while True:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                return

    def get_event(self, timeout: float = 0.05):
        try:
            return self._queue.get(timeout=timeout)
        except queue.Empty:
            return None

    def _create_proc(self):
        user32 = ctypes.windll.user32

        @ctypes.WINFUNCTYPE(ctypes.c_ssize_t, ctypes.c_int, ctypes.c_size_t, ctypes.c_void_p)
        def keyboard_proc(n_code, w_param, l_param):
            if n_code == HC_ACTION:
                data = ctypes.cast(l_param, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
                vk_code = int(data.vkCode)
                if w_param in (WM_KEYDOWN, WM_SYSKEYDOWN):
                    if vk_code not in self._pressed_keys:
                        self._pressed_keys.add(vk_code)
                        self._queue.put(("down", vk_code))
                elif w_param in (WM_KEYUP, WM_SYSKEYUP):
                    if vk_code in self._pressed_keys:
                        self._pressed_keys.discard(vk_code)
                        self._queue.put(("up", vk_code))

            return user32.CallNextHookEx(self._hook, n_code, w_param, l_param)

        return keyboard_proc

    def _run(self) -> None:
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        kernel32.GetCurrentThreadId.restype = ctypes.c_ulong
        kernel32.GetModuleHandleW.restype = ctypes.c_void_p
        kernel32.GetModuleHandleW.argtypes = [ctypes.c_wchar_p]
        user32.SetWindowsHookExW.restype = ctypes.c_void_p
        user32.SetWindowsHookExW.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulong]
        user32.UnhookWindowsHookEx.restype = ctypes.c_bool
        user32.UnhookWindowsHookEx.argtypes = [ctypes.c_void_p]
        user32.CallNextHookEx.restype = ctypes.c_ssize_t
        user32.CallNextHookEx.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_size_t, ctypes.c_void_p]
        self._thread_id = kernel32.GetCurrentThreadId()
        self._proc = self._create_proc()
        module_handle = kernel32.GetModuleHandleW(None)
        self._hook = user32.SetWindowsHookExW(
            WH_KEYBOARD_LL,
            self._proc,
            module_handle,
            0,
        )

        if not self._hook:
            error_code = ctypes.windll.kernel32.GetLastError()
            self._logger.error("全局热键监听器启动失败，错误码=%s", error_code)
            self._thread_id = None
            return

        msg = wintypes.MSG()
        try:
            while not self._stop_event.is_set():
                result = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
                if result == 0 or result == -1:
                    break
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
        finally:
            if self._hook:
                user32.UnhookWindowsHookEx(self._hook)
                self._hook = None
            self._pressed_keys.clear()
            self._thread_id = None


class ClickerManager:
    """连点器管理类 - 处理热键和用户交互（Interception 版）。"""

    def __init__(self):
        self.logger = logging.getLogger("clicker")
        self.clicker = InterceptionCore()
        self.target_process_name = "NRC-Win64-Shipping.exe"
        self.target_window = TargetWindowBinding(self.target_process_name)
        self.action_executor = ActionExecutor(self.target_window)
        self.action_manager = ActionScriptManager(get_app_dir() / "data")
        self.hotkey_listener = GlobalHotkeyListener()
        self.running = True
        self.listening = False  # 标记是否在监听热键
        self.clicker.set_focus_callback(self._ensure_click_target_foreground)
        self.logger.info("连点器管理器已初始化")

    def _ensure_click_target_foreground(self) -> bool:
        """连点时的前台守护：若已绑定窗口则尝试保持目标窗口在前台。"""
        if not self.target_window.is_bound():
            return True
        activated = self.target_window.activate()
        if not activated:
            self.logger.warning("前台守护失败：%s", self.target_window.describe())
        return activated

    def _on_start(self):
        if not self.clicker.running:
            self.logger.info("连点器将在 3 秒后启动，请切换到游戏窗口...")
            print("\n⏳ 连点器将在 3 秒后启动...")
            for countdown in range(3, 0, -1):
                print(f"   倒计时: {countdown}...")
                time.sleep(1)

            # 如果已绑定目标窗口，启动前先尝试激活窗口
            if self.target_window.is_bound():
                activated = self.target_window.activate()
                self.logger.info("启动前激活目标窗口: %s | 结果=%s", self.target_window.describe(), activated)

            self.clicker.start()
            self.logger.info("连点器已启动")
            print("✓ 连点器已启动！")

    def _on_stop(self):
        if self.clicker.running:
            self.clicker.stop()
            self.logger.info("连点器已停止")

    def _on_exit(self):
        if self.clicker.running:
            self.clicker.stop()
        self.listening = False
        self.logger.info("已退出热键监听，返回主菜单")

    def _on_stats(self):
        stats = self.clicker.get_stats()
        self.logger.info("========== 连点器统计信息 ==========")
        self.logger.info("状态: %s", "运行中" if stats["running"] else "已停止")
        self.logger.info("总点击次数: %s", stats["click_count"])
        self.logger.info("点击中心: (%s, %s)", stats["center_x"], stats["center_y"])
        self.logger.info("随机移动半径: %spx", stats["radius"])
        self.logger.info("点击间隔: %sms", stats["click_interval"])
        self.logger.info("按压持续时间: %sms", stats["hold_duration"])
        self.logger.info("时间抖动范围: %sms", stats["jitter_range"])
        if "duration" in stats:
            self.logger.info("运行时长: %.2f秒", stats["duration"])
            self.logger.info("平均频率: %.2f次/秒", stats["clicks_per_second"])
        self.logger.info("====================================")

    def _countdown_and_activate_target(self, title: str, after_message: str) -> None:
        """统一的启动倒计时流程。"""
        print(f"\n⏳ {title}将在 3 秒后启动，请切换到游戏窗口...")
        for countdown in range(3, 0, -1):
            print(f"   倒计时: {countdown}...")
            time.sleep(1)

        if self.target_window.is_bound():
            activated = self.target_window.activate()
            self.logger.info("启动前激活目标窗口: %s | 结果=%s", self.target_window.describe(), activated)

        print(after_message)

    def _run_script_session(self, script_name: str, actions) -> None:
        """以和连点器类似的方式运行动作脚本。"""
        stop_event = threading.Event()
        pause_event = threading.Event()
        pause_event.set()

        worker = threading.Thread(
            target=self.action_executor.execute_sequence,
            args=(actions, stop_event, pause_event),
            daemon=True,
        )

        self.logger.info("脚本将在 3 秒后启动: %s", script_name)
        self._countdown_and_activate_target(
            f"脚本 {script_name}",
            f"✓ 脚本已启动: {script_name}",
        )
        worker.start()

        print("\n【脚本热键】")
        print("  F1  - 继续脚本（暂停后使用，继续前再次等待 3 秒）")
        print("  F2  - 暂停脚本")
        print("  F4  - 退出脚本并返回菜单")

        paused = False
        self.hotkey_listener.start()
        self.hotkey_listener.clear()
        try:
            while worker.is_alive():
                try:
                    event = self.hotkey_listener.get_event(timeout=0.05)
                    if not event:
                        continue

                    event_type, vk_code = event
                    if event_type != "down":
                        continue

                    if vk_code == VK_F2 and not paused:
                        pause_event.clear()
                        paused = True
                        self.logger.info("脚本已暂停: %s", script_name)
                        print("⏸ 脚本已暂停")

                    elif vk_code == VK_F1 and paused:
                        self.logger.info("脚本将在 3 秒后继续: %s", script_name)
                        print("\n⏳ 脚本将在 3 秒后继续执行，请切换到游戏窗口...")
                        for countdown in range(3, 0, -1):
                            if stop_event.is_set():
                                break
                            print(f"   倒计时: {countdown}...")
                            time.sleep(1)
                        if not stop_event.is_set():
                            if self.target_window.is_bound():
                                activated = self.target_window.activate()
                                self.logger.info("继续前激活目标窗口: %s | 结果=%s", self.target_window.describe(), activated)
                            pause_event.set()
                            paused = False
                            print("▶ 脚本已继续")

                    elif vk_code == VK_F4:
                        stop_event.set()
                        pause_event.set()
                        self.logger.info("脚本会话已终止: %s", script_name)
                        print("■ 脚本已终止，返回菜单")
                        worker.join(timeout=1.0)
                        return
                except Exception as e:
                    self.logger.error("脚本会话异常: %s", e)
                    stop_event.set()
                    pause_event.set()
                    break
        finally:
            self.hotkey_listener.stop()

        if stop_event.is_set():
            return

        print("✓ 脚本已执行完成")

    def show_menu(self):
        """显示主菜单。"""
        print("\n")
        print("╔════════════════════════════════════════╗")
        print("║           ROCOKINGDOM 连点器            ║")
        print("║   基于 Interception 驱动的高效点击工具   ║")
        print("╚════════════════════════════════════════╝")
        print("\n【热键控制】")
        print("  F1  - 启动连点器")
        print("  F2  - 停止连点器")
        print("  F3  - 显示统计信息")
        print("  F4  - 返回菜单")
        print("  Delete + 0..9 - 游戏内快捷键（Delete+0 切换连点器；Delete+1..9 执行脚本）")
        print("\n【默认参数】")
        print(f"  点击中心位置: ({self.clicker.config.center_x}, {self.clicker.config.center_y})")
        print(f"  随机移动半径: {self.clicker.config.radius}px")
        print(f"  点击间隔: {self.clicker.config.click_interval}ms")
        print(f"  按压持续时间: {self.clicker.config.hold_duration}ms")
        print(f"  时间抖动范围: {self.clicker.config.jitter_range}ms")
        print(f"  启动时移动鼠标到目标: {getattr(self.clicker.config, 'move_mouse', True)}")
        print("\n")
        print(f"【脚本目标窗口】{self.target_window.describe()}")
        print("\n")

    def interactive_config(self):
        """交互式配置。"""
        while True:
            print("\n【参数调整】(输入数字选择)")
            print("  1 - 设置点击中心位置")
            print("  2 - 设置随机移动半径")
            print("  3 - 设置点击间隔")
            print("  4 - 设置按压持续时间")
            print("  5 - 设置时间抖动范围")
            print("  6 - 加载配置预设")
            print("  7 - 管理已保存的配置")
            print("  8 - 动作脚本管理（创建/执行/删除脚本）")
            print("  9 - 绑定目标窗口（NRC-Win64-Shipping.exe）")
            print("  0 - 开始监听热键")
            print("  m - 切换是否在每次点击前移动鼠标")
            print("  直接回车 - 进入热键监听")

            choice = input("请选择操作: ").strip()
            if not choice or choice == "0":
                return

            if choice == "1":
                try:
                    x = int(input("请输入点击中心X坐标: "))
                    y = int(input("请输入点击中心Y坐标: "))
                    self.clicker.config.center_x = x
                    self.clicker.config.center_y = y
                    ConfigManager.save_config(self.clicker.config, "default")
                    print(f"✓ 点击中心已设置为 ({x}, {y})")
                except ValueError:
                    print("❌ 输入错误")
            elif choice == "2":
                try:
                    radius = int(input("请输入随机移动半径(像素): "))
                    self.clicker.config.radius = radius
                    ConfigManager.save_config(self.clicker.config, "default")
                    print(f"✓ 随机半径已设置为 {radius}px")
                except ValueError:
                    print("❌ 输入错误")
            elif choice == "3":
                try:
                    interval = int(input("请输入点击间隔(毫秒): "))
                    self.clicker.config.click_interval = interval
                    ConfigManager.save_config(self.clicker.config, "default")
                    print(f"✓ 点击间隔已设置为 {interval}ms")
                except ValueError:
                    print("❌ 输入错误")
            elif choice == "4":
                try:
                    duration = int(input("请输入按压持续时间(毫秒): "))
                    self.clicker.config.hold_duration = duration
                    ConfigManager.save_config(self.clicker.config, "default")
                    print(f"✓ 按压持续时间已设置为 {duration}ms")
                except ValueError:
                    print("❌ 输入错误")
            elif choice == "5":
                try:
                    jitter = int(input("请输入时间抖动范围(毫秒): "))
                    self.clicker.config.jitter_range = jitter
                    ConfigManager.save_config(self.clicker.config, "default")
                    print(f"✓ 时间抖动范围已设置为 {jitter}ms")
                except ValueError:
                    print("❌ 输入错误")
            elif choice == "6":
                self.load_config_menu()
            elif choice.lower() == 'm':
                cur = getattr(self.clicker.config, 'move_mouse', True)
                self.clicker.config.move_mouse = not cur
                ConfigManager.save_config(self.clicker.config, "default")
                print(f"✓ move_mouse 已设置为 {self.clicker.config.move_mouse}")
            elif choice == "7":
                self.manage_configs_menu()
            elif choice == "8":
                self.action_script_menu()
            elif choice == "9":
                self._bind_target_window_menu()
            else:
                self.logger.warning("选择无效，请重新输入")

    def load_config_menu(self):
        """加载预设配置菜单"""
        presets = ConfigManager.list_presets()
        print("\n可用预设:")
        for i, (key, name) in enumerate(presets, 1):
            print(f"  {i}. {name} ({key})")
        choice = input("选择预设编号 (按 Enter 返回): ").strip()
        if not choice:
            return
        try:
            idx = int(choice) - 1
            if 0 <= idx < len(presets):
                key = presets[idx][0]
                cfg = ConfigManager.load_preset(key)
                self.clicker.config = cfg
                ConfigManager.save_config(self.clicker.config, "default")
                print(f"✓ 预设 '{presets[idx][1]}' 已加载并设置为当前配置")
            else:
                print("❌ 编号无效")
        except ValueError:
            print("❌ 输入错误")

    def manage_configs_menu(self):
        """管理已保存的配置（加载/删除）"""
        while True:
            configs = ConfigManager.list_configs()
            print("\n已保存的配置:")
            for i, name in enumerate(configs, 1):
                print(f"  {i}. {name}")
            print("  d<number> - 删除配置，例如 d2")
            print("  b - 返回")
            choice = input("选择操作或编号: ").strip()
            if not choice or choice.lower() == 'b':
                return
            if choice.startswith('d'):
                try:
                    idx = int(choice[1:]) - 1
                    if 0 <= idx < len(configs):
                        name = configs[idx]
                        if ConfigManager.delete_config(name):
                            print(f"✓ 已删除配置: {name}")
                        else:
                            print("❌ 删除失败")
                    else:
                        print("❌ 编号无效")
                except ValueError:
                    print("❌ 输入错误")
                continue
            try:
                idx = int(choice) - 1
                if 0 <= idx < len(configs):
                    name = configs[idx]
                    cfg = ConfigManager.load_config(name)
                    self.clicker.config = cfg
                    ConfigManager.save_config(self.clicker.config, "default")
                    print(f"✓ 已加载配置: {name}")
                    return
                else:
                    print("❌ 编号无效")
            except ValueError:
                print("❌ 输入错误")

    def action_script_menu(self):
        """动作脚本管理（列出/执行/创建/删除）"""
        while True:
            scripts = self.action_manager.list_scripts()
            print("\n动作脚本:")
            if scripts:
                for i, s in enumerate(scripts, 1):
                    print(f"  {i}. {s}")
            else:
                print("  (无脚本)")

            print("  e<number> - 执行脚本，例如 e1")
            print("  c - 创建简单点击脚本")
            print("  d<number> - 删除脚本，例如 d1")
            print("  b - 返回")
            choice = input("选择操作: ").strip()
            if not choice or choice.lower() == 'b':
                return
            if choice.lower().startswith('e'):
                try:
                    idx = int(choice[1:]) - 1
                    if 0 <= idx < len(scripts):
                        name = scripts[idx]
                        actions = self.action_manager.load_script(name)
                        if actions:
                            self._run_script_session(name, actions)
                        else:
                            print("❌ 加载脚本失败")
                    else:
                        print("❌ 编号无效")
                except ValueError:
                    print("❌ 输入错误")
                continue
            if choice.lower().startswith('d'):
                try:
                    idx = int(choice[1:]) - 1
                    if 0 <= idx < len(scripts):
                        name = scripts[idx]
                        path = self.action_manager.scripts_dir / f"{name}.json"
                        try:
                            path.unlink()
                            print(f"✓ 已删除脚本: {name}")
                        except Exception as e:
                            print("❌ 删除失败:", e)
                    else:
                        print("❌ 编号无效")
                except ValueError:
                    print("❌ 输入错误")
                continue
            if choice.lower() == 'c':
                try:
                    name = input("脚本名 (不含扩展名): ").strip()
                    if not name:
                        print("❌ 名称不能为空")
                        continue
                    x = int(input("点击 X 坐标: "))
                    y = int(input("点击 Y 坐标: "))
                    hold = int(input("按住时长 ms (默认100): ") or 100)
                    action = ClickAction(x=x, y=y, hold_ms=hold)
                    self.action_manager.save_script(name, [action])
                    print(f"✓ 已创建脚本: {name}")
                except ValueError:
                    print("❌ 输入错误")
                continue

    def _bind_target_window_menu(self):
        """目标窗口绑定菜单。"""
        print(f"\n正在查询进程 '{self.target_process_name}' 的窗口...")
        windows = list_windows_for_process(self.target_process_name)
        
        if not windows:
            print(f"未找到进程 '{self.target_process_name}' 的窗口")
            return
        
        print("找到以下窗口:")
        for i, (hwnd, title) in enumerate(windows, 1):
            print(f"  {i}. {title} (hwnd={hwnd})")
        
        try:
            choice = input("请选择窗口编号 (按 Enter 取消): ").strip()
            if not choice:
                return
            
            idx = int(choice) - 1
            if 0 <= idx < len(windows):
                hwnd, title = windows[idx]
                self.target_window.bind(hwnd)
                print(f"✓ 已绑定窗口: {title}")
            else:
                print("❌ 编号无效")
        except ValueError:
            print("❌ 输入错误")

    def _on_delete_number(self, number: int):
        """Delete + 0..9 快捷键处理"""
        if number == 0:
            # Delete+0: 切换连点器
            if self.clicker.running:
                self._on_stop()
            else:
                self._on_start()
        else:
            # Delete+1..9: 执行对应脚本
            script_name = f"script_{number}"
            scripts = self.action_manager.list_scripts()
            
            if script_name in scripts:
                actions = self.action_manager.load_script(script_name)
                if actions:
                    self.logger.info("执行脚本: %s", script_name)
                    threading.Thread(
                        target=self.action_executor.execute_sequence,
                        args=(actions,),
                        daemon=True
                    ).start()
            else:
                self.logger.warning("脚本不存在: %s", script_name)

    def listen_hotkeys(self):
        """监听热键。"""
        self.listening = True
        self.logger.info("开始监听热键...")

        self.hotkey_listener.start()
        self.hotkey_listener.clear()

        delete_pressed = False

        try:
            while self.listening:
                try:
                    event = self.hotkey_listener.get_event(timeout=0.05)
                    if not event:
                        continue

                    event_type, vk_code = event

                    if vk_code == VK_DELETE:
                        delete_pressed = event_type == "down"
                        continue

                    if event_type != "down":
                        continue

                    if vk_code == VK_F1:
                        self._on_start()
                        continue

                    if vk_code == VK_F2:
                        self._on_stop()
                        continue

                    if vk_code == VK_F3:
                        self._on_stats()
                        continue

                    if vk_code == VK_F4:
                        self._on_exit()
                        break

                    if delete_pressed and VK_0 <= vk_code <= VK_9:
                        self._on_delete_number(vk_code - VK_0)

                except Exception as e:
                    self.logger.error("热键监听异常: %s", e)
                    time.sleep(0.1)
        finally:
            self.hotkey_listener.stop()

    def run_menu_loop(self):
        """主菜单循环。"""
        self.show_menu()
        self.interactive_config()
        self.logger.info("连点器已准备就绪，监听热键中...")

        try:
            self.listen_hotkeys()
        except KeyboardInterrupt:
            self.logger.info("收到中断信号")

        print("\n")
        print("╔════════════════════════════════════════╗")
        print("║           已返回主菜单                  ║")
        print("╚════════════════════════════════════════╝")
        choice = input("是否继续? [y/n]: ").strip().lower()
        if choice != 'y':
            self.running = False

        ConfigManager.save_config(self.clicker.config, "default")

    def run(self):
        """启动程序。"""
        setup_logger()
        self.clicker.config = ConfigManager.load_config("default")
        self.logger.info("=" * 50)
        self.logger.info("连点器启动（Interception 版）")
        self.logger.info("=" * 50)
        
        try:
            while self.running:
                self.run_menu_loop()
        
        except Exception as e:
            self.logger.error("程序异常: %s", e)
        finally:
            # 清理
            if self.clicker.running:
                self.clicker.stop()
            self.logger.info("连点器已关闭")


def main():
    """主入口"""
    try:
        manager = ClickerManager()
        manager.run()
    except Exception as e:
        logging.error("致命错误: %s", e)
        sys.exit(1)


if __name__ == "__main__":
    main()
