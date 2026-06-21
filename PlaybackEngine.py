"""
PlaybackEngine — 读取 JSON 录制脚本，按事件时间戳回放。

支持的 JSON 版本：
  - v1: 键盘 key.vk 字段（虚拟键码）
  - v2: 键盘 key.code 字段（扫描码，推荐）

回放流程：
  1. load() 加载并验证脚本
  2. start() 开始回放（在后台线程中运行）
  3. stop() 立即停止回放
"""

from __future__ import annotations

import ctypes
import json
import logging
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Callable

from InterceptionCore import InterceptionMouseStroke, InterceptionKeyStroke

# Interception 鼠标状态位（与 interception.h 对齐）
# 注意：MOUSE_MOVE 不是 state 位，是 filter 位（0x1000）！
# 鼠标移动事件的 state=0，通过 flags 区分相对/绝对移动
MOUSE_LEFT_DOWN = 0x001
MOUSE_LEFT_UP   = 0x002
MOUSE_RIGHT_DOWN = 0x004
MOUSE_RIGHT_UP   = 0x008
MOUSE_MIDDLE_DOWN = 0x010
MOUSE_MIDDLE_UP   = 0x020
MOUSE_WHEEL = 0x400
MOUSE_HWHEEL = 0x800

# Interception 键盘状态
KEY_STATE_DOWN = 0x00
KEY_STATE_UP   = 0x01

# Interception 鼠标 flags
MOUSE_MOVE_ABSOLUTE = 0x01



@dataclass
class PlaybackEvent:
    t: float
    type: str
    raw: dict


class PlaybackEngine:
    """读取录制脚本并回放。"""

    def __init__(self, clicker, logger: Optional[logging.Logger] = None):
        self.clicker = clicker
        self.logger = logger or logging.getLogger("playback")

        self._script: Optional[dict] = None
        self._events: list[dict] = []
        self._playing = False
        self._paused = False
        self._stop_requested = False
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        # 暂停事件：set=运行，clear=暂停
        self._pause_event = threading.Event()
        self._pause_event.set()

        # 回放速度倍率（1.0 = 原始速度）
        self.speed: float = 1.0

        # 缓存的设备 ID（首次回放时枚举）
        self._keyboard_device: Optional[int] = None
        self._mouse_device: Optional[int] = None

        # 回放完成回调（由 ClickerManager 设置），签名 callback(success: bool, stopped: bool)
        self.on_complete: Optional[Callable[[bool, bool], None]] = None

    # ---- 加载 ----------------------------------------------------------------
    def load(self, filepath: str | Path) -> bool:
        """加载 JSON 脚本文件。"""
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                self._script = json.load(f)
        except Exception as e:
            self.logger.error("加载脚本失败: %s", e)
            return False

        meta = self._script.get("meta", {})
        if meta.get("type") != "recorded":
            self.logger.error("脚本 type 不是 'recorded'")
            return False

        self._events = self._script.get("events", [])
        self.logger.info(
            "已加载脚本: %s，共 %d 条事件",
            meta.get("name", "?"), len(self._events),
        )
        return True

    def get_meta(self) -> dict:
        return self._script.get("meta", {}) if self._script else {}

    # ---- 设备枚举 -----------------------------------------------------------
    def _find_keyboard_device(self) -> Optional[int]:
        """枚举找到第一个键盘设备 ID（1~10）。"""
        if self._keyboard_device is not None:
            return self._keyboard_device
        lib = self.clicker._lib
        if not lib:
            return None
        for device in range(1, 11):  # 键盘设备范围 1~10
            try:
                if lib.interception_is_keyboard(device) > 0:
                    self._keyboard_device = device
                    self.logger.info("回放：已找到键盘设备 %d", device)
                    return device
            except Exception:
                continue
        self.logger.warning("回放：未找到键盘设备")
        return None

    def _find_mouse_device(self) -> Optional[int]:
        """枚举找到第一个鼠标设备 ID（11~20）。"""
        if self._mouse_device is not None:
            return self._mouse_device
        lib = self.clicker._lib
        if not lib:
            return None
        for device in range(11, 21):  # 鼠标设备范围 11~20
            try:
                if lib.interception_is_mouse(device) > 0:
                    self._mouse_device = device
                    self.logger.info("回放：已找到鼠标设备 %d", device)
                    return device
            except Exception:
                continue
        self.logger.warning("回放：未找到鼠标设备")
        return None

    # ---- 回放控制 -----------------------------------------------------------
    def start(self) -> bool:
        """启动后台回放线程。"""
        if self._playing:
            return False
        if not self._events:
            self.logger.error("没有可回放的事件")
            return False
        if not self.clicker.is_ready():
            self.logger.error("Interception 未就绪")
            return False

        with self._lock:
            self._playing = True
            self._stop_requested = False
            self._playback_index = 0

        self._thread = threading.Thread(target=self._playback_loop, daemon=True)
        self._thread.start()
        return True

    def stop(self) -> None:
        with self._lock:
            self._stop_requested = True
            # 如果在暂停状态，恢复一下让线程能退出
            self._pause_event.set()

    def is_playing(self) -> bool:
        return self._playing

    def is_paused(self) -> bool:
        return self._playing and self._paused

    def pause(self) -> bool:
        """暂停回放。返回是否成功切换。"""
        with self._lock:
            if not self._playing or self._paused:
                return False
            self._paused = True
            self._pause_event.clear()
        self.logger.info("回放已暂停")
        return True

    def resume(self) -> bool:
        """恢复回放。返回是否成功切换。"""
        with self._lock:
            if not self._playing or not self._paused:
                return False
            self._paused = False
            self._pause_event.set()
        self.logger.info("回放已恢复")
        return True

    def toggle_pause(self) -> str:
        """切换暂停/恢复。返回 'paused' / 'resumed' / 'noop'。"""
        with self._lock:
            if not self._playing:
                return "noop"
            if self._paused:
                self._paused = False
                self._pause_event.set()
                self.logger.info("回放已恢复")
                return "resumed"
            else:
                self._paused = True
                self._pause_event.clear()
                self.logger.info("回放已暂停")
                return "paused"

    def get_progress(self) -> tuple[int, int]:
        """返回 (当前索引, 总事件数)。用于 UI 进度条。"""
        with self._lock:
            total = len(self._events)
            idx = getattr(self, "_playback_index", 0)
            return idx, total

    # ---- 内部回放循环 -------------------------------------------------------
    def _build_key_stroke(self, ev: dict) -> Optional[InterceptionKeyStroke]:
        """根据录制事件构造键盘 stroke（优先用扫描码 code，fallback 用 vk）。"""
        stroke = InterceptionKeyStroke()
        ctypes.memset(ctypes.byref(stroke), 0, ctypes.sizeof(InterceptionKeyStroke))

        scan_code = ev.get("code")
        if scan_code is None:
            # v1 格式：通过虚拟键码反向映射
            vk = ev.get("vk", 0)
            if not vk:
                return None
            # MapVirtualKeyW(vk, MAPVK_VK_TO_VSC=0)
            scan_code = ctypes.windll.user32.MapVirtualKeyW(vk, 0) & 0xFF
            if scan_code == 0:
                return None

        action = ev.get("action", "down")
        stroke.code = scan_code & 0xFFFF
        stroke.state = KEY_STATE_UP if action == "up" else KEY_STATE_DOWN
        stroke.information = 0
        return stroke

    def _build_mouse_stroke(self, ev: dict) -> Optional[InterceptionMouseStroke]:
        """构造鼠标 stroke（移动或点击）。

        move 事件的 x/y 是相对移动量，用相对模式（flags=0）发送。
        click 事件只发送按钮状态，不带坐标。
        """
        stroke = InterceptionMouseStroke()
        ctypes.memset(ctypes.byref(stroke), 0, ctypes.sizeof(InterceptionMouseStroke))

        typ = ev.get("type")
        if typ == "move":
            # 相对移动：state=0，flags=0 (INTERCEPTION_MOUSE_MOVE_RELATIVE)
            # ★ state 必须是 0，不是 0x800（0x800 是 HWHEEL 水平滚轮！）
            stroke.flags = 0
            stroke.state = 0
            stroke.x = int(ev.get("x", 0))
            stroke.y = int(ev.get("y", 0))
            return stroke

        elif typ == "click":
            # 点击：只发送按钮状态，不需要坐标
            button = ev.get("button", "left")
            action = ev.get("action", "down")
            if button == "left":
                stroke.state = MOUSE_LEFT_DOWN if action == "down" else MOUSE_LEFT_UP
            elif button == "right":
                stroke.state = MOUSE_RIGHT_DOWN if action == "down" else MOUSE_RIGHT_UP
            elif button == "middle":
                stroke.state = MOUSE_MIDDLE_DOWN if action == "down" else MOUSE_MIDDLE_UP
            else:
                return None
            stroke.flags = 0
            stroke.x = 0
            stroke.y = 0
            return stroke

        return None

    def _send_stroke(self, lib, ctx, device, stroke) -> bool:
        """发送一个 stroke 到指定设备（用数组方式，兼容 argtypes 声明）。"""
        try:
            arr = (InterceptionMouseStroke * 1)(stroke)
            result = lib.interception_send(ctx, device, arr, 1)
            return result > 0
        except Exception as e:
            self.logger.error("发送 stroke 失败 (device=%d): %s", device, e)
            return False

    def _move_to_screen_pos(self, lib, ctx, ms_device, x: int, y: int) -> bool:
        """把鼠标移动到屏幕像素坐标 (x, y)（用绝对坐标模式）。"""
        if not ms_device:
            return False
        # 屏幕像素坐标 → Interception 绝对坐标（0~65535）
        sw = max(ctypes.windll.user32.GetSystemMetrics(0) - 1, 1)
        sh = max(ctypes.windll.user32.GetSystemMetrics(1) - 1, 1)
        abs_x = int(max(0, min(x, sw)) * 65535 / sw)
        abs_y = int(max(0, min(y, sh)) * 65535 / sh)

        stroke = InterceptionMouseStroke()
        stroke.state = 0
        stroke.flags = MOUSE_MOVE_ABSOLUTE
        stroke.rolling = 0
        stroke.x = abs_x
        stroke.y = abs_y
        stroke.information = 0
        return self._send_stroke(lib, ctx, ms_device, stroke)

    def _playback_loop(self):
        lib = self.clicker._lib
        ctx = self.clicker._ctx
        if not ctx:
            self.logger.error("Interception 上下文无效")
            self._playing = False
            return

        # 枚举设备
        kb_device = self._find_keyboard_device()
        ms_device = self._find_mouse_device()
        if not kb_device and not ms_device:
            self.logger.error("找不到任何输入设备，无法回放")
            self._playing = False
            return

        # ★ 恢复录制时的初始鼠标位置（用 SetCursorPos，绕过加速）
        # 相对移动量叠加在初始位置上才能完全复现
        meta = self._script.get("meta", {}) if self._script else {}
        start_x = meta.get("start_cursor_x")
        start_y = meta.get("start_cursor_y")
        if start_x is not None and start_y is not None:
            try:
                ctypes.windll.user32.SetCursorPos(int(start_x), int(start_y))
                self.logger.info("已恢复鼠标初始位置: (%d, %d)", start_x, start_y)
                time.sleep(0.1)
            except Exception as e:
                self.logger.warning("恢复鼠标位置失败: %s", e)

        self.logger.info(
            "开始回放（共 %d 条事件，速度 %.1fx，键盘=%s，鼠标=%s）",
            len(self._events), self.speed, kb_device, ms_device,
        )
        last_t = 0.0

        try:
            for idx, ev in enumerate(self._events):
                with self._lock:
                    if self._stop_requested:
                        self.logger.info("回放被用户停止 (%d/%d)", idx, len(self._events))
                        break
                    self._playback_index = idx

                t = ev.get("t", 0.0)
                typ = ev.get("type", "")

                wait_t = (t - last_t) / self.speed
                if wait_t > 0:
                    # 分段 sleep，便于及时响应暂停/停止
                    remaining = wait_t
                    while remaining > 0:
                        # 暂停时阻塞（不消耗 remaining）
                        self._pause_event.wait(timeout=None)
                        with self._lock:
                            if self._stop_requested:
                                break
                        # 醒来后小段 sleep，避免占用 CPU
                        chunk = min(remaining, 0.02)
                        time.sleep(chunk)
                        remaining -= chunk
                with self._lock:
                    if self._stop_requested:
                        break

                try:
                    if typ == "key":
                        if kb_device is None:
                            continue
                        ks = self._build_key_stroke(ev)
                        if ks is None:
                            continue
                        send_buf = InterceptionMouseStroke()
                        ctypes.memmove(
                            ctypes.byref(send_buf), ctypes.byref(ks),
                            ctypes.sizeof(InterceptionKeyStroke),
                        )
                        self._send_stroke(lib, ctx, kb_device, send_buf)

                    elif typ in ("move", "click"):
                        if ms_device is None:
                            continue
                        ms = self._build_mouse_stroke(ev)
                        if ms is None:
                            continue
                        self._send_stroke(lib, ctx, ms_device, ms)

                    else:
                        self.logger.warning("未知事件类型: %s", typ)

                except Exception as e:
                    self.logger.error("回放失败 [%s]: %s", typ, e)

                last_t = t

        finally:
            stopped = self._stop_requested
            self.logger.info("回放完成（%d 条事件，stopped=%s）", len(self._events), stopped)
            with self._lock:
                self._playing = False
                self._paused = False
                self._playback_index = 0
                self._pause_event.set()
            # 通知上层回放结束
            if self.on_complete:
                try:
                    self.on_complete(True, stopped)
                except Exception as e:
                    self.logger.warning("on_complete 回调异常: %s", e)
