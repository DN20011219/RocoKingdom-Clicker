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
import traceback
from pathlib import Path
import threading
import argparse

from InterceptionCore import InterceptionCore
from ConfigManager import ConfigManager, FKEY_VK, FKEY_SCANCODE, DEFAULT_HOTKEYS
from ActionScript import (
    ActionScriptManager,
    ActionExecutor,
    ClickAction,
    MoveAction,
    KeyAction,
    WaitAction,
)
from InputRecorder import InputRecorder
from PlaybackEngine import PlaybackEngine


VK_F1 = 0x70
VK_F2 = 0x71
VK_F3 = 0x72
VK_F4 = 0x73
VK_F7 = 0x76   # 开始录制（默认，可自定义）
VK_F8 = 0x77   # 停止录制（默认，可自定义）
VK_F9 = 0x78   # 取消录制（默认，可自定义）
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


def show_message_box(text: str, title: str = "RocoKingdom Clicker", error: bool = True) -> int:
    """弹出一个 Windows 消息框（静默启动模式下也可见）。

    MB_OK = 0, MB_ICONERROR = 0x10, MB_ICONWARNING = 0x30, MB_TOPMOST = 0x40000
    """
    try:
        flags = 0
        if error:
            flags |= 0x10  # MB_ICONERROR
        else:
            flags |= 0x30  # MB_ICONWARNING
        flags |= 0x40000  # MB_TOPMOST 置顶显示
        return int(ctypes.windll.user32.MessageBoxW(0, str(text), str(title), flags))
    except Exception:
        # 最后兜底：打印到标准输出，避免无声失败
        print(f"\n[{title}]")
        print(text)
        return 0


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
            ev = self._queue.get(timeout=timeout)
            try:
                self._logger.debug("hotkey consumer: get_event %s", ev)
            except Exception:
                pass
            return ev
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
                        try:
                            self._logger.debug("hotkey producer: put down %s", vk_code)
                        except Exception:
                            pass
                        self._queue.put(("down", vk_code))
                elif w_param in (WM_KEYUP, WM_SYSKEYUP):
                    if vk_code in self._pressed_keys:
                        self._pressed_keys.discard(vk_code)
                        try:
                            self._logger.debug("hotkey producer: put up %s", vk_code)
                        except Exception:
                            pass
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
    """连点器管理类 - 处理热键和用户交互（Interception 版）。

    驱动未安装时不会崩溃，但会在各个入口处显示 MessageBox 提示用户安装驱动。
    """

    def __init__(self, push_toast=None):
        self.logger = logging.getLogger("clicker")
        self.clicker = InterceptionCore()
        self.action_executor = ActionExecutor()
        # 注入 clicker 引用到 action_executor，以便执行器能读取运行时配置（例如 move_mouse）
        try:
            self.action_executor.clicker = self.clicker
        except Exception:
            pass
        self.action_manager = ActionScriptManager(get_app_dir() / "data")
        self.hotkey_listener = GlobalHotkeyListener()
        self.running = True
        self.listening = False  # 标记是否在监听热键
        # 倒计时状态：当程序/脚本在短暂倒计时后启动时，设置为结束时间戳
        self.countdown_end: float | None = None
        self.countdown_label: str | None = None
        # 当前脚本会话信息（供 GUI 查询）
        self.current_script_name: str | None = None
        self.script_running: bool = False
        self.script_paused: bool = False
        self._script_session_lock = threading.Lock()
        self._script_session_thread: threading.Thread | None = None
        self._script_session_stop_event: threading.Event | None = None
        self._script_session_pause_event: threading.Event | None = None
        # Toast notification callback (set by GUI)
        self._push_toast = push_toast

        # ---- 录制/回放引擎 ----
        self._recorder: InputRecorder | None = None
        self._playback: PlaybackEngine | None = None
        self._recording_name: str | None = None   # 当前录制脚本名
        self._recording_session_active: bool = False
        self._playback_name: str | None = None    # 当前回放脚本名

        # ---- 热键配置 ----
        self._hotkeys: dict[str, str] = ConfigManager.load_hotkeys()
        self._hotkey_vk: dict[str, int] = {
            k: FKEY_VK[v] for k, v in self._hotkeys.items()
        }

        # 延迟初始化录制器（需要 clicker.is_ready() 后才能使用）
        self._init_recorder()
        # 应用热键配置到录制器
        self._apply_hotkeys_to_recorder()

        # 启动全局热键监听（GUI 和 CLI 模式通用）
        self._hotkey_dispatch_thread: threading.Thread | None = None
        self._start_hotkey_dispatch()

        if not self.clicker.is_ready():
            self.logger.warning("Interception 驱动未就绪：%s", self.clicker.init_error)
        self.logger.info("连点器管理器已初始化")

    def _show_driver_warning(self):
        """显示驱动未就绪的 MessageBox（带安装步骤），同时返回 False 方便调用方处理。"""
        msg = (
            "Interception 驱动未就绪，无法执行点击操作。\n\n"
            f"{self.clicker.init_error or '请先安装驱动。'}\n\n"
            "安装步骤：\n"
            "  1) 以【管理员身份】运行程序目录下 driver_installer\\install-interception.exe /install\n"
            "  2) 重启电脑后重新运行本程序。"
        )
        self.logger.warning("驱动未就绪，拒绝执行操作：%s", self.clicker.init_error)
        try:
            show_message_box(msg, "驱动未就绪 - RocoKingdom Clicker", error=True)
        except Exception:
            pass
        return False

    # ---- 录制引擎初始化 ----

    def _init_recorder(self):
        """延迟初始化 InputRecorder（需在 Interception 就绪后调用）。"""
        if self._recorder is not None:
            return  # 已有实例
        try:
            self._recorder = InputRecorder(logger=self.logger)
            self._recorder.set_clicker(self.clicker)
            # 设置停止/取消热键回调（在录制线程里检测到热键时触发）
            self._recorder.on_stop = self._on_recording_hotkey_stop
            self._recorder.on_cancel = self._on_recording_hotkey_cancel
            # 设置 overlay 事件回调（把录制事件计数推送到 toast）
            self._recorder._overlay.on_event = self._on_recording_event
            # 应用当前热键配置到录制器
            if hasattr(self, '_hotkeys'):
                self._apply_hotkeys_to_recorder()
            self.logger.info("InputRecorder 初始化完成")
        except Exception as e:
            self.logger.warning("InputRecorder 初始化失败（驱动未就绪？）：%s\n%s",
                                e, traceback.format_exc())
            self._recorder = None

        try:
            self._playback = PlaybackEngine(self.clicker, self.logger)
            # 设置回放完成回调：弹 toast 提示用户
            self._playback.on_complete = self._on_playback_complete
            self.logger.info("PlaybackEngine 初始化完成")
        except Exception as e:
            self.logger.warning("PlaybackEngine 初始化失败：%s\n%s", e, traceback.format_exc())
            self._playback = None

    # ---- 热键配置与全局监听 ----

    def _apply_hotkeys_to_recorder(self):
        """把当前热键配置应用到 InputRecorder。"""
        if self._recorder is None:
            return
        try:
            ok = self._recorder.set_hotkeys(
                start=self._hotkeys["start_recording"],
                stop=self._hotkeys["stop_recording"],
                cancel=self._hotkeys["cancel_recording"],
            )
            if ok:
                self.logger.info("已应用热键配置到 InputRecorder: %s", self._hotkeys)
            else:
                self.logger.warning("应用热键配置到 InputRecorder 失败：录制热键冲突")
        except Exception as e:
            self.logger.warning("应用热键配置到 InputRecorder 失败: %s", e)

    def _start_hotkey_dispatch(self):
        """启动全局热键监听线程（GUI 和 CLI 模式通用）。"""
        if self._hotkey_dispatch_thread and self._hotkey_dispatch_thread.is_alive():
            return
        self.hotkey_listener.start()
        self._hotkey_dispatch_thread = threading.Thread(
            target=self._hotkey_dispatch_loop, daemon=True
        )
        self._hotkey_dispatch_thread.start()
        self.logger.info("全局热键监听已启动")

    def _hotkey_dispatch_loop(self):
        """全局热键事件分发循环（后台线程）。"""
        while True:
            try:
                event = self.hotkey_listener.get_event(timeout=0.1)
                if not event:
                    continue
                event_type, vk_code = event
                if event_type != "down":
                    continue
                self._dispatch_hotkey(vk_code)
            except Exception as e:
                self.logger.error("热键分发循环异常: %s", e)
                time.sleep(0.1)

    def _dispatch_hotkey(self, vk_code: int):
        """根据虚拟键码分发热键事件到对应处理函数。"""
        try:
            if vk_code == self._hotkey_vk["pause_resume"]:
                self._on_pause_resume_hotkey()
            elif vk_code == self._hotkey_vk["start_recording"]:
                if not self._recording_session_active:
                    self.start_recording()
                else:
                    self._toast("已在录制中", duration=1.5)
            elif vk_code == self._hotkey_vk["stop_recording"]:
                if self._recording_session_active:
                    self.stop_recording_and_save()
            elif vk_code == self._hotkey_vk["cancel_recording"]:
                if self._recording_session_active:
                    self.cancel_recording()
        except Exception as e:
            self.logger.error("热键处理异常: %s\n%s", e, traceback.format_exc())

    def _on_pause_resume_hotkey(self):
        """处理暂停/继续热键：优先回放，其次脚本。"""
        # 回放优先
        if self._playback and self._playback.is_playing():
            result = self._playback.toggle_pause()
            if result == "paused":
                self._toast("⏸ 回放已暂停", duration=2.0)
            elif result == "resumed":
                self._toast("▶ 回放已恢复", duration=2.0)
            return
        # 脚本其次
        if self.script_running:
            if self.script_paused:
                self.resume_script()
            else:
                self.pause_script()
            return
        # 连点器最后（CLI 模式 F2 停止连点器）
        if self.clicker.running:
            self._on_stop()

    # ---- 脚本暂停/继续/停止 API（供 GUI 按钮和热键调用） ----

    def pause_script(self) -> bool:
        """暂停当前正在运行的脚本。"""
        if not self.script_running or self.script_paused:
            return False
        with self._script_session_lock:
            if self._script_session_pause_event:
                self._script_session_pause_event.clear()
        self.script_paused = True
        self.logger.info("脚本已暂停: %s", self.current_script_name)
        self._toast("⏸ 脚本已暂停", duration=2.0)
        return True

    def resume_script(self) -> bool:
        """恢复暂停的脚本（3 秒倒计时后继续）。"""
        if not self.script_running or not self.script_paused:
            return False

        def _do_resume():
            try:
                self.logger.info("脚本将在 3 秒后继续: %s", self.current_script_name)
                self._toast("⏳ 脚本即将继续", duration=3.5)
                self.countdown_end = time.time() + 3
                self.countdown_label = "脚本即将继续"
                for i in range(3, 0, -1):
                    if self._script_session_stop_event and self._script_session_stop_event.is_set():
                        break
                    time.sleep(1)
                self.countdown_end = None
                self.countdown_label = None
                if self._script_session_stop_event and not self._script_session_stop_event.is_set():
                    with self._script_session_lock:
                        if self._script_session_pause_event:
                            self._script_session_pause_event.set()
                    self.script_paused = False
                    self._toast("▶ 脚本已继续", duration=2.0)
            except Exception as e:
                self.logger.error("恢复脚本异常: %s\n%s", e, traceback.format_exc())

        threading.Thread(target=_do_resume, daemon=True).start()
        return True

    def stop_script(self) -> bool:
        """停止当前正在运行的脚本（直接终止，不保留进度）。"""
        if not self.script_running:
            return False
        self.logger.info("停止脚本: %s", self.current_script_name)
        self._stop_active_script_session(wait_timeout=2.0)
        self._toast("⏹ 脚本已停止", duration=2.0)
        return True

    # ---- 回放暂停/继续 API（供 GUI 按钮调用） ----

    def pause_playback(self) -> bool:
        """暂停回放。"""
        if self._playback and self._playback.is_playing() and not self._playback.is_paused():
            if self._playback.pause():
                self._toast("⏸ 回放已暂停", duration=2.0)
                return True
        return False

    def resume_playback(self) -> bool:
        """恢复回放。"""
        if self._playback and self._playback.is_paused():
            if self._playback.resume():
                self._toast("▶ 回放已恢复", duration=2.0)
                return True
        return False

    # ---- 热键配置 API ----

    def get_hotkeys(self) -> dict:
        """返回当前热键配置。"""
        return dict(self._hotkeys)

    def set_hotkeys(self, hotkeys: dict) -> bool:
        """更新热键配置并保存。返回是否成功。

        Args:
            hotkeys: {key_name: fkey_str} 字典，例如 {"pause_resume": "F3"}
        """
        try:
            # 验证：所有键必须是已知的，值必须是合法 F-key
            for key, val in hotkeys.items():
                if key not in DEFAULT_HOTKEYS:
                    self.logger.warning("未知热键名: %s", key)
                    return False
                if val not in FKEY_VK:
                    self.logger.warning("非法 F-key: %s", val)
                    return False

            # 检查是否有重复按键（不同功能不能使用同一个键）
            new_hotkeys = dict(self._hotkeys)
            new_hotkeys.update(hotkeys)
            used_keys = list(new_hotkeys.values())
            if len(used_keys) != len(set(used_keys)):
                # 找出冲突的按键，给出更具体的提示
                conflicts = [k for k in set(used_keys) if used_keys.count(k) > 1]
                self._toast(f"❌ 按键冲突：{', '.join(conflicts)} 被多个功能使用", duration=3.0)
                return False

            self._hotkeys = new_hotkeys
            self._hotkey_vk = {k: FKEY_VK[v] for k, v in self._hotkeys.items()}
            ConfigManager.save_hotkeys(self._hotkeys)
            self._apply_hotkeys_to_recorder()
            self.logger.info("热键配置已更新: %s", self._hotkeys)
            self._toast("✓ 热键配置已保存", duration=2.0)
            return True
        except Exception as e:
            self.logger.error("设置热键配置异常: %s\n%s", e, traceback.format_exc())
            return False

    def _on_recording_event(self, msg: str):
        """录制事件回调（由 DebugOverlay 调用）：把消息推送到 toast。"""
        try:
            self._toast(msg, duration=0.8)
        except Exception:
            pass

    def _on_playback_complete(self, success: bool, stopped: bool):
        """回放完成回调（在回放线程里被调用）：弹 toast 提示用户。"""
        try:
            name = self._playback_name or "录制"
            if stopped:
                self._toast(f"⏹ 回放已停止: {name}", duration=3.0)
            elif success:
                self._toast(f"✅ 回放完成: {name}", duration=3.0)
            else:
                self._toast(f"❌ 回放异常: {name}", duration=3.0)
        except Exception:
            pass

    def _on_recording_hotkey_stop(self):
        """停止录制热键回调（在录制线程里被调用）。"""
        self.logger.info("停止录制热键：停止录制并保存")
        # 在新线程里执行保存流程，避免阻塞录制线程
        threading.Thread(
            target=self._do_stop_and_save,
            daemon=True,
        ).start()

    def _on_recording_hotkey_cancel(self):
        """取消录制热键回调（在录制线程里被调用）。"""
        self.logger.info("取消录制热键：取消录制")
        threading.Thread(
            target=self._do_cancel_recording,
            daemon=True,
        ).start()

    def _do_stop_and_save(self):
        """实际执行停止+保存（在新线程里跑，带完整异常捕获）。"""
        try:
            self.stop_recording_and_save()
        except Exception as e:
            self.logger.error("停止录制并保存异常: %s\n%s", e, traceback.format_exc())
            try:
                self._toast(f"❌ 保存失败: {e}", duration=5.0)
            except Exception:
                pass

    def _do_cancel_recording(self):
        """实际执行取消录制（在新线程里跑，带完整异常捕获）。"""
        try:
            self.cancel_recording()
        except Exception as e:
            self.logger.error("取消录制异常: %s\n%s", e, traceback.format_exc())
            try:
                self._toast(f"❌ 取消失败: {e}", duration=5.0)
            except Exception:
                pass

    # ---- 录制控制 API（供 GUI / 热键调用） ----

    def start_recording(self, script_name: str = "recording") -> bool:
        """开始录制输入事件。返回是否成功。"""
        try:
            self.logger.info("start_recording 被调用: name=%s", script_name)

            if self._recorder is None:
                self.logger.info("recorder 未初始化，调用 _init_recorder()")
                self._init_recorder()

            if self._recorder is None:
                self.logger.error("录制失败：InputRecorder 初始化失败")
                self._toast("❌ 录制器初始化失败", duration=4.0)
                return False

            if not self.clicker.is_ready():
                self.logger.error("录制失败：Interception 驱动未就绪")
                self._show_driver_warning()
                return False

            if self._recording_session_active:
                self.logger.warning("录制已在进行中，忽略重复调用")
                return False

            # 停止其他脚本/连点器
            if self.clicker.running:
                self.logger.info("停止连点器以开始录制")
                self.clicker.stop()
            self._stop_active_script_session(wait_timeout=1.0)
            if self._playback and self._playback.is_playing():
                self.logger.info("停止回放以开始录制")
                self._playback.stop()

            self._recording_name = script_name
            self._recording_session_active = True
            self.logger.info("调用 recorder.start() …")
            self._recorder.start()
            self.logger.info("录制开始：%s", script_name)
            stop_key = self._hotkeys.get("stop_recording", "F8")
            cancel_key = self._hotkeys.get("cancel_recording", "F9")
            print(f"\n🎙 录制已开始: {script_name}")
            print("  移动鼠标、点击、按键都会被记录...")
            print(f"  {stop_key} — 停止录制并保存")
            print(f"  {cancel_key} — 取消录制（不保存）")
            self._toast(f"🎙 录制开始: {script_name}\n{stop_key} 停止保存 / {cancel_key} 取消", duration=4.0)
            return True
        except Exception as e:
            self.logger.error("start_recording 异常: %s\n%s", e, traceback.format_exc())
            self._recording_session_active = False
            self._toast(f"❌ 录制启动失败: {e}", duration=5.0)
            return False

    def stop_recording_and_save(self, script_name: str | None = None) -> str | None:
        """停止录制并弹出保存对话框。返回保存的文件路径，失败返回 None。"""
        try:
            if not self._recording_session_active or self._recorder is None:
                self.logger.warning("停止录制：当前未在录制")
                return None

            name = script_name or self._recording_name or "recording"
            self.logger.info("停止录制：开始获取事件…")
            events = self._recorder.stop()
            self._recording_session_active = False
            self.logger.info("停止录制：收到 %d 条事件", len(events) if events else 0)

            if not events:
                self.logger.warning("录制结束但无事件")
                self._toast("录制结束：无事件（未检测到输入）", duration=3.0)
                return None

            # 弹出 tkinter 对话框让用户输入脚本名
            self.logger.info("保存录制：调用 save() …")
            saved_path = self._recorder.save(events, name)
            self.logger.info("录制已保存: %s（共 %d 条事件）", saved_path, len(events))
            self._toast(f"💾 录制已保存: {saved_path.name}\n共 {len(events)} 条事件", duration=4.0)
            return str(saved_path)
        except Exception as e:
            self.logger.error("停止录制并保存异常: %s\n%s", e, traceback.format_exc())
            self._toast(f"❌ 保存失败: {e}", duration=5.0)
            return None

    def cancel_recording(self) -> None:
        """取消当前录制（不保存）。"""
        try:
            if not self._recording_session_active or self._recorder is None:
                self.logger.warning("取消录制：当前未在录制")
                return
            self.logger.info("取消录制：调用 recorder.stop() …")
            self._recorder.stop()
            self._recording_session_active = False
            self.logger.info("录制已取消")
            print("\n❌ 录制已取消（未保存）")
            self._toast("录制已取消", duration=2.0)
        except Exception as e:
            self.logger.error("取消录制异常: %s\n%s", e, traceback.format_exc())
            self._toast(f"❌ 取消失败: {e}", duration=5.0)

    def is_recording(self) -> bool:
        """返回当前是否在录制中。"""
        return self._recording_session_active

    def get_recording_status(self) -> dict:
        """返回录制相关状态（供 GUI 查询）。"""
        return {
            "recording": self._recording_session_active,
            "recording_name": self._recording_name,
            "playback_active": bool(self._playback and self._playback.is_playing()),
        }

    # ---- 回放控制 API ----

    def play_recording(self, filepath: str | Path, speed: float = 1.0,
                       loop_count: int = 1, loop_delay: float = 0.0) -> bool:
        """加载并回放录制脚本。返回是否成功启动。

        所有失败路径都会通过 toast 提示用户。

        Args:
            filepath: 脚本文件路径
            speed: 回放速度倍率（1.0 = 原速）
            loop_count: 循环次数（1=单次，0=无限循环，>1=指定次数）
            loop_delay: 每轮循环间隔秒数
        """
        def toast(msg: str, duration: float = 4.0):
            self._toast(msg, duration)
            self.logger.info("play_recording: %s", msg)

        if self._playback is None:
            self._init_recorder()

        if self._playback is None:
            toast("❌ PlaybackEngine 初始化失败", duration=5.0)
            return False

        if not self.clicker.is_ready():
            toast("❌ Interception 驱动未就绪", duration=5.0)
            self._show_driver_warning()
            return False

        # 先停止其他会话
        if self.clicker.running:
            self.clicker.stop()
        self._stop_active_script_session(wait_timeout=1.0)
        if self._playback.is_playing():
            self._playback.stop()
            time.sleep(0.2)

        path = Path(filepath)
        if not path.exists():
            toast(f"❌ 脚本文件不存在: {filepath}", duration=5.0)
            return False

        if not self._playback.load(path):
            toast(f"❌ 加载脚本失败: {path.name}", duration=5.0)
            return False

        # 兼容性检测（如果方法存在）
        check_compat = getattr(self._playback, "check_compat", None)
        if callable(check_compat):
            try:
                warnings = check_compat()
                if warnings:
                    toast("⚠ 兼容性警告: " + "; ".join(warnings), duration=5.0)
            except Exception as e:
                self.logger.warning("check_compat 异常: %s", e)

        self._playback.speed = speed
        # 设置循环回放参数
        self._playback.set_loop_config(count=loop_count, delay=loop_delay)
        loop_label = "无限" if loop_count == 0 else str(loop_count)
        if self._playback.start():
            meta = self._playback.get_meta()
            name = meta.get("name", path.stem)
            self._playback_name = name
            events_list = getattr(self._playback, "_events", [])
            n_events = len(events_list)
            loop_info = f"，循环 {loop_label} 次" if loop_count != 1 else ""
            toast(f"▶ 回放启动: {name}（{n_events} 条事件，{speed}x{loop_info}）", duration=3.0)
            return True

        toast("❌ 回放启动失败（start() 返回 False）", duration=5.0)
        return False

    def stop_playback(self) -> None:
        """立即停止回放。"""
        if self._playback and self._playback.is_playing():
            self._playback.stop()
            print("\n⏹ 回放已停止")
            self._toast("回放已停止", duration=2.0)

    def _toast(self, text: str, duration: float = 3.0):
        """Show a floating toast notification (thread-safe, no-op if no GUI)."""
        try:
            if self._push_toast:
                self._push_toast(text, duration)
        except Exception:
            pass

    def _on_start(self):
        if not self.clicker.is_ready():
            self._show_driver_warning()
            return
        if not self.clicker.running:
            self.logger.info("连点器将在 3 秒后启动，请切换到游戏窗口...")
            print("\n⏳ 连点器将在 3 秒后启动...")
            self._toast("⏳ 连点器即将启动", duration=3.5)
            # 设置倒计时信息供 GUI 查询
            try:
                self.countdown_end = time.time() + 3
                self.countdown_label = "连点器即将启动"
            except Exception:
                self.countdown_end = None
                self.countdown_label = None
            for countdown in range(3, 0, -1):
                print(f"   倒计时: {countdown}...")
                time.sleep(1)

            # 清除倒计时并启动
            self.countdown_end = None
            self.countdown_label = None
            self.clicker.start()
            self.logger.info("连点器已启动")
            print("✓ 连点器已启动！")
        elif self.clicker.is_paused():
            # 旧的全局定时逻辑已移除，脚本层面的定时请使用 timed 动作

            self.logger.info("连点器将在 3 秒后恢复，请切换到游戏窗口...")
            print("\n⏳ 连点器将在 3 秒后恢复...")
            self._toast("⏳ 连点器即将恢复", duration=3.5)
            try:
                self.countdown_end = time.time() + 3
                self.countdown_label = "连点器将恢复运行"
            except Exception:
                self.countdown_end = None
                self.countdown_label = None
            for countdown in range(3, 0, -1):
                print(f"   倒计时: {countdown}...")
                time.sleep(1)

            self.countdown_end = None
            self.countdown_label = None
            if self.clicker.resume():
                print("▶ 连点器已恢复！")

    

    def _on_stop(self):
        if self.clicker.running:
            self.clicker.stop()
            self.logger.info("连点器已停止")
            self._toast("⏹ 连点器已停止", duration=2.0)

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
        # 旧的全局定时模式已废弃，使用脚本层面的 timed 动作
        self.logger.info("暂停状态: %s", "是" if stats.get("paused") else "否")
        if "duration" in stats:
            self.logger.info("运行时长: %.2f秒", stats["duration"])
            self.logger.info("平均频率: %.2f次/秒", stats["clicks_per_second"])
        self.logger.info("====================================")

    def _stop_active_script_session(self, wait_timeout: float = 5.0) -> bool:
        """停止当前正在运行的脚本会话，并尽量等待其退出。"""
        with self._script_session_lock:
            active_thread = self._script_session_thread
            stop_event = self._script_session_stop_event
            pause_event = self._script_session_pause_event

        if active_thread is None:
            return True

        if stop_event is not None:
            stop_event.set()
        if pause_event is not None:
            pause_event.set()

        if active_thread.is_alive() and active_thread is not threading.current_thread():
            active_thread.join(timeout=wait_timeout)

        alive = active_thread.is_alive()
        if alive:
            self.logger.warning("旧脚本会话未能在 %.1f 秒内退出", wait_timeout)
            return False

        with self._script_session_lock:
            if self._script_session_thread is active_thread:
                self._script_session_thread = None
                self._script_session_stop_event = None
                self._script_session_pause_event = None

        return True

    def _run_script_session(self, script_name: str, actions) -> None:
        """以和连点器类似的方式运行动作脚本。

        热键（暂停/继续/停止）由全局热键分发线程处理，这里只负责启动 worker 并等待完成。
        """
        if not self.clicker.is_ready():
            self._show_driver_warning()
            return
        if not self._stop_active_script_session():
            print(f"❌ 旧脚本会话未能及时退出，已取消启动新脚本: {script_name}")
            return

        stop_event = threading.Event()
        pause_event = threading.Event()
        pause_event.set()

        worker = threading.Thread(
            target=self.action_executor.execute_sequence,
            args=(actions, stop_event, pause_event),
            daemon=True,
        )

        with self._script_session_lock:
            self._script_session_thread = worker
            self._script_session_stop_event = stop_event
            self._script_session_pause_event = pause_event

        self.logger.info("脚本将在 3 秒后启动: %s", script_name)
        print(f"\n⏳ 脚本 {script_name} 将在 3 秒后启动...")
        self._toast(f"⏳ 脚本 {script_name} 即将启动", duration=3.5)
        # 设置倒计时信息供 GUI 查询
        try:
            self.countdown_end = time.time() + 3
            self.countdown_label = f"脚本 {script_name} 即将启动"
        except Exception:
            self.countdown_end = None
            self.countdown_label = None
        for countdown in range(3, 0, -1):
            print(f"   倒计时: {countdown}...")
            time.sleep(1)
        # 清除倒计时并启动
        self.countdown_end = None
        self.countdown_label = None
        print(f"✓ 脚本已启动: {script_name}")
        # 标记脚本会话状态
        self.current_script_name = script_name
        self.script_running = True
        self.script_paused = False
        worker.start()

        pause_key = self._hotkeys.get("pause_resume", "F2")
        print(f"\n【脚本热键】{pause_key} - 暂停/继续脚本")

        # 等待 worker 完成（热键由全局分发线程处理）
        worker.join()

        # 清理脚本会话状态
        with self._script_session_lock:
            if self._script_session_thread is worker:
                self._script_session_thread = None
                self._script_session_stop_event = None
                self._script_session_pause_event = None
                self.script_running = False
                self.script_paused = False
                self.current_script_name = None

        if stop_event.is_set():
            return

        print("✓ 脚本已执行完成")
        self._toast("✓ 脚本已执行完成", duration=3.0)

    def show_menu(self):
        """显示主菜单。"""
        print("\n")
        print("╔════════════════════════════════════════╗")
        print("║           ROCOKINGDOM 连点器            ║")
        print("║   基于 Interception 驱动的高效点击工具   ║")
        print("╚════════════════════════════════════════╝")
        print("\n【热键控制】（可在 GUI 中自定义）")
        print(f"  {self._hotkeys['pause_resume']:4s}  - 暂停/继续 脚本/回放")
        print(f"  {self._hotkeys['start_recording']:4s}  - 开始录制输入")
        print(f"  {self._hotkeys['stop_recording']:4s}  - 停止录制并保存")
        print(f"  {self._hotkeys['cancel_recording']:4s}  - 取消录制（不保存）")
        print("\n【默认参数】")
        print(f"  点击中心位置: ({self.clicker.config.center_x}, {self.clicker.config.center_y})")
        print(f"  随机移动半径: {self.clicker.config.radius}px")
        print(f"  点击间隔: {self.clicker.config.click_interval}ms")
        print(f"  按压持续时间: {self.clicker.config.hold_duration}ms")
        print(f"  时间抖动范围: {self.clicker.config.jitter_range}ms")
        print(f"  启动时移动鼠标到目标: {getattr(self.clicker.config, 'move_mouse', True)}")
        # 全局定时模式已移除，建议使用脚本中的 timed 动作
        print("\n")

    def interactive_config(self):
        """交互式配置。"""
        while True:
            print("\n【参数调整】(输入数字选择)")
            print("  6 - 加载配置预设")
            print("  7 - 管理已保存的配置")
            print("  8 - 动作脚本管理（创建/执行/删除脚本）")
            
            print("  0 - 开始监听热键")
            print("  m - 切换是否在每次点击前移动鼠标")
            print("  直接回车 - 进入热键监听")

            choice = input("请选择操作: ").strip()
            if not choice or choice == "0":
                return

            if choice == "6":
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
            # 已移除绑定目标窗口的交互菜单，改用脚本或手动绑定
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

    # 目标窗口绑定交互已移除（使用脚本或手动绑定目标窗口）

    # Delete + 0..9 快捷键处理已移除（GUI 操作替代）

    def listen_hotkeys(self):
        """CLI 模式：保持程序运行（热键由全局分发线程处理）。"""
        self.listening = True
        self.logger.info("开始监听热键（全局分发线程已启动）...")
        print("\n监听热键中... 按 Ctrl+C 退出。")
        try:
            while self.listening:
                time.sleep(0.5)
        except KeyboardInterrupt:
            self.logger.info("收到中断信号")
            self.listening = False

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
        parser = argparse.ArgumentParser(description="RocoKingdom Clicker")
        parser.add_argument('--gui', action='store_true', help='启动图形界面')
        args = parser.parse_args()
        # 如果是打包后的可执行文件（frozen），默认打开 GUI 窗口以匹配发布版行为
        if getattr(sys, 'frozen', False) and not args.gui:
            args.gui = True

        # 先尝试初始化一次，以检查驱动/DLL 是否就绪
        setup_logger()
        probe = InterceptionCore()
        if not probe.is_ready():
            msg = (
                "RocoKingdom Clicker 需要 Interception 驱动才能工作。\n\n"
                f"{probe.init_error or '无法加载 interception.dll。'}\n\n"
                "安装步骤：\n"
                "  1) 以【管理员身份】运行本程序目录中的 driver_installer\\install-interception.exe /install\n"
                "  2) 重启电脑后再运行本程序。\n\n"
                "（如果你已安装驱动，可能只是还没重启；或安装程序所在路径不正确。）"
            )
            show_message_box(msg, "驱动未就绪 - RocoKingdom Clicker", error=True)
            logging.error("Interception 初始化失败：%s", probe.init_error)
            sys.exit(1)
        del probe

        if args.gui:
            # 延迟导入 GUI，以避免在非 GUI 模式下引入额外依赖
            try:
                import gui
                gui.start_gui()
                return
            except Exception as e:
                logging.error("启动 GUI 失败: %s", e)
                show_message_box(
                    f"图形界面启动失败：\n{e}\n\n将使用命令行模式继续。",
                    "GUI 启动失败 - RocoKingdom Clicker",
                    error=True,
                )

        manager = ClickerManager()
        manager.run()
    except Exception as e:
        logging.error("致命错误: %s", e)
        show_message_box(
            f"程序启动时发生错误：\n{e}\n\n请先以管理员身份运行 driver_installer\\install-interception.exe /install\n安装驱动并重启电脑。",
            "启动失败 - RocoKingdom Clicker",
            error=True,
        )
        sys.exit(1)


if __name__ == "__main__":
    main()
