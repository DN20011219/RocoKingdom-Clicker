"""
动作脚本模块 - 支持自定义动作序列，基于 Interception 驱动
支持动作：Click（点击）、Move（移动）、Key（按键）、Wait（等待）、RegionClick（窗口区域连点）
"""

from __future__ import annotations

import ctypes
import json
import logging
import math
import random
import threading
import time
from dataclasses import dataclass, asdict, field
from enum import Enum
from pathlib import Path
from typing import Any, List, Optional
from ctypes import wintypes

from InterceptionCore import find_keyboard_device, find_mouse_device
from PathPlanner import PathPlanner
from ConfigManager import ConfigManager


user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

MAX_PATH = 260
SW_RESTORE = 9
SW_SHOW = 5
INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002
INTERCEPTION_MAX_DEVICE = 20
INTERCEPTION_KEY_DOWN = 0x00
INTERCEPTION_KEY_UP = 0x01
INTERCEPTION_KEY_E0 = 0x02

VK_TO_SCANCODE = {
    0x08: (0x0E, False),
    0x09: (0x0F, False),
    0x0D: (0x1C, False),
    0x10: (0x2A, False),
    0x11: (0x1D, False),
    0x12: (0x38, False),
    0x13: (0x45, False),
    0x14: (0x3A, False),
    0x1B: (0x01, False),
    0x20: (0x39, False),
    0x21: (0x49, True),
    0x22: (0x51, True),
    0x23: (0x4F, True),
    0x24: (0x47, True),
    0x25: (0x4B, True),
    0x26: (0x48, True),
    0x27: (0x4D, True),
    0x28: (0x50, True),
    0x2D: (0x53, True),
    0x2E: (0x53, True),
    0x30: (0x0B, False),
    0x31: (0x02, False),
    0x32: (0x03, False),
    0x33: (0x04, False),
    0x34: (0x05, False),
    0x35: (0x06, False),
    0x36: (0x07, False),
    0x37: (0x08, False),
    0x38: (0x09, False),
    0x39: (0x0A, False),
    0x41: (0x1E, False),
    0x42: (0x30, False),
    0x43: (0x2E, False),
    0x44: (0x20, False),
    0x45: (0x12, False),
    0x46: (0x21, False),
    0x47: (0x22, False),
    0x48: (0x23, False),
    0x49: (0x17, False),
    0x4A: (0x24, False),
    0x4B: (0x25, False),
    0x4C: (0x26, False),
    0x4D: (0x32, False),
    0x4E: (0x31, False),
    0x4F: (0x18, False),
    0x50: (0x19, False),
    0x51: (0x10, False),
    0x52: (0x13, False),
    0x53: (0x1F, False),
    0x54: (0x14, False),
    0x55: (0x16, False),
    0x56: (0x2F, False),
    0x57: (0x11, False),
    0x58: (0x2D, False),
    0x59: (0x15, False),
    0x5A: (0x2C, False),
    # 主键盘区标点（US 布局，AT Set 1 扫描码）。MIDI 自动演奏面板允许把音符绑到
    # 任意可注入的按键上，表里没有的键只能退回 keybd_event，游戏里更容易丢键。
    0xBA: (0x27, False),
    0xBB: (0x0D, False),
    0xBC: (0x33, False),
    0xBD: (0x0C, False),
    0xBE: (0x34, False),
    0xBF: (0x35, False),
    0xC0: (0x29, False),
    0xDB: (0x1A, False),
    0xDC: (0x2B, False),
    0xDD: (0x1B, False),
    0xDE: (0x28, False),
    0x60: (0x52, False),
    0x61: (0x4F, False),
    0x62: (0x50, False),
    0x63: (0x51, False),
    0x64: (0x4B, False),
    0x65: (0x4C, False),
    0x66: (0x4D, False),
    0x67: (0x47, False),
    0x68: (0x48, False),
    0x69: (0x49, False),
    0x6A: (0x37, False),
    0x6B: (0x4E, False),
    0x6D: (0x4A, False),
    0x6E: (0x53, False),
    0x6F: (0x35, True),
    0x70: (0x3B, False),
    0x71: (0x3C, False),
    0x72: (0x3D, False),
    0x73: (0x3E, False),
    0x74: (0x3F, False),
    0x75: (0x40, False),
    0x76: (0x41, False),
    0x77: (0x42, False),
    0x78: (0x43, False),
    0x79: (0x44, False),
    0x7A: (0x57, False),
    0x7B: (0x58, False),
    0xA0: (0x2A, False),
    0xA1: (0x36, False),
    0xA2: (0x1D, False),
    0xA3: (0x1D, True),
    0xA4: (0x38, False),
    0xA5: (0x38, True),
}


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.c_size_t),
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", wintypes.WCHAR * MAX_PATH),
    ]


class POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


# Interception 结构体定义
class InterceptionMouseStroke(ctypes.Structure):
    _fields_ = [
        ("state", ctypes.c_ushort),
        ("flags", ctypes.c_ushort),
        ("rolling", ctypes.c_short),
        ("x", ctypes.c_int),
        ("y", ctypes.c_int),
        ("information", ctypes.c_uint),
    ]


class InterceptionKeyStroke(ctypes.Structure):
    _fields_ = [
        ("code", ctypes.c_ushort),
        ("state", ctypes.c_ushort),
        ("information", ctypes.c_uint),
    ]


class ActionType(Enum):
    """动作类型"""
    CLICK = "click"
    MOVE = "move"
    KEY = "key"
    COMBO = "combo"
    WAIT = "wait"
    REGION_CLICK = "region_click"


@dataclass
class ClickAction:
    """点击动作"""
    x: int
    y: int
    hold_ms: int = 100  # 默认按住 100ms
    x_jitter_px: int = 0
    y_jitter_px: int = 0
    hold_jitter_ms: int = 0

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class MoveAction:
    """移动动作"""
    x: int
    y: int
    duration_ms: int = 100  # 移动耗时
    x_jitter_px: int = 0
    y_jitter_px: int = 0
    duration_jitter_ms: int = 0

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class KeyAction:
    """按键动作"""
    vk_code: int
    hold_ms: int = 50
    hold_jitter_ms: int = 0

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class WaitAction:
    """等待动作"""
    duration_ms: int
    duration_jitter_ms: int = 0


@dataclass
class ComboAction:
    """组合按键动作"""
    vk_codes: List[int]
    hold_ms: int = 50
    hold_jitter_ms: int = 0

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class LoopAction:
    """循环动作"""
    actions: List[Any] = field(default_factory=list)
    count: int = 1
    forever: bool = False
    pause_ms: int = 0
    pause_jitter_ms: int = 0

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class TimedAction:
    """定时包裹动作：在执行窗口内重复运行内部 actions，然后休眠。"""
    actions: List[Any] = field(default_factory=list)
    execute_ms: int = 0
    sleep_ms: int = 0
    repeat: int = 1
    forever: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class RegionClickAction:
    """窗口区域连点：在矩形区域内随机取点连点，点间用拟人轨迹移动。

    全程使用相对位移注入（与 PlaybackEngine 一致），不使用绝对坐标，
    因此天然支持多显示器与光标锁定场景，也不受全局 move_mouse 开关影响。
    """
    region_x: int = 0
    region_y: int = 0
    region_width: int = 0
    region_height: int = 0
    clicks: int = 0                 # 0 = 不限次数
    forever: bool = True
    clicks_per_spot: int = 3        # 每个点点击多少次后才移动
    clicks_per_spot_jitter: int = 1
    interval_ms: int = 120          # 同一点两次点击的间隔
    interval_jitter_ms: int = 40
    hold_ms: int = 80
    hold_jitter_ms: int = 30
    move_duration_ms: int = 220     # 两点之间的移动耗时
    move_duration_jitter_ms: int = 60
    margin_px: int = 8              # 区域内边距，避免贴边点击
    min_spot_distance_px: int = 40  # 相邻两个点的最小距离
    x_jitter_px: int = 3            # 单次点击的落点抖动
    y_jitter_px: int = 3
    spot_pause_ms: int = 0          # 换点后的额外停顿
    spot_pause_jitter_ms: int = 0
    path_strategy: str = "global"   # global / sine / fitts / neuromotor / straight
    path_steps: int = 0             # 0 = 按移动耗时自动推算
    button: str = "left"            # left / right / middle
    correct_drift_px: int = 4       # 0 = 关闭漂移校正
    path_params: dict = field(default_factory=dict)  # 可覆盖全局扰动参数

    def to_dict(self) -> dict:
        return asdict(self)


# 动作数据类 → JSON type 字段映射（保存脚本时必需，否则无法被 _parse_action 回读）
_ACTION_TYPE_MAP = {
    ClickAction: "click",
    MoveAction: "move",
    KeyAction: "key",
    ComboAction: "combo",
    WaitAction: "wait",
    LoopAction: "loop",
    TimedAction: "timed",
    RegionClickAction: "region_click",
}


def _action_to_dict(action: Any) -> dict:
    """把动作对象序列化为带 type 字段的 dict（loop/timed 的嵌套动作递归处理）。"""
    data = asdict(action)
    data["type"] = _ACTION_TYPE_MAP.get(type(action), "unknown")
    nested = getattr(action, "actions", None)
    if isinstance(nested, list):
        # asdict 会丢掉嵌套动作的 type，这里用原对象重建
        data["actions"] = [_action_to_dict(a) for a in nested]
    return data


class ActionExecutor:
    """动作执行器 - 基于 Interception 驱动"""

    # 鼠标按键的 down/up state 值（Interception bitmask）
    _BUTTON_STATES = {
        "left": (0x001, 0x002),
        "right": (0x004, 0x008),
        "middle": (0x010, 0x020),
    }

    # PathPlanner 接受的参数白名单（与 PlaybackEngine._planner_kwargs 保持一致）
    _PLANNER_PARAM_KEYS = frozenset({
        "sine_amplitude_px", "sine_frequency",
        "fitts_jitter_px", "fitts_arc_px",
        "fitts_overshoot_chance", "fitts_overshoot_px",
        "nm_lognormal_sigma", "nm_perception_noise", "nm_entropy_alpha",
        "nm_lateral_drift", "nm_max_corrections",
    })

    def __init__(self):
        self.logger = logging.getLogger("action_executor")
        
        # 加载 Interception 库
        self._lib = None
        self._ctx = None
        self._device = None
        # 引用外部 clicker（由 ClickerManager 注入），用于读取运行时配置
        self.clicker = None
        # 记录被忽略的移动操作次数（当全局 move_mouse 被禁用时）
        self.ignored_moves = 0
        self._ignored_lock = threading.Lock()
        # 路径扰动参数（由 ClickerManager 从 path_planner.json 注入）
        self.path_planner_params: dict = {}
        # 区域连点运行时统计（供 GUI 展示）
        self.region_click_active = False
        self.region_click_total = 0
        self.region_click_spots = 0
        self._initialize_interception()
    
    def _initialize_interception(self):
        """初始化 Interception"""
        try:
            import os
            import platform
            # 与 InterceptionCore 保持一致的多路径搜索：只找 __file__ 同目录和 CWD
            # 两条路径时，源码模式下（仓库根目录没有 interception.dll）会全部落空，
            # 导致 self._lib=None、动作脚本的鼠标和键盘静默失效。补上 third\ 回退，
            # 并用 isfile 预筛，避免对不存在的相对路径盲目 CDLL。
            arch = "x64" if platform.architecture()[0] == "64bit" else "x86"
            project_root = os.path.dirname(os.path.abspath(__file__))
            dll_paths = [
                os.path.join(project_root, "interception.dll"),
                os.path.join(project_root, "third", "Interception", "library", arch, "interception.dll"),
                os.path.join(project_root, "third", "Interception", "library", "x64", "interception.dll"),
                os.path.join(project_root, "third", "Interception", "library", "x86", "interception.dll"),
                "interception.dll",
            ]
            
            lib = None
            for path in dll_paths:
                if not path or not os.path.isfile(path):
                    continue
                try:
                    lib = ctypes.CDLL(path)
                    self.logger.info(f"加载 Interception: {path}")
                    break
                except Exception:
                    continue
            
            if lib is None:
                raise OSError("无法加载 interception.dll")
            
            self._lib = lib
            self._lib.interception_create_context.restype = ctypes.c_void_p
            self._lib.interception_destroy_context.argtypes = [ctypes.c_void_p]
            self._lib.interception_send.argtypes = [
                ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint
            ]
            self._lib.interception_send.restype = ctypes.c_int
            self._lib.interception_is_keyboard.argtypes = [ctypes.c_int]
            self._lib.interception_is_keyboard.restype = ctypes.c_int
            self._lib.interception_is_mouse.argtypes = [ctypes.c_int]
            self._lib.interception_is_mouse.restype = ctypes.c_int
            
            self._ctx = self._lib.interception_create_context()
            if not self._ctx:
                raise OSError("无法创建 Interception 上下文")
            
            # 枚举真实挂载了硬件的鼠标槽位；不能硬编码 11，空槽位 send 会静默失败。
            # 这里不抛异常：没有鼠标时键盘注入仍然可用。
            self._device = find_mouse_device(self._lib, self._ctx)
            if self._device is None:
                self.logger.warning("未发现鼠标设备，脚本中的鼠标动作将无效")
            self._keyboard_device = None
            self.logger.info("ActionExecutor Interception 已初始化")
        
        except Exception as e:
            self.logger.warning("Interception 初始化失败，将回退到用户模式: %s", e)
            self._lib = None
    
    def _send_mouse_stroke(self, stroke: InterceptionMouseStroke) -> bool:
        """发送鼠标 stroke"""
        if not self._lib or not self._ctx or self._device is None:
            return False
        try:
            arr = (InterceptionMouseStroke * 1)(stroke)
            result = self._lib.interception_send(self._ctx, self._device, ctypes.cast(arr, ctypes.c_void_p), 1)
            return result > 0
        except Exception as e:
            self.logger.error("发送鼠标 stroke 失败: %s", e)
            return False

    def _send_key_stroke(self, device: int, stroke: InterceptionKeyStroke) -> bool:
        """发送键盘 stroke。"""
        if not self._lib or not self._ctx:
            return False
        try:
            arr = (InterceptionKeyStroke * 1)(stroke)
            result = self._lib.interception_send(self._ctx, device, ctypes.cast(arr, ctypes.c_void_p), 1)
            return result > 0
        except Exception as e:
            self.logger.error("发送键盘 stroke 失败: %s", e)
            return False

    def _ensure_keyboard_device(self) -> Optional[int]:
        """枚举一个真正挂载了硬件的键盘设备。"""
        if self._keyboard_device is not None:
            return self._keyboard_device
        if not self._lib or not self._ctx:
            return None
        device = find_keyboard_device(self._lib, self._ctx)
        if device is not None:
            self._keyboard_device = device
            self.logger.info("已发现键盘设备: %d", device)
            return device
        self.logger.warning("未发现键盘设备，键盘注入将失败")
        return None

    def _send_key_event(self, scancode: int, state: int, extended: bool = False) -> bool:
        """发送单个键盘事件。"""
        device = self._ensure_keyboard_device()
        if device is None:
            return False
        stroke = InterceptionKeyStroke()
        stroke.code = scancode
        stroke.state = state | (INTERCEPTION_KEY_E0 if extended else 0)
        stroke.information = 0
        return self._send_key_stroke(device, stroke)

    def _vk_to_scancode(self, vk_code: int) -> tuple[Optional[int], bool]:
        return VK_TO_SCANCODE.get(vk_code, (None, False))

    def is_keyboard_ready(self) -> bool:
        """键盘注入是否可用（找得到真正挂了硬件的键盘设备）。"""
        return self._ensure_keyboard_device() is not None

    def key_down(self, vk_code: int) -> bool:
        """按下一个键并保持按住，返回是否成功。

        与 _execute_key 的区别在于按下和抬起被拆成两次调用：MIDI 自动演奏需要
        精确控制每个音的时值（按住多久由谱子决定），不能在一次调用里 sleep 完。
        """
        scancode, extended = self._vk_to_scancode(vk_code)
        if scancode is None:
            self.logger.warning("未知 VK，无法注入按键: 0x%02x", vk_code)
            return False
        if self._send_key_event(scancode, INTERCEPTION_KEY_DOWN, extended):
            return True
        try:
            user32.keybd_event(vk_code, 0, 0, 0)
            self.logger.warning("按键按下回退 keybd_event VK=0x%02x", vk_code)
            return True
        except Exception as e:
            self.logger.error("按键按下失败 VK=0x%02x: %s", vk_code, e)
            return False

    def key_up(self, vk_code: int) -> bool:
        """抬起一个按住的键，返回是否成功。"""
        scancode, extended = self._vk_to_scancode(vk_code)
        if scancode is None:
            self.logger.warning("未知 VK，无法注入按键: 0x%02x", vk_code)
            return False
        if self._send_key_event(scancode, INTERCEPTION_KEY_UP, extended):
            return True
        try:
            user32.keybd_event(vk_code, 0, KEYEVENTF_KEYUP, 0)
            self.logger.warning("按键抬起回退 keybd_event VK=0x%02x", vk_code)
            return True
        except Exception as e:
            self.logger.error("按键抬起失败 VK=0x%02x: %s", vk_code, e)
            return False

    def _apply_jitter(self, value: int, jitter: int, minimum: int = 0) -> int:
        """给数值添加随机扰动并限制下限。"""
        if jitter <= 0:
            return max(minimum, int(value))
        return max(minimum, int(round(value + random.uniform(-jitter, jitter))))

    def _screen_to_interception(self, x: int, y: int) -> tuple[int, int]:
        """把屏幕像素坐标转换为 Interception 绝对坐标。"""
        width = max(user32.GetSystemMetrics(0) - 1, 1)
        height = max(user32.GetSystemMetrics(1) - 1, 1)
        ix = int(max(0, min(x, width)) * 65535 / width)
        iy = int(max(0, min(y, height)) * 65535 / height)
        return ix, iy
    
    def _execute_click(self, action: ClickAction) -> None:
        """执行点击"""
        # 根据全局配置决定是否移动鼠标
        move_allowed = True
        try:
            if getattr(self, 'clicker', None) is not None:
                move_allowed = bool(getattr(self.clicker.config, 'move_mouse', True))
        except Exception:
            move_allowed = True

        if move_allowed:
            # 移动
            target_x = self._apply_jitter(action.x, action.x_jitter_px)
            target_y = self._apply_jitter(action.y, action.y_jitter_px)
            abs_x, abs_y = self._screen_to_interception(target_x, target_y)
            stroke = InterceptionMouseStroke()
            stroke.state = 0
            stroke.flags = 0x001  # absolute
            stroke.rolling = 0
            stroke.x = abs_x
            stroke.y = abs_y
            stroke.information = 0
            self._send_mouse_stroke(stroke)
            time.sleep(0.02)
        else:
            # 不移动，但记录被忽略的移动次数，随后仍然执行按下/释放以在当前位置点击
            with self._ignored_lock:
                self.ignored_moves += 1
        
        # 按下
        stroke = InterceptionMouseStroke()
        stroke.state = 0x001
        stroke.flags = 0
        stroke.x = 0
        stroke.y = 0
        stroke.information = 0
        self._send_mouse_stroke(stroke)
        hold_ms = self._apply_jitter(action.hold_ms, action.hold_jitter_ms, minimum=1)
        time.sleep(hold_ms / 1000.0)
        
        # 释放
        stroke = InterceptionMouseStroke()
        stroke.state = 0x002
        stroke.flags = 0
        stroke.x = 0
        stroke.y = 0
        stroke.information = 0
        self._send_mouse_stroke(stroke)
        
        if move_allowed:
            self.logger.debug("点击 (%d, %d)", target_x, target_y)
        else:
            self.logger.debug("点击（忽略移动，当前位置点击）")
    
    def _execute_move(self, action: MoveAction) -> None:
        """执行移动"""
        # 如果全局配置关闭鼠标移动，则忽略此移动动作
        move_allowed = True
        try:
            if getattr(self, 'clicker', None) is not None:
                move_allowed = bool(getattr(self.clicker.config, 'move_mouse', True))
        except Exception:
            move_allowed = True

        if not move_allowed:
            with self._ignored_lock:
                self.ignored_moves += 1
            self.logger.debug("移动指令已被忽略（move_mouse=False）")
            return

        target_x = self._apply_jitter(action.x, action.x_jitter_px)
        target_y = self._apply_jitter(action.y, action.y_jitter_px)
        abs_x, abs_y = self._screen_to_interception(target_x, target_y)
        stroke = InterceptionMouseStroke()
        stroke.state = 0
        stroke.flags = 0x001  # absolute
        stroke.rolling = 0
        stroke.x = abs_x
        stroke.y = abs_y
        stroke.information = 0
        self._send_mouse_stroke(stroke)
        duration_ms = self._apply_jitter(action.duration_ms, action.duration_jitter_ms, minimum=1)
        time.sleep(duration_ms / 1000.0)
        self.logger.debug("移动到 (%d, %d)", target_x, target_y)
    
    def _execute_key(self, action: KeyAction) -> None:
        """执行按键。"""
        scancode, extended = self._vk_to_scancode(action.vk_code)
        if scancode is None:
            self.logger.warning("未知 VK，无法通过 Interception 发送: 0x%02x", action.vk_code)
            return

        if self._send_key_event(scancode, INTERCEPTION_KEY_DOWN, extended):
            hold_ms = self._apply_jitter(action.hold_ms, action.hold_jitter_ms, minimum=1)
            time.sleep(hold_ms / 1000.0)
            self._send_key_event(scancode, INTERCEPTION_KEY_UP, extended)
            self.logger.debug("按键 Interception VK=0x%02x SC=0x%02x", action.vk_code, scancode)
            return

        try:
            user32.keybd_event(action.vk_code, 0, 0, 0)
            hold_ms = self._apply_jitter(action.hold_ms, action.hold_jitter_ms, minimum=1)
            time.sleep(hold_ms / 1000.0)
            user32.keybd_event(action.vk_code, 0, KEYEVENTF_KEYUP, 0)
            self.logger.warning("按键回退 keybd_event VK=0x%02x", action.vk_code)
        except Exception as e:
            self.logger.error("按键失败: %s", e)

    def _execute_combo(self, action: ComboAction) -> None:
        """执行组合按键。"""
        key_specs: list[tuple[int, bool]] = []
        for vk_code in action.vk_codes:
            scancode, extended = self._vk_to_scancode(vk_code)
            if scancode is None:
                self.logger.warning("未知 VK，跳过组合键中的该键: 0x%02x", vk_code)
                continue
            key_specs.append((scancode, extended))

        if not key_specs:
            return

        sent_all = True
        for scancode, extended in key_specs:
            sent_all = self._send_key_event(scancode, INTERCEPTION_KEY_DOWN, extended) and sent_all

        hold_ms = self._apply_jitter(action.hold_ms, action.hold_jitter_ms, minimum=1)
        time.sleep(hold_ms / 1000.0)

        for scancode, extended in reversed(key_specs):
            self._send_key_event(scancode, INTERCEPTION_KEY_UP, extended)

        if sent_all:
            self.logger.debug("组合按键 Interception VK=%s", "+".join(f"0x{vk:02x}" for vk in action.vk_codes))
            return

        for vk_code in reversed(action.vk_codes):
            try:
                user32.keybd_event(vk_code, 0, KEYEVENTF_KEYUP, 0)
            except Exception:
                pass
        self.logger.warning("组合按键部分失败，已回退 keybd_event VK=%s", "+".join(f"0x{vk:02x}" for vk in action.vk_codes))
    
    def _execute_wait(self, action: WaitAction) -> None:
        """执行等待"""
        duration_ms = self._apply_jitter(action.duration_ms, action.duration_jitter_ms, minimum=1)
        time.sleep(duration_ms / 1000.0)
        self.logger.debug("等待 %dms", duration_ms)

    # ---- 窗口区域连点（region_click）----

    def _get_cursor_pos(self) -> tuple[int, int]:
        """读取当前光标位置（虚拟屏绝对坐标）。"""
        point = POINT()
        try:
            if user32.GetCursorPos(ctypes.byref(point)):
                return int(point.x), int(point.y)
        except Exception as e:
            self.logger.debug("GetCursorPos 失败: %s", e)
        return 0, 0

    def _planner_kwargs(self, overrides: Optional[dict] = None) -> dict:
        """过滤出 PathPlanner 认识的扰动参数（动作自带参数可覆盖全局）。"""
        merged = dict(self.path_planner_params or {})
        if overrides:
            merged.update(overrides)
        return {k: v for k, v in merged.items() if k in self._PLANNER_PARAM_KEYS}

    def _resolve_strategy(self, action: RegionClickAction) -> str:
        """解析路径策略：'global' 跟随 path_planner.json，其余直接使用。"""
        strategy = str(action.path_strategy or "global").strip().lower()
        if strategy in PathPlanner.VALID_STRATEGIES:
            return strategy
        if strategy != "global":
            self.logger.warning("未知路径策略 '%s'，回退到全局配置", action.path_strategy)
        fallback = str((self.path_planner_params or {}).get("strategy", "fitts")).lower()
        return fallback if fallback in PathPlanner.VALID_STRATEGIES else "fitts"

    def _build_base_moves(self, dx: int, dy: int, duration_ms: int, steps: int) -> List[dict]:
        """合成均匀插值的直线基准路径。

        时间戳必须严格递增且 > 0，否则 PathPlanner._norm_times 会因跳度为 0
        返回 None，导致扰动静默失效。x/y 为逐步增量，末步自带舍入误差校正。
        """
        total_seconds = max(1, int(duration_ms)) / 1000.0
        base: List[dict] = []
        prev_ix, prev_iy = 0, 0
        for i in range(steps):
            frac = (i + 1) / steps
            ix = int(round(dx * frac))
            iy = int(round(dy * frac))
            base.append({
                "t": total_seconds * frac,
                "type": "move",
                "x": ix - prev_ix,
                "y": iy - prev_iy,
            })
            prev_ix, prev_iy = ix, iy
        return base

    def _plan_relative_move(self, dx: int, dy: int, duration_ms: int,
                            action: RegionClickAction) -> List[dict]:
        """按用户设定的耗时与策略，生成两点之间的相对移动序列。"""
        if dx == 0 and dy == 0:
            return []
        steps = int(action.path_steps or 0)
        if steps <= 0:
            # 约 8ms 一步（模拟 125Hz 采样），并限制在 4~120 步之间
            steps = max(4, min(120, int(max(1, duration_ms) / 8)))
        base = self._build_base_moves(dx, dy, duration_ms, steps)
        strategy = self._resolve_strategy(action)
        try:
            planner = PathPlanner(strategy=strategy, **self._planner_kwargs(action.path_params))
            return planner.generate_path(dx, dy, base)
        except Exception as e:
            self.logger.warning("路径规划失败（%s），回退直线基准: %s", strategy, e)
            return base

    def _send_relative_move(self, dx: int, dy: int) -> bool:
        """发送一个相对移动 stroke（flags=0，不带 MOUSE_MOVE_ABSOLUTE）。"""
        stroke = InterceptionMouseStroke()
        stroke.state = 0
        stroke.flags = 0
        stroke.rolling = 0
        stroke.x = int(dx)
        stroke.y = int(dy)
        stroke.information = 0
        return self._send_mouse_stroke(stroke)

    def _send_button(self, state: int) -> bool:
        """发送一个鼠标按键 stroke（down 或 up）。"""
        stroke = InterceptionMouseStroke()
        stroke.state = int(state)
        stroke.flags = 0
        stroke.rolling = 0
        stroke.x = 0
        stroke.y = 0
        stroke.information = 0
        return self._send_mouse_stroke(stroke)

    def _send_relative_moves(self, moves: List[dict],
                             stop_event: Optional[threading.Event],
                             pause_event: Optional[threading.Event]) -> bool:
        """按时间戳分段发送相对移动，支持暂停/停止中断。返回 False 表示被中断。"""
        prev_t = 0.0
        for ev in moves:
            if stop_event and stop_event.is_set():
                return False
            if not self._wait_until_ready(stop_event, pause_event):
                return False
            try:
                t = float(ev.get("t", prev_t))
            except (TypeError, ValueError):
                t = prev_t
            gap = t - prev_t
            if gap > 0:
                prev_t = t
                if not self._sleep_with_controls(gap, stop_event, pause_event):
                    return False
            dx = int(ev.get("x", 0) or 0)
            dy = int(ev.get("y", 0) or 0)
            if dx == 0 and dy == 0:
                continue  # no-op 占位不发送
            self._send_relative_move(dx, dy)
        return True

    def _pick_spot(self, bounds: tuple[int, int, int, int],
                   prev_spot: Optional[tuple[int, int]],
                   min_distance: int) -> tuple[int, int]:
        """在采样范围内随机取一个点，尽量远离上一个点。"""
        x0, y0, x1, y1 = bounds
        candidate = ((x0 + x1) // 2, (y0 + y1) // 2)
        for _ in range(12):
            spot = (random.randint(x0, x1), random.randint(y0, y1))
            candidate = spot
            if prev_spot is None or min_distance <= 0:
                return spot
            dist = math.hypot(spot[0] - prev_spot[0], spot[1] - prev_spot[1])
            if dist >= min_distance:
                return spot
        # 拒绝采样全部失败（区域太小）：用最后一次采样结果，保证不会卡死
        return candidate

    def _correct_drift(self, target_x: int, target_y: int, threshold: int) -> None:
        """对比实际光标位置与目标点，超阈值时补发一个小的相对位移。"""
        if threshold <= 0:
            return
        cursor_x, cursor_y = self._get_cursor_pos()
        if cursor_x == 0 and cursor_y == 0:
            return
        dx = int(target_x) - cursor_x
        dy = int(target_y) - cursor_y
        if abs(dx) <= threshold and abs(dy) <= threshold:
            return
        self.logger.debug("漂移校正: (%d,%d) -> (%d,%d)", cursor_x, cursor_y, target_x, target_y)
        self._send_relative_move(dx, dy)

    def _click_at_spot(self, spot: tuple[int, int], action: RegionClickAction,
                       down_state: int, up_state: int,
                       stop_event: Optional[threading.Event],
                       pause_event: Optional[threading.Event]) -> bool:
        """在 spot 上执行一次点击。

        落点抖动用"微移 + 反向补偿"：按下前偏移 ±jitter，抬起后反向补回，
        这样 spot 基准不会随点击次数随机游走，而每次落点仍然独立随机。
        """
        jx = random.randint(-action.x_jitter_px, action.x_jitter_px) if action.x_jitter_px > 0 else 0
        jy = random.randint(-action.y_jitter_px, action.y_jitter_px) if action.y_jitter_px > 0 else 0
        if jx or jy:
            self._send_relative_move(jx, jy)

        self._send_button(down_state)
        hold_ms = self._apply_jitter(action.hold_ms, action.hold_jitter_ms, minimum=1)
        interrupted = not self._sleep_with_controls(hold_ms / 1000.0, stop_event, pause_event)
        self._send_button(up_state)
        if interrupted:
            return False

        if jx or jy:
            self._send_relative_move(-jx, -jy)
        return True

    def _execute_region_click(self, action: RegionClickAction,
                              stop_event: Optional[threading.Event],
                              pause_event: Optional[threading.Event]) -> bool:
        """执行窗口区域连点：随机取点 → 连点 N 次 → 拟人轨迹移动到下一点。"""
        width = int(action.region_width)
        height = int(action.region_height)
        if width <= 0 or height <= 0:
            self.logger.error("区域连点参数非法：区域尺寸为 %dx%d", width, height)
            return False

        down_state, up_state = self._BUTTON_STATES.get(
            str(action.button or "left").strip().lower(), self._BUTTON_STATES["left"])

        margin = max(0, int(action.margin_px))
        x0 = int(action.region_x) + margin
        y0 = int(action.region_y) + margin
        x1 = int(action.region_x) + width - margin
        y1 = int(action.region_y) + height - margin
        if x1 < x0 or y1 < y0:
            # margin 过大导致范围反转：收缩到区域中心，保证仍可采样
            center_x = int(action.region_x) + width // 2
            center_y = int(action.region_y) + height // 2
            x0, x1, y0, y1 = center_x, center_x, center_y, center_y
        bounds = (x0, y0, x1, y1)

        total_limit = max(0, int(action.clicks))
        run_forever = bool(action.forever) or total_limit <= 0
        strategy = self._resolve_strategy(action)

        self.region_click_total = 0
        self.region_click_spots = 0
        self.region_click_active = True
        self.logger.info(
            "区域连点开始：%dx%d @ (%d,%d) | 每点 %d±%d 次 | 间隔 %d±%dms | 按住 %d±%dms | 移动 %d±%dms | 策略 %s",
            width, height, action.region_x, action.region_y,
            action.clicks_per_spot, action.clicks_per_spot_jitter,
            action.interval_ms, action.interval_jitter_ms,
            action.hold_ms, action.hold_jitter_ms,
            action.move_duration_ms, action.move_duration_jitter_ms,
            strategy,
        )

        try:
            spot = self._pick_spot(bounds, None, action.min_spot_distance_px)
            self.region_click_spots += 1

            # 首点进入：从当前光标位置相对移动过去（不做绝对跳转）
            cursor_x, cursor_y = self._get_cursor_pos()
            first_duration = self._apply_jitter(
                action.move_duration_ms, action.move_duration_jitter_ms, minimum=1)
            if not self._send_relative_moves(
                self._plan_relative_move(spot[0] - cursor_x, spot[1] - cursor_y,
                                         first_duration, action),
                stop_event, pause_event,
            ):
                return False
            self._correct_drift(spot[0], spot[1], action.correct_drift_px)

            while True:
                if stop_event and stop_event.is_set():
                    return False
                if not self._wait_until_ready(stop_event, pause_event):
                    return False

                spot_clicks = self._apply_jitter(
                    action.clicks_per_spot, action.clicks_per_spot_jitter, minimum=1)
                for i in range(spot_clicks):
                    if not self._click_at_spot(spot, action, down_state, up_state,
                                               stop_event, pause_event):
                        return False
                    self.region_click_total += 1
                    if not run_forever and self.region_click_total >= total_limit:
                        self.logger.info("区域连点完成：共 %d 次点击，%d 个点",
                                         self.region_click_total, self.region_click_spots)
                        return True
                    if i < spot_clicks - 1:
                        interval = self._apply_jitter(
                            action.interval_ms, action.interval_jitter_ms, minimum=0)
                        if interval > 0 and not self._sleep_with_controls(
                                interval / 1000.0, stop_event, pause_event):
                            return False

                # 换点：拟人轨迹移动到下一个随机点
                next_spot = self._pick_spot(bounds, spot, action.min_spot_distance_px)
                duration = self._apply_jitter(
                    action.move_duration_ms, action.move_duration_jitter_ms, minimum=1)
                if not self._send_relative_moves(
                    self._plan_relative_move(next_spot[0] - spot[0], next_spot[1] - spot[1],
                                             duration, action),
                    stop_event, pause_event,
                ):
                    return False
                spot = next_spot
                self.region_click_spots += 1
                self._correct_drift(spot[0], spot[1], action.correct_drift_px)

                if action.spot_pause_ms > 0:
                    pause_ms = self._apply_jitter(
                        action.spot_pause_ms, action.spot_pause_jitter_ms, minimum=0)
                    if pause_ms > 0 and not self._sleep_with_controls(
                            pause_ms / 1000.0, stop_event, pause_event):
                        return False
        finally:
            self.region_click_active = False

    def _execute_timed(self, action: TimedAction, stop_event: Optional[threading.Event], pause_event: Optional[threading.Event]) -> bool:
        """执行 TimedAction：在 execute_ms 窗口内重复运行内部 actions，然后休眠 sleep_ms。"""
        exec_seconds = max(0, action.execute_ms) / 1000.0
        sleep_seconds = max(0, action.sleep_ms) / 1000.0

        def run_one_window() -> bool:
            # 执行窗口开始
            end_time = time.time() + exec_seconds
            while time.time() < end_time:
                if stop_event and stop_event.is_set():
                    return False

                # 如果暂停，则等待并延长 end_time
                if pause_event is not None and not pause_event.is_set():
                    paused_at = time.time()
                    if not self._wait_until_ready(stop_event, pause_event):
                        return False
                    resumed_at = time.time()
                    # 延长执行窗口
                    end_time += (resumed_at - paused_at)
                    continue

                # 执行一次内部动作序列
                if not self._execute_actions(action.actions, stop_event, pause_event):
                    return False
                # 如果内部动作耗时较长，loop 会自然超过窗口；下一次循环会检查时间
            return True

        iteration = 0
        while action.forever or iteration < max(1, int(action.repeat)):
            if stop_event and stop_event.is_set():
                return False
            if not self._wait_until_ready(stop_event, pause_event):
                return False

            if exec_seconds > 0:
                if not run_one_window():
                    return False

            # 进入可中断睡眠
            if sleep_seconds > 0:
                if not self._sleep_with_controls(sleep_seconds, stop_event, pause_event):
                    return False

            iteration += 1
        return True

    def _wait_until_ready(
        self,
        stop_event: Optional[threading.Event],
        pause_event: Optional[threading.Event],
        poll_interval: float = 0.05,
    ) -> bool:
        """等待脚本恢复运行，返回 False 表示已请求停止。"""
        while True:
            if stop_event and stop_event.is_set():
                return False
            if pause_event is None or pause_event.is_set():
                return True
            time.sleep(poll_interval)

    def _sleep_with_controls(
        self,
        duration: float,
        stop_event: Optional[threading.Event],
        pause_event: Optional[threading.Event],
    ) -> bool:
        """支持暂停/停止的分段睡眠。"""
        deadline = time.time() + duration
        while True:
            if stop_event and stop_event.is_set():
                return False
            if pause_event is not None and not pause_event.is_set():
                if not self._wait_until_ready(stop_event, pause_event):
                    return False
                continue
            remaining = deadline - time.time()
            if remaining <= 0:
                return True
            time.sleep(min(0.05, remaining))

    def _execute_actions(
        self,
        actions: List[Any],
        stop_event: Optional[threading.Event] = None,
        pause_event: Optional[threading.Event] = None,
    ) -> bool:
        """执行动作序列的内部实现。"""
        for action in actions:
            if stop_event and stop_event.is_set():
                return False
            if not self._wait_until_ready(stop_event, pause_event):
                return False

            if isinstance(action, LoopAction):
                iteration = 0
                max_iterations = action.count if action.count > 0 else 1
                while action.forever or iteration < max_iterations:
                    if stop_event and stop_event.is_set():
                        return False
                    if not self._wait_until_ready(stop_event, pause_event):
                        return False
                    if not self._execute_actions(action.actions, stop_event, pause_event):
                        return False
                    if action.pause_ms > 0:
                        pause_ms = self._apply_jitter(action.pause_ms, action.pause_jitter_ms, minimum=0)
                        if pause_ms > 0 and not self._sleep_with_controls(pause_ms / 1000.0, stop_event, pause_event):
                            return False
                    iteration += 1
                continue

            if isinstance(action, RegionClickAction):
                if not self._execute_region_click(action, stop_event, pause_event):
                    return False
            elif isinstance(action, ClickAction):
                self._execute_click(action)
            elif isinstance(action, MoveAction):
                self._execute_move(action)
            elif isinstance(action, KeyAction):
                self._execute_key(action)
            elif isinstance(action, ComboAction):
                self._execute_combo(action)
            elif isinstance(action, TimedAction):
                if not self._execute_timed(action, stop_event, pause_event):
                    return False
            elif isinstance(action, WaitAction):
                if not self._sleep_with_controls(action.duration_ms / 1000.0, stop_event, pause_event):
                    return False

        return True
    
    def execute_sequence(
        self,
        actions: List[Any],
        stop_event: Optional[threading.Event] = None,
        pause_event: Optional[threading.Event] = None,
    ) -> bool:
        """执行动作序列"""
        try:
            return self._execute_actions(actions, stop_event, pause_event)
        except Exception as e:
            self.logger.error("执行序列失败: %s", e)
            return False


class ActionScriptManager:
    """动作脚本管理器"""
    
    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir)
        self.scripts_dir = self.data_dir / "action_scripts"
        self.scripts_dir.mkdir(parents=True, exist_ok=True)
        self.logger = logging.getLogger("action_manager")
    
    def load_script(self, script_name: str) -> List[Any]:
        """加载脚本"""
        path = self.scripts_dir / f"{script_name}.json"
        if not path.exists():
            self.logger.warning("脚本不存在: %s", path)
            return []
        
        try:
            with open(path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            
            actions = [self._parse_action(item) for item in data.get("actions", [])]
            actions = [action for action in actions if action is not None]
            
            self.logger.info("已加载脚本: %s (%d 个动作)", script_name, len(actions))
            return actions
        
        except Exception as e:
            self.logger.error("加载脚本失败: %s", e)
            return []

    def _parse_action(self, item: dict) -> Any:
        """把 JSON 动作转换为内部动作对象。"""
        action_type = item.get("type")
        if action_type == "click":
            return ClickAction(
                item["x"],
                item["y"],
                item.get("hold_ms", 100),
                item.get("x_jitter_px", 0),
                item.get("y_jitter_px", 0),
                item.get("hold_jitter_ms", 0),
            )
        if action_type == "move":
            return MoveAction(
                item["x"],
                item["y"],
                item.get("duration_ms", 100),
                item.get("x_jitter_px", 0),
                item.get("y_jitter_px", 0),
                item.get("duration_jitter_ms", 0),
            )
        if action_type == "key":
            return KeyAction(item["vk_code"], item.get("hold_ms", 50), item.get("hold_jitter_ms", 0))
        if action_type == "combo":
            return ComboAction(
                item.get("vk_codes", []),
                item.get("hold_ms", 50),
                item.get("hold_jitter_ms", 0),
            )
        if action_type == "wait":
            return WaitAction(item["duration_ms"], item.get("duration_jitter_ms", 0))
        if action_type == "region_click":
            return self._parse_region_click(item)
        if action_type == "loop":
            nested = [self._parse_action(sub_item) for sub_item in item.get("actions", [])]
            nested = [action for action in nested if action is not None]
            count = int(item.get("count", 1))
            forever = bool(item.get("forever", False) or item.get("until_exit", False) or count <= 0)
            if count <= 0:
                count = 1
            return LoopAction(
                actions=nested,
                count=count,
                forever=forever,
                pause_ms=int(item.get("pause_ms", 0)),
                pause_jitter_ms=int(item.get("pause_jitter_ms", 0)),
            )
        if action_type == "timed":
            nested = [self._parse_action(sub_item) for sub_item in item.get("actions", [])]
            nested = [action for action in nested if action is not None]
            execute_ms = int(item.get("execute_ms", 0))
            sleep_ms = int(item.get("sleep_ms", 0))
            repeat = int(item.get("repeat", 1))
            forever = bool(item.get("forever", False))
            return TimedAction(
                actions=nested,
                execute_ms=execute_ms,
                sleep_ms=sleep_ms,
                repeat=repeat,
                forever=forever,
            )
        self.logger.warning("未知动作类型: %s", action_type)
        return None

    def _parse_region_click(self, item: dict) -> Optional[RegionClickAction]:
        """解析 region_click 动作。

        区域支持三种写法：
          1) 内联对象："region": {"x":..,"y":..,"width":..,"height":..}（兼容 w/h）
          2) 扁平字段："region_x"/"region_y"/"region_width"/"region_height"
          3) 预设引用："region_preset": "预设名"（读 data/clicker_configs/region_presets.json）
        """
        region = item.get("region") or {}
        if not isinstance(region, dict):
            region = {}
        preset_name = str(item.get("region_preset") or "")
        if not region and preset_name:
            region = ConfigManager.get_region_preset(preset_name) or {}
            if not region:
                self.logger.warning("区域预设不存在: %s", preset_name)

        try:
            rx = int(region.get("x", item.get("region_x", 0)))
            ry = int(region.get("y", item.get("region_y", 0)))
            rw = int(region.get("width", region.get("w", item.get("region_width", 0))))
            rh = int(region.get("height", region.get("h", item.get("region_height", 0))))
        except (TypeError, ValueError) as e:
            self.logger.warning("region_click 区域坐标非法，已忽略该动作: %s", e)
            return None
        if rw <= 0 or rh <= 0:
            self.logger.warning("region_click 区域尺寸非法（%dx%d），已忽略该动作", rw, rh)
            return None

        defaults = RegionClickAction()
        path_params = item.get("path_params") or {}
        if not isinstance(path_params, dict):
            path_params = {}
        action = RegionClickAction(
            region_x=rx, region_y=ry, region_width=rw, region_height=rh,
            clicks=int(item.get("clicks", defaults.clicks)),
            forever=bool(item.get("forever", defaults.forever)),
            clicks_per_spot=int(item.get("clicks_per_spot", defaults.clicks_per_spot)),
            clicks_per_spot_jitter=int(item.get("clicks_per_spot_jitter", defaults.clicks_per_spot_jitter)),
            interval_ms=int(item.get("interval_ms", defaults.interval_ms)),
            interval_jitter_ms=int(item.get("interval_jitter_ms", defaults.interval_jitter_ms)),
            hold_ms=int(item.get("hold_ms", defaults.hold_ms)),
            hold_jitter_ms=int(item.get("hold_jitter_ms", defaults.hold_jitter_ms)),
            move_duration_ms=int(item.get("move_duration_ms", defaults.move_duration_ms)),
            move_duration_jitter_ms=int(item.get("move_duration_jitter_ms", defaults.move_duration_jitter_ms)),
            margin_px=int(item.get("margin_px", defaults.margin_px)),
            min_spot_distance_px=int(item.get("min_spot_distance_px", defaults.min_spot_distance_px)),
            x_jitter_px=int(item.get("x_jitter_px", defaults.x_jitter_px)),
            y_jitter_px=int(item.get("y_jitter_px", defaults.y_jitter_px)),
            spot_pause_ms=int(item.get("spot_pause_ms", defaults.spot_pause_ms)),
            spot_pause_jitter_ms=int(item.get("spot_pause_jitter_ms", defaults.spot_pause_jitter_ms)),
            path_strategy=str(item.get("path_strategy", defaults.path_strategy)),
            path_steps=int(item.get("path_steps", defaults.path_steps)),
            button=str(item.get("button", defaults.button)),
            correct_drift_px=int(item.get("correct_drift_px", defaults.correct_drift_px)),
            path_params=path_params,
        )
        # clicks > 0 时以次数为准，避免 forever 默认值覆盖用户意图
        if action.clicks > 0:
            action.forever = bool(item.get("forever", False))
        return action
    
    def save_script(self, script_name: str, actions: List[Any]) -> bool:
        """保存动作脚本（带统一 meta 头，便于与录制脚本统一管理）。"""
        path = self.scripts_dir / f"{script_name}.json"
        try:
            data = {
                "meta": {"type": "action", "version": 1, "name": script_name},
                "name": script_name,
                "actions": [_action_to_dict(a) for a in actions]
            }
            with open(path, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            self.logger.info("脚本已保存: %s", path)
            return True
        except Exception as e:
            self.logger.error("保存脚本失败: %s", e)
            return False

    def list_scripts(self) -> List[str]:
        """列出所有动作脚本（排除录制脚本）。"""
        result = []
        for f in self.scripts_dir.glob("*.json"):
            try:
                with open(f, "r", encoding="utf-8") as fh:
                    data = json.load(fh)
                    meta_type = data.get("meta", {}).get("type")
                    # 包含：显式 type="action"，或旧文件（无 meta 字段，type 为 None）
                    # 排除：type="recorded"
                    if meta_type in ("action", None):
                        result.append(f.stem)
            except Exception:
                # 格式错误，视作 action 脚本
                result.append(f.stem)
        return result
