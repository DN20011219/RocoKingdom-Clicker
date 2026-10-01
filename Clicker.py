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
from dataclasses import fields as dataclass_fields

from InterceptionCore import InterceptionCore
from ConfigManager import (
    ConfigManager,
    DEFAULT_MUSIC_PLAYER,
    FKEY_VK,
    FKEY_SCANCODE,
    DEFAULT_HOTKEYS,
    DEFAULT_ANCHOR_MODE,
)
from ActionScript import (
    ActionScriptManager,
    ActionExecutor,
    ClickAction,
    MoveAction,
    KeyAction,
    WaitAction,
    RegionClickAction,
)
from InputRecorder import InputRecorder
from PlaybackEngine import PlaybackEngine
from MidiScore import (
    KeyLayout,
    MidiParseError,
    MusicLibrary,
    PerformanceOptions,
    best_shift,
    build_performance,
    check_score,
    note_name,
    parse_midi,
)
from MusicPlayer import MusicPlayer
import DriverInstaller


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


def _report_driver_not_ready(core: InterceptionCore, *, interactive: bool = False) -> str:
    """按失败原因分类提示驱动未就绪，返回 DriverInstaller 的状态字符串。

    - dll_missing        ：打包/解压不完整，重装驱动没用 → 只提示重新下载
    - driver_install_hint：驱动未装或没重启 → interactive 时直接走一键安装
    - 其它（如找不到鼠标设备）：给出具体原因即可
    """
    title = "驱动未就绪 - RocoKingdom Clicker"
    detail = core.init_error or "未知原因"

    if core.dll_missing:
        show_message_box(
            f"程序文件不完整，无法工作。\n\n{detail}",
            "文件缺失 - RocoKingdom Clicker",
            error=True,
        )
        return DriverInstaller.INSTALL_NO_INSTALLER

    if core.driver_install_hint:
        if interactive:
            return DriverInstaller.offer_one_click_install(detail, title=title)
        show_message_box(
            f"Interception 驱动未就绪，无法执行点击操作。\n\n{detail}\n\n"
            "请重新运行本程序，在弹出的对话框里选择自动安装；\n"
            "或双击程序目录里的 install_driver.bat。",
            title,
            error=True,
        )
        return DriverInstaller.INSTALL_DECLINED

    show_message_box(f"Interception 无法工作。\n\n{detail}", title, error=True)
    return DriverInstaller.INSTALL_FAILED


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
        # 加载持久化配置（含 move_mouse 开关），避免每次启动都回退到默认值
        self.clicker = InterceptionCore(ConfigManager.load_config("default"))
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
        # 区域录制请求队列：热键在后台线程触发，Tk 窗口只能在主线程创建，
        # 所以这里只投递请求，由 GUI 的 _refresh_loop 在主线程排空（与 toast 队列同模式）
        self._region_request_queue: queue.Queue = queue.Queue()
        # Toast notification callback (set by GUI)
        self._push_toast = push_toast

        # ---- 录制/回放引擎 ----
        self._recorder: InputRecorder | None = None
        self._playback: PlaybackEngine | None = None
        self._recording_name: str | None = None   # 当前录制脚本名
        self._recording_session_active: bool = False
        self._playback_name: str | None = None    # 当前回放脚本名

        # ---- MIDI 自动演奏 ----
        # 曲谱库固定在 data/music；解析结果按 (路径, mtime) 缓存，
        # 面板每次刷新都重新解析一个几 MB 的 MIDI 会明显卡顿
        self.music_library = MusicLibrary(get_app_dir() / "data")
        # 启动即建好 data/music，用户能直接往里丢 .mid 文件
        try:
            self.music_library.ensure_dir()
        except Exception as e:
            self.logger.warning("创建曲谱目录失败: %s", e)
        self._music_player: MusicPlayer | None = None
        self._music_name: str | None = None
        self._music_cache: dict[str, tuple[float, object]] = {}
        # 倒计时期间用户点了停止：靠这个事件把还没开演的会话掐掉
        self._music_cancel = threading.Event()

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

        # 驱动未就绪的一键安装引导只主动弹一次（见 _show_driver_warning）
        self._driver_prompt_done = False

        if not self.clicker.is_ready():
            self.logger.warning("Interception 驱动未就绪：%s", self.clicker.init_error)
        self.logger.info("连点器管理器已初始化")

    def _show_driver_warning(self):
        """驱动未就绪时的统一提示入口，返回 False 方便调用方直接 return。

        能一键安装的场景直接交给 DriverInstaller 弹「是/否」引导用户装驱动，
        不再只给一段需要手敲命令的说明。
        """
        self.logger.warning("驱动未就绪，拒绝执行操作：%s", self.clicker.init_error)
        # 安装流程会连着弹好几个模态框、甚至计划重启电脑；同一个会话里只主动
        # 引导一次，之后按热键只记日志，避免用户被反复打断。
        if self._driver_prompt_done:
            return False
        self._driver_prompt_done = True
        try:
            _report_driver_not_ready(self.clicker, interactive=True)
        except Exception as exc:
            self.logger.error("驱动提示流程异常: %s", exc)
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
            # 设置锚点回调（F12 按下时触发，直接弹 toast）
            self._recorder.on_anchor = self._on_anchor_marked
            # 设置 overlay 事件回调（把录制事件计数推送到 toast）
            self._recorder._overlay.on_event = self._on_recording_event
            # 应用当前热键配置到录制器
            if hasattr(self, '_hotkeys'):
                self._apply_hotkeys_to_recorder()
            # 应用锚点模式配置到录制器
            self._recorder.anchor_mode = ConfigManager.load_anchor_mode()
            self.logger.info("InputRecorder 初始化完成（锚点模式: %s）", self._recorder.anchor_mode)
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
                anchor=self._hotkeys.get("mark_anchor", "F12"),
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
        while self.running:
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
            # 演奏期间只认暂停/继续热键。注入的按键同样会被低级键盘钩子看到，
            # 如果用户把某个音符绑到了 F1-F12，演奏到那个音就会顺带触发录制或圈选。
            if ((self.is_music_playing() or self.is_music_counting_down())
                    and vk_code != self._hotkey_vk.get("pause_resume")):
                return
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
            elif vk_code == self._hotkey_vk.get("record_region"):
                # 互斥检查在 request_region_capture 内部，与面板按钮共用同一条路径
                self.request_region_capture("drag")
        except Exception as e:
            self.logger.error("热键处理异常: %s\n%s", e, traceback.format_exc())

    def _on_pause_resume_hotkey(self):
        """处理暂停/继续热键：依次判断演奏、回放、脚本、连点器。"""
        # MIDI 演奏优先（它和其他会话互斥，且最需要立刻止住声音）
        if self.is_music_playing():
            if self._music_player.is_paused():
                self.resume_music_play()
            else:
                self.pause_music_play()
            return
        if self.is_music_counting_down():
            self.stop_music_play()
            return
        # 回放其次
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

    # ---- 窗口区域连点 API（供 GUI 第四栏调用）----

    @staticmethod
    def _region_text(config: dict) -> str:
        """区域的可读表示：800x600 @ (100,200)。"""
        try:
            return "{}x{} @ ({},{})".format(
                int(config.get("region_width", 0)), int(config.get("region_height", 0)),
                int(config.get("region_x", 0)), int(config.get("region_y", 0)),
            )
        except (TypeError, ValueError):
            return "未设置"

    def _build_region_action(self, config: dict) -> RegionClickAction | None:
        """把参数字典转为 RegionClickAction（只保留数据类认识的字段）。"""
        valid = {f.name for f in dataclass_fields(RegionClickAction)}
        kwargs = {k: v for k, v in config.items() if k in valid}
        try:
            return RegionClickAction(**kwargs)
        except Exception as e:
            self.logger.error("构造区域连点动作失败: %s", e)
            return None

    def get_region_click_config(self) -> dict:
        """返回当前保存的区域连点参数（含区域预设列表）。"""
        config = ConfigManager.load_region_click()
        config["region_presets"] = ConfigManager.list_region_presets()
        return config

    def save_region_click_config(self, config: dict) -> bool:
        """保存区域连点参数。

        先与已落盘的配置合并，避免面板未编辑的高级字段（如 path_params）
        在“保存参数”时被静默丢弃。
        """
        merged = ConfigManager.load_region_click()
        merged.update(config or {})
        return ConfigManager.save_region_click(merged)

    def list_region_presets(self) -> list:
        """列出所有区域预设名称。"""
        return ConfigManager.list_region_presets()

    def get_region_preset(self, name: str) -> dict | None:
        """按名称取区域预设。"""
        return ConfigManager.get_region_preset(name)

    def save_region_preset(self, name: str, region: dict, note: str = "") -> bool:
        """保存一个区域预设。"""
        ok = ConfigManager.save_region_preset(name, region or {}, note)
        if ok:
            self._toast(f"✓ 区域预设已保存：{name}", duration=2.5)
        else:
            self._toast("❌ 区域预设保存失败（名称为空或尺寸非法）", duration=3.0)
        return ok

    def delete_region_preset(self, name: str) -> bool:
        """删除一个区域预设。"""
        ok = ConfigManager.delete_region_preset(name)
        self._toast(f"✓ 已删除区域预设：{name}" if ok else "❌ 区域预设不存在", duration=2.5)
        return ok

    def _region_capture_blocker(self) -> str | None:
        """返回阻止区域圈选的原因；允许时返回 None。"""
        if self._recording_session_active:
            return "⚠ 正在录制，请先停止"
        if self.script_running:
            return "⚠ 已有脚本在运行，请先停止"
        if self._playback and self._playback.is_playing():
            return "⚠ 回放进行中，请先停止"
        if self.is_music_playing() or self.is_music_counting_down():
            return "⚠ 正在演奏 MIDI，请先停止"
        return None

    def request_region_capture(self, mode: str = "drag") -> bool:
        """投递一个区域录制请求（由 GUI 主线程排空后创建圈选窗口）。

        mode: "drag" = 全屏拖拽圈选；"window" = 拾取光标下的窗口。
        返回是否成功投递（被互斥检查拦下时为 False）。

        互斥检查放在这里而不是各个调用点：圈选会弹出覆盖全屏的 Tk 窗口，
        F6 热键和面板上的捕获按钮（拖拽圈选 / 拾取窗口）必须走同一条路径。
        曾经只有热键这一侧做检查，从按钮进来就能在脚本运行中把覆盖层
        盖到游戏上。
        """
        if mode not in ("drag", "window"):
            mode = "drag"
        blocker = self._region_capture_blocker()
        if blocker:
            self._toast(blocker, duration=2.5)
            self.logger.info("区域圈选被拒绝: %s", blocker)
            return False
        self._region_request_queue.put(mode)
        if mode == "drag":
            self._toast("📐 请拖拽圈选目标区域", duration=2.5)
        else:
            self._toast("🪟 3 秒后拾取鼠标所在窗口", duration=3.0)
        self.logger.info("已投递区域录制请求: %s", mode)
        return True

    def pop_region_request(self) -> str | None:
        """取出一个区域录制请求（无请求返回 None）。"""
        try:
            return self._region_request_queue.get_nowait()
        except queue.Empty:
            return None

    def start_region_click(self, params: dict | None = None) -> bool:
        """启动窗口区域连点。

        复用脚本会话（_run_script_session），因此自动获得 F2 暂停/继续、停止按钮
        与状态显示能力。移动全程使用相对位移注入，不受全局 move_mouse 开关影响。
        """
        config = ConfigManager.load_region_click()
        if params:
            for key in list(config.keys()):
                if key in params and params[key] is not None:
                    config[key] = params[key]

        # 预设名优先：只给了预设名、没给尺寸时，用预设回填区域
        preset_name = str(config.get("region_preset") or "").strip()
        if preset_name and int(config.get("region_width", 0) or 0) <= 0:
            preset = ConfigManager.get_region_preset(preset_name)
            if preset:
                for src, dst in (("x", "region_x"), ("y", "region_y"),
                                 ("width", "region_width"), ("height", "region_height")):
                    if src in preset:
                        config[dst] = preset[src]
            else:
                self._toast(f"❌ 区域预设不存在：{preset_name}", duration=3.0)
                return False

        try:
            width = int(config.get("region_width", 0))
            height = int(config.get("region_height", 0))
        except (TypeError, ValueError):
            width = height = 0
        if width <= 0 or height <= 0:
            self._toast("❌ 请先圈选或填写目标区域", duration=3.0)
            return False

        if not self.clicker.is_ready():
            self._show_driver_warning()
            return False
        if self.is_music_playing() or self.is_music_counting_down():
            self._toast("⚠ 正在演奏 MIDI，请先停止", duration=2.5)
            return False
        if self.script_running:
            self._toast("⚠ 已有脚本在运行，请先停止", duration=2.5)
            return False
        if self._playback and self._playback.is_playing():
            self._toast("⚠ 正在回放，请先停止", duration=2.5)
            return False
        if self._recording_session_active:
            self._toast("⚠ 正在录制，请先停止", duration=2.5)
            return False
        if not getattr(self.clicker.config, "move_mouse", True):
            self._toast("ℹ 区域连点以移动为核心，不受「启动时移动鼠标」开关影响", duration=3.5)

        action = self._build_region_action(config)
        if action is None:
            self._toast("❌ 区域连点参数非法", duration=3.0)
            return False

        name = f"窗口连点 {self._region_text(config)}"
        # _run_script_session 含 3 秒倒计时与 worker.join()，会阻塞，所以放到后台线程
        threading.Thread(
            target=self._run_script_session, args=(name, [action]), daemon=True
        ).start()
        return True

    def stop_region_click(self) -> bool:
        """停止窗口区域连点。"""
        if not self.script_running:
            return False
        self.logger.info("停止窗口区域连点: %s", self.current_script_name)
        self._stop_active_script_session(wait_timeout=2.0)
        self._toast("⏹ 窗口连点已停止", duration=2.0)
        return True

    def get_region_status(self) -> dict:
        """返回区域连点的运行状态与统计（供 GUI 展示）。"""
        executor = self.action_executor
        return {
            "active": bool(getattr(executor, "region_click_active", False)),
            "total_clicks": int(getattr(executor, "region_click_total", 0)),
            "spots_visited": int(getattr(executor, "region_click_spots", 0)),
        }

    def save_region_click_as_script(self, script_name: str,
                                    params: dict | None = None) -> bool:
        """把当前区域连点参数保存为可直接执行的动作脚本。"""
        script_name = (script_name or "").strip()
        if not script_name:
            self._toast("❌ 脚本名不能为空", duration=2.5)
            return False

        config = ConfigManager.load_region_click()
        if params:
            for key in list(config.keys()):
                if key in params and params[key] is not None:
                    config[key] = params[key]
        action = self._build_region_action(config)
        if action is None:
            self._toast("❌ 区域连点参数非法，无法保存脚本", duration=3.0)
            return False
        if action.region_width <= 0 or action.region_height <= 0:
            self._toast("❌ 区域尺寸非法，无法保存脚本", duration=3.0)
            return False

        ok = self.action_manager.save_script(script_name, [action])
        self._toast(f"✓ 已保存脚本：{script_name}" if ok else f"❌ 保存脚本失败：{script_name}",
                    duration=3.0)
        return ok

    # ---- MIDI 自动演奏 API（供 GUI 第五栏调用） ----

    def _ensure_music_player(self) -> MusicPlayer | None:
        """惰性创建演奏引擎（复用 action_executor 的键盘注入通道，不再多开一个
        Interception 上下文）。"""
        if self._music_player is not None:
            return self._music_player
        try:
            player = MusicPlayer(self.action_executor, self.logger)
            player.on_complete = self._on_music_complete
            self._music_player = player
            return player
        except Exception as e:
            self.logger.error("MusicPlayer 初始化失败: %s\n%s", e, traceback.format_exc())
            return None

    @staticmethod
    def _music_layout(config: dict) -> KeyLayout:
        """从配置里的绑定列表重建键位表。"""
        return KeyLayout.from_list(
            config.get("bindings") or [],
            name=str(config.get("layout_name") or "custom"),
        )

    def get_music_config(self) -> dict:
        """返回 MIDI 演奏参数，附带曲谱库路径与键位表概况。"""
        config = ConfigManager.load_music_player()
        layout = self._music_layout(config)
        config["music_dir"] = str(self.music_library.music_dir)
        config["layout_range"] = layout.range_text()
        config["layout_conflicts"] = layout.conflicts()
        return config

    def save_music_config(self, config: dict) -> bool:
        """保存 MIDI 演奏参数。

        先与已落盘的配置合并，避免面板未编辑的字段（如 min_hold_ms）被静默丢弃。
        """
        merged = ConfigManager.load_music_player()
        merged.update(config or {})
        ok = ConfigManager.save_music_player(merged)
        if ok:
            self._toast("✓ 演奏参数已保存", duration=2.5)
        else:
            self._toast("❌ 演奏参数保存失败", duration=3.0)
        return ok

    def reset_music_bindings(self) -> dict:
        """把键位绑定恢复成出厂的洛克手碟九键。"""
        merged = ConfigManager.load_music_player()
        merged["bindings"] = [dict(item) for item in DEFAULT_MUSIC_PLAYER["bindings"]]
        merged["layout_name"] = DEFAULT_MUSIC_PLAYER["layout_name"]
        ConfigManager.save_music_player(merged)
        self._toast("✓ 已恢复默认键位（洛克手碟九键）", duration=2.5)
        return self.get_music_config()

    def list_music_scores(self) -> list:
        """列出 data/music 下的全部曲谱。"""
        try:
            return self.music_library.list_scores()
        except Exception as e:
            self.logger.error("列出曲谱失败: %s", e)
            return []

    def import_music_score(self, path: str) -> str | None:
        """把一个 MIDI 文件复制进曲谱库，返回新曲谱名。"""
        try:
            imported = self.music_library.import_file(path)
        except Exception as e:
            self.logger.error("导入曲谱失败: %s", e)
            imported = None
        if imported is None:
            self._toast("❌ 导入失败：只能导入 .mid / .midi 文件", duration=3.0)
            return None
        self._toast(f"✓ 已导入曲谱：{imported.stem}", duration=2.5)
        return imported.stem

    def delete_music_score(self, name: str) -> bool:
        """从曲谱库删除一首曲谱。"""
        if self.is_music_playing() and self._music_name == name:
            self._toast("⚠ 正在演奏该曲谱，请先停止", duration=2.5)
            return False
        ok = self.music_library.delete(name)
        # 缓存键是解析后的绝对路径，删除时拿不到扩展名，直接整体清掉最省事：
        # 缓存本来就只存几首曲谱，重建成本可以忽略
        self._music_cache.clear()
        self._toast(f"✓ 已删除曲谱：{name}" if ok else "❌ 曲谱不存在", duration=2.5)
        return ok

    def _load_music_score(self, name: str):
        """解析曲谱，返回 (score, error)。结果按 mtime 缓存。"""
        path = self.music_library.resolve(name)
        if path is None:
            return None, f"曲谱不存在：{name}"
        try:
            stamp = path.stat().st_mtime
        except OSError:
            stamp = 0.0
        cache_key = str(path)
        cached = self._music_cache.get(cache_key)
        if cached is not None and cached[0] == stamp:
            return cached[1], None
        try:
            score = parse_midi(path)
        except MidiParseError as e:
            return None, f"MIDI 解析失败：{e}"
        except Exception as e:
            self.logger.error("解析曲谱异常 %s: %s\n%s", path, e, traceback.format_exc())
            return None, f"读取曲谱失败：{e}"
        self._music_cache[cache_key] = (stamp, score)
        return score, None

    def analyze_music_score(self, name: str, shift: int | None = None) -> dict:
        """检查曲谱能否用当前键位表完整演奏（GUI 面板的分析入口）。"""
        return self._analyze_music(name, ConfigManager.load_music_player(), shift)

    def _analyze_music(self, name: str, config: dict, shift: int | None = None) -> dict:
        layout = self._music_layout(config)
        score, error = self._load_music_score(name)
        try:
            requested = int(shift if shift is not None else config.get("semitone_shift", 0))
        except (TypeError, ValueError):
            requested = 0
        requested = max(-48, min(48, requested))

        result = {
            "name": name,
            "loaded": score is not None,
            "error": error,
            "ok": False,
            "total_notes": 0,
            "playable_notes": 0,
            "missing_notes": 0,
            "missing": [],
            "missing_text": "",
            "summary": "",
            "warnings": [],
            "duration": 0.0,
            "duration_text": "0:00",
            "bpm": 0.0,
            "track_count": 0,
            "tracks": [],
            "min_pitch": None,
            "max_pitch": None,
            "min_name": "—",
            "max_name": "—",
            "requested_shift": requested,
            "shift": requested,
            "auto_shift_used": False,
            "shift_suggestion": None,
            "peak_notes_per_second": 0,
            "same_key_overlaps": 0,
            "layout_range": layout.range_text(),
            "layout_key_count": len(layout),
            "layout_conflicts": layout.conflicts(),
        }

        if score is None:
            result["warnings"] = [error or "曲谱无法解析"]
            return result

        check = check_score(score, layout, requested)
        auto_used = False
        # 原调演奏不了时，自动挑一个「能演奏全部音符且移调量最小」的方案；
        # 找不到就保持原移调，把缺失音原样报给用户
        if not check.ok and config.get("auto_shift", True):
            suggested = best_shift(score, layout)
            if suggested is not None and suggested != requested:
                check = check_score(score, layout, suggested)
                auto_used = True

        result.update({
            "ok": check.ok,
            "total_notes": check.total_notes,
            "playable_notes": check.playable_notes,
            "missing_notes": check.missing_notes,
            "missing": [
                {
                    "pitch": item.pitch,
                    "name": item.name,
                    "count": item.count,
                    "first_time": round(item.first_time, 2),
                    "nearest_name": item.nearest_name,
                    "nearest_semitones": item.nearest_semitones,
                    "describe": item.describe(),
                }
                for item in check.missing
            ],
            "missing_text": check.describe_missing(),
            "summary": check.summary(),
            "duration": round(check.duration, 2),
            "duration_text": score.duration_text(),
            "bpm": round(check.bpm, 1),
            "track_count": len(score.tracks),
            "tracks": [{"name": t.name, "note_count": t.note_count}
                       for t in score.tracks],
            "min_pitch": check.min_pitch,
            "max_pitch": check.max_pitch,
            "min_name": note_name(check.min_pitch) if check.min_pitch is not None else "—",
            "max_name": note_name(check.max_pitch) if check.max_pitch is not None else "—",
            "shift": check.shift,
            "auto_shift_used": auto_used,
            "shift_suggestion": None if check.ok else best_shift(score, layout),
            "peak_notes_per_second": check.peak_notes_per_second,
            "same_key_overlaps": check.same_key_overlaps,
        })

        warnings: list[str] = []
        if not len(layout):
            warnings.append("键位表是空的，请先绑定按键")
        for key in layout.conflicts():
            warnings.append(f"按键 {key} 绑了多个音，实际只会发出音高最低的那个")
        if check.total_notes == 0:
            warnings.append("曲谱里没有音符")
        elif not check.ok and result["shift_suggestion"] is not None:
            warnings.append(
                f"把移调改成 {result['shift_suggestion']:+d} 半音即可完整演奏")
        if check.peak_notes_per_second > 12:
            warnings.append(
                f"最密集的一秒有 {check.peak_notes_per_second} 个音，游戏里可能来不及全部触发")
        if check.same_key_overlaps:
            warnings.append(
                f"有 {check.same_key_overlaps} 处同键音符重叠，演奏时会自动提前抬手重触发")
        if auto_used:
            warnings.append(f"原调演奏不了，已自动移调 {check.shift:+d} 半音")
        result["warnings"] = warnings
        return result

    def is_music_playing(self) -> bool:
        return bool(self._music_player and self._music_player.is_playing())

    def is_music_counting_down(self) -> bool:
        """是否处于开演前的倒计时阶段（还没真正开始按键）。"""
        return bool(self._music_name and self.countdown_end and not self.is_music_playing())

    def start_music_play(self, name: str, params: dict | None = None) -> bool:
        """开始演奏指定曲谱。

        验收要求在这里落实：只有「每个音都绑定了按键」（或移调后能全部绑定）
        才允许开演，否则直接拒绝并告诉用户是哪几个音没有绑定。
        """
        player = self._ensure_music_player()
        if player is None:
            self._toast("❌ 演奏引擎初始化失败", duration=3.0)
            return False
        if player.is_playing() or self.is_music_counting_down():
            self._toast("⚠ 已在演奏中，请先停止", duration=2.5)
            return False
        if not self.action_executor.is_keyboard_ready():
            self._toast("❌ 键盘注入不可用（Interception 未就绪）", duration=4.0)
            self._show_driver_warning()
            return False
        if self.script_running:
            self._toast("⚠ 已有脚本在运行，请先停止", duration=2.5)
            return False
        if self._playback and self._playback.is_playing():
            self._toast("⚠ 正在回放，请先停止", duration=2.5)
            return False
        if self._recording_session_active:
            self._toast("⚠ 正在录制，请先停止", duration=2.5)
            return False

        config = ConfigManager.load_music_player()
        if params:
            for key in list(config.keys()):
                if key in params and params[key] is not None:
                    config[key] = params[key]

        analysis = self._analyze_music(name, config)
        if not analysis["loaded"]:
            self._toast(f"❌ {analysis['error'] or '曲谱无法解析'}", duration=4.0)
            return False
        if not analysis["ok"]:
            self._toast(self._missing_toast_text(analysis), duration=6.0)
            return False

        score, _ = self._load_music_score(name)
        if score is None:
            self._toast("❌ 曲谱读取失败", duration=3.0)
            return False

        options = PerformanceOptions(
            hold_ratio=int(config["hold_percent"]) / 100.0,
            min_hold_ms=int(config["min_hold_ms"]),
            retrigger_gap_ms=int(config["retrigger_gap_ms"]),
            max_polyphony=int(config["max_polyphony"]),
        )
        performance = build_performance(score, self._music_layout(config),
                                        analysis["shift"], options)
        if not performance.events:
            self._toast("❌ 没有可演奏的音符", duration=3.0)
            return False

        player.speed = int(config["speed_percent"]) / 100.0
        player.set_loop_config(int(config["loop_count"]),
                               int(config["loop_delay_ms"]) / 1000.0)
        if not player.load(performance, name):
            self._toast("❌ 载入演奏序列失败", duration=3.0)
            return False

        self._music_cancel.clear()
        threading.Thread(
            target=self._run_music_session,
            args=(name, max(0, int(config["countdown_sec"]))),
            daemon=True,
        ).start()
        return True

    @staticmethod
    def _missing_toast_text(analysis: dict) -> str:
        """把「哪些音没绑定按键」压缩成一条能放进 toast 的提示。"""
        missing = analysis.get("missing") or []
        shown = "、".join(f"{item['name']}×{item['count']}" for item in missing[:4])
        if len(missing) > 4:
            shown += f" 等 {len(missing)} 个音"
        text = f"❌ 无法演奏：{shown} 没有绑定按键"
        suggestion = analysis.get("shift_suggestion")
        if suggestion is not None:
            text += f"\n改成移调 {suggestion:+d} 半音即可完整演奏"
        return text

    def _run_music_session(self, name: str, countdown: int) -> None:
        """倒计时后真正开演（放在后台线程，避免阻塞 GUI）。"""
        player = self._music_player
        if player is None:
            return
        self._music_name = name
        try:
            if countdown > 0:
                self.countdown_end = time.time() + countdown
                self.countdown_label = f"演奏 {name} 即将开始"
                self._toast(f"⏳ {countdown} 秒后开始演奏：{name}\n请切换到游戏窗口",
                            duration=countdown + 1.0)
                for _ in range(countdown):
                    if self._music_cancel.wait(1.0):
                        break
                self.countdown_end = None
                self.countdown_label = None
                if self._music_cancel.is_set():
                    self._music_name = None
                    self._toast("已取消演奏", duration=2.0)
                    return

            if player.start():
                loop = player.loop_count
                label = "，无限循环" if loop == 0 else (f"，循环 {loop} 次" if loop > 1 else "")
                meta = player.get_meta()
                self._toast(
                    f"🎹 开始演奏：{name}"
                    f"（{meta.get('note_count', 0)} 个音符 / {meta.get('key_count', 0)} 个键{label}）",
                    duration=3.0)
            else:
                self._music_name = None
                self._toast("❌ 演奏启动失败", duration=3.0)
        except Exception as e:
            self._music_name = None
            self.countdown_end = None
            self.countdown_label = None
            self.logger.error("演奏会话异常: %s\n%s", e, traceback.format_exc())
            self._toast(f"❌ 演奏异常: {e}", duration=4.0)

    def stop_music_play(self) -> bool:
        """停止演奏（倒计时阶段则是取消）。"""
        self._music_cancel.set()
        player = self._music_player
        if player is not None and player.is_playing():
            name = self._music_name or "曲谱"
            player.stop()
            self._toast(f"⏹ 演奏已停止：{name}", duration=2.0)
            return True
        if self._music_name:
            self.countdown_end = None
            self.countdown_label = None
            self._music_name = None
            self._toast("已取消演奏", duration=2.0)
            return True
        return False

    def pause_music_play(self) -> bool:
        player = self._music_player
        if player and player.pause():
            self._toast("⏸ 演奏已暂停", duration=2.0)
            return True
        return False

    def resume_music_play(self) -> bool:
        player = self._music_player
        if player and player.resume():
            self._toast("▶ 演奏已继续", duration=2.0)
            return True
        return False

    def get_music_status(self) -> dict:
        """返回演奏状态与进度（供 GUI 展示）。"""
        player = self._music_player
        playing = bool(player and player.is_playing())
        counting_down = self.is_music_counting_down()
        if not playing:
            return {
                "playing": False,
                "paused": False,
                "counting_down": counting_down,
                "name": self._music_name if counting_down else None,
                "event_index": 0,
                "event_total": 0,
                "position": 0.0,
                "duration": 0.0,
                "loop_current": 0,
                "loop_total": 0,
            }
        index, total = player.get_progress()
        position, duration = player.get_position()
        loop_current, loop_total = player.get_loop_progress()
        return {
            "playing": True,
            "paused": bool(player.is_paused()),
            "counting_down": False,
            "name": self._music_name,
            "event_index": index,
            "event_total": total,
            "position": round(position, 2),
            "duration": round(duration, 2),
            "loop_current": loop_current,
            "loop_total": loop_total,
        }

    def _on_music_complete(self, success: bool, stopped: bool):
        """演奏结束回调（在演奏线程里调用）。"""
        name = self._music_name or "曲谱"
        self._music_name = None
        try:
            if stopped:
                # 主动停止的提示由 stop_music_play 负责，这里不重复弹
                return
            if success:
                self._toast(f"✅ 演奏完成：{name}", duration=3.0)
            else:
                self._toast(f"❌ 演奏中断：{name}", duration=3.0)
        except Exception:
            pass

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

    def _on_anchor_marked(self, n: int):
        """锚点标记回调（F12 按下时由录制线程调用）：直接弹 toast 提示。"""
        try:
            self._toast(f"📌 锚点 #{n} 已标记", duration=2.5)
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
            # 演奏注入的按键会被录制器当成用户输入记下来，必须先停掉
            if self.is_music_playing() or self.is_music_counting_down():
                self.logger.info("停止 MIDI 演奏以开始录制")
                self.stop_music_play()

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

        if self.is_music_playing() or self.is_music_counting_down():
            toast("⚠ 正在演奏 MIDI，请先停止", duration=3.0)
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
        # 应用路径扰动算法配置
        try:
            pp_cfg = ConfigManager.load_path_planner()
            strategy = pp_cfg.get("strategy", "fitts")
            if strategy in ("sine", "fitts", "neuromotor", "straight"):
                self._playback.path_strategy = strategy
            self._playback.path_planner_params = pp_cfg
        except Exception:
            pass
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

    def shutdown(self):
        """彻底关闭管理器，停止所有后台线程并释放资源（供 GUI 退出时调用）。"""
        self.logger.info("开始关闭 ClickerManager...")

        # 1. 停止连点器
        if self.clicker.running:
            self.clicker.stop()

        # 2. 停止 MIDI 演奏（演奏线程会抬起所有按住的键，不能跳过）
        self._music_cancel.set()
        if self._music_player and self._music_player.is_playing():
            self._music_player.stop()

        # 3. 停止回放
        if self._playback and self._playback.is_playing():
            self._playback.stop()

        # 4. 停止脚本执行（唤醒暂停中的线程）
        self._stop_active_script_session(wait_timeout=2.0)

        # 5. 停止录制会话
        if self._recording_session_active and self._recorder:
            self._recorder.stop()
            self._recording_session_active = False

        # 6. 停止录制热键监听
        if self._recorder:
            self._recorder.stop_hotkey_listener()

        # 7. 停止全局热键监听（卸载低级别键盘钩子）
        self.hotkey_listener.stop()

        # 8. 停止热键分发线程
        self.running = False
        self.listening = False
        if self._hotkey_dispatch_thread and self._hotkey_dispatch_thread.is_alive():
            self._hotkey_dispatch_thread.join(timeout=1.0)

        self.logger.info("ClickerManager 已关闭")

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

        # 注入路径扰动参数（region_click 动作使用，与回放共用 path_planner.json）
        try:
            self.action_executor.path_planner_params = ConfigManager.load_path_planner()
        except Exception as e:
            self.logger.warning("注入路径扰动参数失败: %s", e)

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
        print(f"  {self._hotkeys.get('record_region', 'F6'):4s}  - 录制窗口区域（圈选矩形）")
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
        parser.add_argument('--install-driver', action='store_true',
                            help='只安装 Interception 驱动后退出（供 install_driver.bat 调用）')
        parser.add_argument('--uninstall-driver', action='store_true',
                            help='只卸载 Interception 驱动后退出')
        args = parser.parse_args()

        # 纯驱动维护模式：不初始化 Interception、不开 GUI，全部交给 DriverInstaller。
        # install_driver.bat 会调到这里 —— 那个 .bat 必须是纯 ASCII（cmd 解析含多字节
        # 字符的行会错位，把行尾当命令执行），所以面向用户的中文提示一律由
        # DriverInstaller 的原生消息框给出，而不是靠 .bat 的 echo。
        if args.install_driver or args.uninstall_driver:
            setup_logger()
            sys.exit(DriverInstaller.main(
                ["--uninstall"] if args.uninstall_driver else ["--install"]
            ))

        # 如果是打包后的可执行文件（frozen），默认打开 GUI 窗口以匹配发布版行为
        if getattr(sys, 'frozen', False) and not args.gui:
            args.gui = True

        # 先尝试初始化一次，以检查驱动/DLL 是否就绪
        setup_logger()
        probe = InterceptionCore()
        if not probe.is_ready():
            logging.error("Interception 初始化失败：%s", probe.init_error)
            # 驱动没装时直接弹「是否现在自动安装」，不再要求用户自己开管理员终端敲命令
            _report_driver_not_ready(probe, interactive=True)
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
            f"程序启动时发生错误：\n{e}\n\n"
            "若提示与驱动相关，请双击程序目录里的 install_driver.bat 一键安装，\n"
            "安装完成后重启电脑再试。",
            "启动失败 - RocoKingdom Clicker",
            error=True,
        )
        sys.exit(1)


if __name__ == "__main__":
    main()
