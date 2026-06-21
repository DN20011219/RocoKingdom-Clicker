"""
连点器核心模块 - 基于 Interception 驱动实现
使用 ctypes 调用 Interception 库来注入鼠标事件（内核级）。
"""

from __future__ import annotations

import ctypes
import logging
import math
import random
import threading
import time
from typing import Callable, Optional


user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32
try:
    user32.SetProcessDPIAware()
except Exception:
    pass

SW = user32.GetSystemMetrics(0)
SH = user32.GetSystemMetrics(1)


class InterceptionMouseStroke(ctypes.Structure):
    """Interception 鼠标 stroke 结构体"""
    _fields_ = [
        ("state", ctypes.c_ushort),
        ("flags", ctypes.c_ushort),
        ("rolling", ctypes.c_short),
        ("x", ctypes.c_int),
        ("y", ctypes.c_int),
        ("information", ctypes.c_uint),
    ]


class InterceptionKeyStroke(ctypes.Structure):
    """Interception 键盘 stroke 结构体"""
    _fields_ = [
        ("code", ctypes.c_ushort),
        ("state", ctypes.c_ushort),
        ("information", ctypes.c_uint),
    ]


class ClickerConfig:
    """连点器配置类。"""

    def __init__(self):
        self.enabled = False
        self.center_x = SW // 2
        self.center_y = SH // 2
        self.radius = 30
        # 是否在每次点击前移动鼠标到目标坐标；
        # 若设为 False，则只发送按下/释放事件，使用当前鼠标位置进行点击
        self.move_mouse = False
        # 默认：按住约800ms(每次会额外随机±200~400ms)，然后等待随机300~700ms再按下一次
        self.click_interval = 500
        self.hold_duration = 800
        self.jitter_range = 200
        


class InterceptionCore:
    """
    连点器核心类 - 基于 Interception 驱动。
    
    功能：
    - 在后台线程中循环发送点击事件
    - 支持圆形随机偏移
    - 支持持续时间和间隔的随机化
    - 可选的焦点回调函数（在每次点击前调用）
    """

    def __init__(self, config: ClickerConfig | None = None):
        self.config = config or ClickerConfig()
        self.running = False
        self._pause_event = threading.Event()
        self._pause_event.set()
        self.thread: Optional[threading.Thread] = None
        self.click_count = 0
        self.start_time: Optional[float] = None
        self.focus_callback: Optional[Callable[[], bool]] = None
        self._click_anchor_x = self.config.center_x
        self._click_anchor_y = self.config.center_y
        self._next_micro_pause_at = random.randint(6, 13)
        self.logger = logging.getLogger("interception_clicker")

        # 加载 Interception 库（失败时不抛异常，只记录错误，由上层决定如何提示用户）
        self._lib = None
        self._ctx = None
        self._device = None
        self.init_error: Optional[str] = None
        self._initialize_interception()
        if self.init_error:
            self.logger.warning("Interception 初始化失败: %s", self.init_error)
        else:
            self.logger.info("InterceptionCore 初始化完成")

    def is_ready(self) -> bool:
        """返回 Interception 驱动/DLL 是否已可用。"""
        return self._lib is not None and self._ctx is not None

    def _wait_until_resumed(self, poll_interval: float = 0.1) -> bool:
        """等待恢复运行，返回 False 表示已停止。"""
        while self.running and not self._pause_event.is_set():
            time.sleep(poll_interval)
        return self.running

    def _sleep_interruptible(self, duration: float) -> bool:
        """支持暂停/停止的分段睡眠。"""
        deadline = time.time() + duration
        while self.running:
            if not self._pause_event.is_set():
                if not self._wait_until_resumed():
                    return False
                continue

            remaining = deadline - time.time()
            if remaining <= 0:
                return True
            time.sleep(min(0.05, remaining))

        return False

    def _initialize_interception(self):
        """初始化 Interception 库和上下文（失败时写入 self.init_error 而不是抛异常）。"""
        try:
            import os
            import platform

            # 根据当前进程架构决定使用 x64 还是 x86 版本的 DLL
            arch = "x64" if platform.architecture()[0] == "64bit" else "x86"
            project_root = os.path.dirname(os.path.abspath(__file__))

            # 候选路径：项目根目录（便于本地直接运行）→ 第三方目录（库随仓库自带）
            # → 当前工作目录
            dll_paths = [
                os.path.join(project_root, "interception.dll"),
                os.path.join(project_root, "third", "Interception", "library", arch, "interception.dll"),
                os.path.join(project_root, "third", "Interception", "library", "x64", "interception.dll"),
                os.path.join(project_root, "third", "Interception", "library", "x86", "interception.dll"),
                "interception.dll",
            ]

            lib = None
            used_path = None
            for path in dll_paths:
                if not path or not os.path.isfile(path):
                    continue
                try:
                    lib = ctypes.CDLL(path)
                    used_path = path
                    break
                except Exception:
                    continue

            if lib is None:
                hint = os.path.join(project_root, "third", "Interception", "library", arch, "interception.dll")
                self.init_error = (
                    "找不到或无法加载 interception.dll\n"
                    "可能原因：\n"
                    "  1) Interception 驱动尚未安装\n"
                    "  2) DLL 文件缺失或路径不正确\n\n"
                    f"尝试的架构：{arch}\n最后一次尝试路径：{hint}\n\n"
                    "请先以管理员身份运行 driver_installer\\install-interception.exe /install 安装驱动，然后重启电脑。"
                )
                return

            self._lib = lib

            # --- 谓词函数类型（供 interception_set_filter 使用） ---
            # signature: int (*InterceptionPredicate)(InterceptionDevice device)
            InterceptionPredicate = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_int)

            # --- 绑定所有函数的 argtypes / restype（匹配 interception.h） ---
            lib.interception_create_context.restype = ctypes.c_void_p

            lib.interception_destroy_context.argtypes = [ctypes.c_void_p]
            lib.interception_destroy_context.restype = None

            lib.interception_set_filter.argtypes = [
                ctypes.c_void_p,   # context
                InterceptionPredicate,  # predicate (function pointer)
                ctypes.c_ushort,   # filter
            ]
            lib.interception_set_filter.restype = None

            lib.interception_get_filter.argtypes = [ctypes.c_void_p, ctypes.c_int]
            lib.interception_get_filter.restype = ctypes.c_ushort

            lib.interception_wait.argtypes = [ctypes.c_void_p]
            lib.interception_wait.restype = ctypes.c_int

            lib.interception_wait_with_timeout.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
            lib.interception_wait_with_timeout.restype = ctypes.c_int

            # send / receive：InterceptionStroke 就是 sizeof(InterceptionMouseStroke) 的 char 缓冲
            lib.interception_send.argtypes = [
                ctypes.c_void_p, ctypes.c_int,
                ctypes.POINTER(InterceptionMouseStroke), ctypes.c_uint,
            ]
            lib.interception_send.restype = ctypes.c_int

            lib.interception_receive.argtypes = [
                ctypes.c_void_p, ctypes.c_int,
                ctypes.POINTER(InterceptionMouseStroke), ctypes.c_uint,
            ]
            lib.interception_receive.restype = ctypes.c_int

            lib.interception_is_keyboard.argtypes = [ctypes.c_int]
            lib.interception_is_keyboard.restype = ctypes.c_int

            lib.interception_is_mouse.argtypes = [ctypes.c_int]
            lib.interception_is_mouse.restype = ctypes.c_int

            lib.interception_is_invalid.argtypes = [ctypes.c_int]
            lib.interception_is_invalid.restype = ctypes.c_int

            # --- Interception 过滤器常量（来自 interception.h 的 enum 值） ---
            # 这些是 enum 整数，不是 DLL 导出符号
            lib.INTERCEPTION_FILTER_KEY_ALL = 0xFFFF
            lib.INTERCEPTION_FILTER_KEY_DOWN = 0x01
            lib.INTERCEPTION_FILTER_KEY_UP = 0x02
            lib.INTERCEPTION_FILTER_MOUSE_ALL = 0xFFFF
            lib.INTERCEPTION_FILTER_MOUSE_LEFT_DOWN = 0x001
            lib.INTERCEPTION_FILTER_MOUSE_LEFT_UP = 0x002
            lib.INTERCEPTION_FILTER_MOUSE_RIGHT_DOWN = 0x004
            lib.INTERCEPTION_FILTER_MOUSE_RIGHT_UP = 0x008
            lib.INTERCEPTION_FILTER_MOUSE_MIDDLE_DOWN = 0x010
            lib.INTERCEPTION_FILTER_MOUSE_MIDDLE_UP = 0x020
            lib.INTERCEPTION_FILTER_MOUSE_MOVE = 0x1000

            # --- 把谓词包装函数也挂到 lib 上，方便调用方使用 ---
            lib._interception_predicate_type = InterceptionPredicate
            try:
                lib._is_keyboard_pred = InterceptionPredicate(lib.interception_is_keyboard)
                lib._is_mouse_pred = InterceptionPredicate(lib.interception_is_mouse)
            except Exception:
                pass

            # 创建上下文（驱动未安装/未重启时，这里通常返回 NULL 或直接失败）
            try:
                self._ctx = self._lib.interception_create_context()
            except Exception as inner:
                self._ctx = None
                self.init_error = (
                    "Interception 驱动未就绪：interception_create_context 调用失败。\n\n"
                    f"错误信息：{inner}\n\n"
                    "请先以管理员身份运行 driver_installer\\install-interception.exe /install 安装驱动，然后重启电脑。"
                )
                return

            if not self._ctx:
                self.init_error = (
                    "Interception 驱动未就绪：无法创建上下文（返回 NULL）。\n\n"
                    "可能是驱动已安装但未重启电脑，或者驱动未正确安装。\n"
                    "请运行 driver_installer\\install-interception.exe /install 重新安装驱动，然后重启电脑。"
                )
                return

            # 获取虚拟鼠标设备（INTERCEPTION_MAX_KEYBOARD=10, INTERCEPTION_MOUSE(0) = 11）
            self._device = 11
            self.logger.info(f"成功加载 Interception 库 ({arch}): {used_path}")
            self.logger.info("Interception 上下文已创建，设备 ID: %d", self._device)

        except Exception as e:
            self.logger.error("Interception 初始化异常: %s", e)
            self._lib = None
            self._ctx = None
            self._device = None
            self.init_error = (
                "Interception 初始化异常：\n"
                f"{e}\n\n"
                "请先以管理员身份运行 driver_installer\\install-interception.exe /install 安装驱动，然后重启电脑。"
            )

    def _send_mouse_stroke(self, stroke: InterceptionMouseStroke) -> bool:
        """发送单个鼠标 stroke"""
        if not self._lib or not self._ctx:
            return False
        
        try:
            arr = (InterceptionMouseStroke * 1)(stroke)
            result = self._lib.interception_send(self._ctx, self._device, arr, 1)
            return result > 0
        except Exception as e:
            self.logger.error("发送鼠标 stroke 失败: %s", e)
            return False

    def _screen_to_interception(self, x: int, y: int) -> tuple[int, int]:
        """把屏幕像素坐标转换为 Interception 的绝对坐标。"""
        max_x = max(SW - 1, 1)
        max_y = max(SH - 1, 1)
        ix = int(max(0, min(x, max_x)) * 65535 / max_x)
        iy = int(max(0, min(y, max_y)) * 65535 / max_y)
        return ix, iy

    def _move_mouse_to(self, x: int, y: int) -> bool:
        """移动鼠标到指定位置（绝对坐标）"""
        abs_x, abs_y = self._screen_to_interception(int(round(x)), int(round(y)))
        stroke = InterceptionMouseStroke()
        stroke.state = 0
        stroke.flags = 0x001  # INTERCEPTION_MOUSE_MOVE_ABSOLUTE
        stroke.rolling = 0
        stroke.x = abs_x
        stroke.y = abs_y
        stroke.information = 0
        return self._send_mouse_stroke(stroke)

    def _mouse_down(self) -> bool:
        """按下左键"""
        stroke = InterceptionMouseStroke()
        stroke.state = 0x001  # INTERCEPTION_MOUSE_LEFT_BUTTON_DOWN
        stroke.flags = 0
        stroke.rolling = 0
        stroke.x = 0
        stroke.y = 0
        stroke.information = 0
        return self._send_mouse_stroke(stroke)

    def _mouse_up(self) -> bool:
        """释放左键"""
        stroke = InterceptionMouseStroke()
        stroke.state = 0x002  # INTERCEPTION_MOUSE_LEFT_BUTTON_UP
        stroke.flags = 0
        stroke.rolling = 0
        stroke.x = 0
        stroke.y = 0
        stroke.information = 0
        return self._send_mouse_stroke(stroke)

    def _random_target_around(self, center_x: int, center_y: int) -> tuple[int, int]:
        """围绕固定中心生成一次随机目标点。"""
        angle = random.uniform(0, 2 * math.pi)
        dist = random.uniform(0, self.config.radius)
        offset_x = dist * math.cos(angle)
        offset_y = dist * math.sin(angle)
        return int(round(center_x + offset_x)), int(round(center_y + offset_y))

    def _perform_click(self, center_x: Optional[int] = None, center_y: Optional[int] = None) -> bool:
        """执行一次完整的点击"""
        if center_x is None:
            center_x = self._click_anchor_x
        if center_y is None:
            center_y = self._click_anchor_y

        target_x, target_y = self._random_target_around(center_x, center_y)
        # 根据配置决定是否移动鼠标到目标坐标
        if getattr(self.config, 'move_mouse', True):
            self._move_mouse_to(target_x, target_y)
            if not self._sleep_interruptible(0.02):
                return False
        
        # 按下左键
        self._mouse_down()
        
        # 随机持续时间
        hold_time = (self.config.hold_duration + 
                    random.uniform(-self.config.jitter_range, self.config.jitter_range)) / 1000.0
        if not self._sleep_interruptible(max(0.05, hold_time)):
            return False
        
        # 释放左键
        self._mouse_up()
        
        self.click_count += 1
        return True

    def _clicking_loop(self):
        """后台点击循环"""
        self.logger.info("点击循环已启动")
        base_center_x = self._click_anchor_x
        base_center_y = self._click_anchor_y
        
        try:
            while self.running:
                if not self._pause_event.is_set():
                    if not self._wait_until_resumed():
                        break

                # 焦点回调（保持目标窗口在前台）
                if self.focus_callback:
                    try:
                        if not self.focus_callback():
                            if not self._sleep_interruptible(0.1):
                                break
                            continue
                    except Exception as e:
                        self.logger.warning("焦点回调失败: %s", e)
                
                # 执行点击
                if not self._perform_click(base_center_x, base_center_y):
                    break
                
                # 微暂停（模拟人工 - 每 7~13 次随机）
                if self.click_count % self._next_micro_pause_at == 0:
                    if not self._sleep_interruptible(random.uniform(0.15, 0.35)):
                        break
                    self._next_micro_pause_at = random.randint(6, 13)
                
                # 点击间隔
                interval = self.config.click_interval / 1000.0
                if not self._sleep_interruptible(interval):
                    break
        
        except Exception as e:
            self.logger.error("点击循环异常: %s", e)
        finally:
            self.logger.info("点击循环已结束")

    def start(self):
        """启动连点器。"""
        if self.running:
            self.logger.warning("连点器已在运行中")
            return

        if not self._lib or not self._ctx:
            self.logger.error("Interception 库未初始化")
            return

        self.running = True
        self._pause_event.set()
        self.click_count = 0
        self.start_time = time.time()
        self._click_anchor_x = self.config.center_x
        self._click_anchor_y = self.config.center_y
        self._next_micro_pause_at = random.randint(6, 13)
        
        self.thread = threading.Thread(target=self._clicking_loop, daemon=True)
        self.thread.start()
        self.logger.info("连点器已启动")

    def stop(self):
        """停止连点器。"""
        if not self.running:
            self.logger.warning("连点器未在运行")
            return

        self.running = False
        self._pause_event.set()
        if self.thread:
            self.thread.join(timeout=1.0)
        self.logger.info("连点器已停止 | 总点击数: %d", self.click_count)

    def pause(self):
        """暂停连点器。"""
        if not self.running:
            self.logger.warning("连点器未在运行，无法暂停")
            return False

        if not self._pause_event.is_set():
            return True

        self._pause_event.clear()
        self.logger.info("连点器已暂停")
        return True

    def resume(self):
        """恢复连点器。"""
        if not self.running:
            self.logger.warning("连点器未在运行，无法恢复")
            return False

        self._pause_event.set()
        self.logger.info("连点器已恢复")
        return True

    def is_paused(self) -> bool:
        """返回是否处于暂停状态。"""
        return self.running and not self._pause_event.is_set()

    def set_focus_callback(self, callback: Optional[Callable[[], bool]]) -> None:
        """设置焦点回调函数（在点击前调用）"""
        self.focus_callback = callback

    def get_stats(self) -> dict:
        """获取统计信息"""
        stats = {
            "running": self.running,
            "paused": self.is_paused(),
            "click_count": self.click_count,
            "center_x": self.config.center_x,
            "center_y": self.config.center_y,
            "radius": self.config.radius,
            "move_mouse": getattr(self.config, 'move_mouse', True),
            "click_interval": self.config.click_interval,
            "hold_duration": self.config.hold_duration,
            "jitter_range": self.config.jitter_range,
        }
        
        if self.running and self.start_time:
            elapsed = time.time() - self.start_time
            stats["duration"] = elapsed
            stats["clicks_per_second"] = self.click_count / elapsed if elapsed > 0 else 0
        
        return stats

    def __del__(self):
        """清理资源"""
        if self._lib and self._ctx:
            try:
                self._lib.interception_destroy_context(self._ctx)
                self.logger.info("Interception 上下文已销毁")
            except Exception as e:
                self.logger.error("销毁 Interception 上下文失败: %s", e)
