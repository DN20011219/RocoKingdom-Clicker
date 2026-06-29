"""
InputRecorder — 录制鼠标移动/点击 + 键盘输入，输出 JSON 脚本文件。

基于 Interception 原生 API 的正确用法（参考官方 interception.h）：
  1. interception_set_filter(ctx, predicate, filter)：谓词是函数（不是常量）
  2. 键盘 state：0=down, 1=up
  3. 鼠标按钮在 state 字段（bitmask），flags 仅表示移动类型
"""

from __future__ import annotations

import ctypes
import json
import logging
import queue
import threading
import time
import traceback
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from InterceptionCore import InterceptionMouseStroke, InterceptionKeyStroke


# ---- 按键名称映射（扫描码 -> 可读名称） --------------------------------
SCANCODE_NAMES = {
    0x01: "Esc", 0x02: "1", 0x03: "2", 0x04: "3", 0x05: "4", 0x06: "5",
    0x07: "6", 0x08: "7", 0x09: "8", 0x0A: "9", 0x0B: "0",
    0x0C: "-", 0x0D: "=", 0x0E: "Backspace", 0x0F: "Tab",
    0x10: "Q", 0x11: "W", 0x12: "E", 0x13: "R", 0x14: "T", 0x15: "Y",
    0x16: "U", 0x17: "I", 0x18: "O", 0x19: "P", 0x1A: "[", 0x1B: "]",
    0x1C: "Enter", 0x1D: "Ctrl(L)", 0x1E: "A", 0x1F: "S", 0x20: "D",
    0x21: "F", 0x22: "G", 0x23: "H", 0x24: "J", 0x25: "K", 0x26: "L",
    0x27: ";", 0x28: "'", 0x29: "`", 0x2A: "Shift(L)", 0x2B: "\\",
    0x2C: "Z", 0x2D: "X", 0x2E: "C", 0x2F: "V", 0x30: "B", 0x31: "N",
    0x32: "M", 0x33: ",", 0x34: ".", 0x35: "/", 0x36: "Shift(R)",
    0x37: "Numpad*", 0x38: "Alt(L)", 0x39: "Space", 0x3A: "CapsLock",
    # 功能键 F1~F10（AT Set 2 扫描码，Interception 使用此套）
    0x3B: "F1", 0x3C: "F2", 0x3D: "F3", 0x3E: "F4", 0x3F: "F5",
    0x40: "F6", 0x41: "F7", 0x42: "F8", 0x43: "F9", 0x44: "F10",
    0x45: "NumLock", 0x46: "ScrollLock", 0x47: "Numpad7", 0x48: "Numpad8",
    0x49: "Numpad9", 0x4A: "Numpad-", 0x4B: "Numpad4", 0x4C: "Numpad5",
    0x4D: "Numpad6", 0x4E: "Numpad+", 0x4F: "Numpad1", 0x50: "Numpad2",
    0x51: "Numpad3", 0x52: "Numpad0", 0x53: "Numpad.",
    0x57: "F11", 0x58: "F12",
    0x9C: "Enter(R)", 0x9D: "Ctrl(R)", 0xB5: "/(R)", 0xB8: "Alt(R)",
    0xC7: "Home", 0xC8: "Up", 0xC9: "PgUp", 0xCB: "Left", 0xCD: "Right",
    0xCF: "End", 0xD0: "Down", 0xD1: "PgDn", 0xD2: "Ins", 0xD3: "Del",
    0xDB: "Win(L)", 0xDC: "Win(R)", 0xDD: "Menu",
}

# Interception 标准鼠标按钮
MOUSE_BTN_LEFT_DOWN = 0x001
MOUSE_BTN_LEFT_UP = 0x002
MOUSE_BTN_RIGHT_DOWN = 0x004
MOUSE_BTN_RIGHT_UP = 0x008
MOUSE_BTN_MIDDLE_DOWN = 0x010
MOUSE_BTN_MIDDLE_UP = 0x020
MOUSE_WHEEL = 0x400
MOUSE_HWHEEL = 0x800


# ---- 数据模型 -----------------------------------------------------------------
@dataclass
class MoveEvent:
    """鼠标移动事件。x/y 是相对移动量（与 Interception 原生相对移动一致）。

    用于 3D 游戏等场景（光标被锁定在窗口中心，绝对坐标不可用）。
    不做节流，每个原生移动事件都记录，保证总移动量精确。
    """
    t: float
    type: str = "move"
    x: int = 0
    y: int = 0

    def to_dict(self) -> dict:
        return {"t": round(self.t, 3), "type": self.type, "x": self.x, "y": self.y}


@dataclass
class ClickEvent:
    t: float
    type: str = "click"
    x: int = 0
    y: int = 0
    button: str = "left"
    action: str = "down"

    def to_dict(self) -> dict:
        return {
            "t": round(self.t, 3), "type": self.type,
            "x": self.x, "y": self.y, "button": self.button, "action": self.action
        }


@dataclass
class KeyEvent:
    t: float
    type: str = "key"
    code: int = 0
    key: str = ""
    action: str = "down"

    def to_dict(self) -> dict:
        return {
            "t": round(self.t, 3), "type": self.type,
            "code": self.code, "key": self.key, "action": self.action
        }


RecordingEvent = MoveEvent | ClickEvent | KeyEvent


# ---- 调试浮窗 -----------------------------------------------------------------
class DebugOverlay:
    """录制期间把捕获到的事件通过回调推送给主 GUI（用 toast 显示）。

    不再自己创建 tkinter 窗口（在子线程创建 Tk 会崩溃，且与主窗口冲突）。
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._event_count = 0
        self._last_toast_t = 0.0
        # 事件回调：由 ClickerManager 设置，签名 callback(msg: str)
        self.on_event: Optional[Callable[[str], None]] = None

    def start(self):
        """无操作（兼容旧接口）。"""
        pass

    def stop(self):
        """无操作（兼容旧接口）。"""
        pass

    def log_event(self, msg: str):
        """记录一条事件，通过回调推送给主 GUI。

        为避免 toast 刷屏，每 0.5 秒最多推送一次（累积计数）。
        """
        with self._lock:
            self._event_count += 1

        # 通过回调推送（节流：0.5s 一次）
        now = time.time()
        if now - self._last_toast_t >= 0.5:
            self._last_toast_t = now
            try:
                if self.on_event:
                    with self._lock:
                        count = self._event_count
                    self.on_event(f"🎙 录制中… 已捕获 {count} 条")
            except Exception:
                pass

    def get_count(self) -> int:
        with self._lock:
            return self._event_count


# ---- 系统信息采集 --------------------------------------------------------------
def get_system_info() -> dict:
    user32 = ctypes.windll.user32
    width = user32.GetSystemMetrics(0)
    height = user32.GetSystemMetrics(1)
    return {"screen_width": width, "screen_height": height}


def get_cursor_pos() -> tuple[int, int]:
    """获取当前鼠标屏幕坐标。"""
    class POINT(ctypes.Structure):
        _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]
    pt = POINT()
    ctypes.windll.user32.GetCursorPos(ctypes.byref(pt))
    return int(pt.x), int(pt.y)


# ---- 主录制类 -----------------------------------------------------------------
class InputRecorder:
    """录制鼠标移动/点击/键盘事件，写入 JSON 脚本文件。"""

    # F1-F12 扫描码映射（AT Set 2，Interception 使用此套）
    # 默认热键：F7=开始, F8=停止保存, F9=取消（可由 ClickerManager 通过 set_hotkeys 修改）
    _FKEY_SCANCODE = {
        "F1": 0x3B, "F2": 0x3C, "F3": 0x3D, "F4": 0x3E,
        "F5": 0x3F, "F6": 0x40, "F7": 0x41, "F8": 0x42,
        "F9": 0x43, "F10": 0x44, "F11": 0x57, "F12": 0x58,
    }

    def __init__(
        self,
        target_window: str = "",
        output_dir: Optional[Path] = None,
        logger: Optional[logging.Logger] = None,
    ):
        self.logger = logger or logging.getLogger("input_recorder")
        self.target_window = target_window

        if output_dir is None:
            from Clicker import get_app_dir
            self.output_dir: Path = get_app_dir() / "data" / "action_scripts"
        else:
            self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)

        self._events: list[RecordingEvent] = []
        self._start_time: float = 0.0
        self._recording = False
        self._lock = threading.Lock()
        self._clicker_ref = None

        # 热键扫描码（可配置，默认 F7/F8/F9）
        self.HOTKEY_START = self._FKEY_SCANCODE["F7"]    # F7
        self.HOTKEY_STOP = self._FKEY_SCANCODE["F8"]     # F8
        self.HOTKEY_CANCEL = self._FKEY_SCANCODE["F9"]  # F9

        # 热键回调（由 ClickerManager 设置）：F8=停止保存，F9=取消
        # 签名：on_stop() / on_cancel()
        self.on_stop: Optional[Callable[[], None]] = None
        self.on_cancel: Optional[Callable[[], None]] = None

        self._capture_thread: Optional[threading.Thread] = None
        self._hotkey_thread: Optional[threading.Thread] = None
        self._last_move_t: float = -1.0  # 鼠标移动事件节流时间戳
        self._hotkey_stop = threading.Event()
        # 异步发送队列：录制循环只管 receive+记录，send 放后台线程执行
        # 避免 send 阻塞导致 receive 来不及取事件（驱动会合并未取走的 move 事件）
        self._send_queue: queue.Queue = queue.Queue()
        self._send_thread: Optional[threading.Thread] = None

        self._overlay = DebugOverlay()

    def set_hotkeys(self, start: str = "F7", stop: str = "F8", cancel: str = "F9") -> bool:
        """设置录制热键（F1-F12）。

        Args:
            start: 开始录制热键（仅记录用，录制循环不检测此键）
            stop: 停止录制并保存热键
            cancel: 取消录制热键

        Returns:
            True 表示设置成功；False 表示存在按键冲突（未修改配置）。
        """
        # 互斥判定：start/stop/cancel 三个键不能重复
        keys = [start, stop, cancel]
        if len(keys) != len(set(keys)):
            self.logger.warning(
                "录制热键冲突：start=%s stop=%s cancel=%s（存在重复按键，配置未修改）",
                start, stop, cancel,
            )
            return False
        self.HOTKEY_START = self._FKEY_SCANCODE.get(start, self._FKEY_SCANCODE["F7"])
        self.HOTKEY_STOP = self._FKEY_SCANCODE.get(stop, self._FKEY_SCANCODE["F8"])
        self.HOTKEY_CANCEL = self._FKEY_SCANCODE.get(cancel, self._FKEY_SCANCODE["F9"])
        self.logger.info("录制热键已设置: start=%s(0x%02X) stop=%s(0x%02X) cancel=%s(0x%02X)",
                         start, self.HOTKEY_START, stop, self.HOTKEY_STOP,
                         cancel, self.HOTKEY_CANCEL)
        return True

    # ---- 公开 API ------------------------------------------------------------
    def set_clicker(self, clicker) -> None:
        self._clicker_ref = clicker

    def start(self) -> None:
        """开始录制：记录初始鼠标坐标，打开调试浮窗，启动捕获线程。"""
        if self._recording:
            self.logger.warning("录制已在进行中")
            return

        # 记录录制开始时的鼠标屏幕坐标（回放前会把鼠标移到这个位置）
        try:
            self._start_cursor_x, self._start_cursor_y = get_cursor_pos()
        except Exception:
            self._start_cursor_x, self._start_cursor_y = 0, 0

        with self._lock:
            self._events = []
            self._start_time = time.perf_counter()
            self._recording = True
            self._last_move_t = -1.0

        self.logger.info(
            "录制开始（初始鼠标坐标: %d, %d）",
            self._start_cursor_x, self._start_cursor_y,
        )
        self._overlay.start()

        # 启动异步发送线程（把事件发回系统，避免阻塞录制循环）
        self._send_queue = queue.Queue()
        self._send_thread = threading.Thread(target=self._send_loop, daemon=True)
        self._send_thread.start()

        self._capture_thread = threading.Thread(target=self._capture_loop, daemon=True)
        self._capture_thread.start()

    def _send_loop(self):
        """后台线程：把录制循环收到的事件异步发回系统。

        录制循环只负责 receive + 记录，send 放这里执行。
        这样 receive 不会被 send 阻塞，能及时取走驱动队列里的 move 事件，
        避免驱动因队列满而合并/丢弃 move 事件（快速移动时尤其明显）。
        """
        lib = self._clicker_ref._lib if self._clicker_ref else None
        if lib is None:
            return
        while self._recording:
            try:
                item = self._send_queue.get(timeout=0.1)
            except queue.Empty:
                continue
            if item is None:  # 哨兵，停止信号
                break
            ctx, device, stroke_bytes = item
            try:
                # 重新构造 stroke 缓冲区并发送
                buf = InterceptionMouseStroke()
                ctypes.memmove(ctypes.byref(buf), stroke_bytes, ctypes.sizeof(buf))
                lib.interception_send(ctx, device, ctypes.byref(buf), 1)
            except Exception as e:
                self.logger.error("异步发送事件异常: %s", e)
            finally:
                # 释放 bytes 内存（Python 自动 GC，这里显式 del 一下）
                del stroke_bytes

    def stop(self) -> list[dict]:
        """停止录制，返回事件列表。"""
        if not self._recording:
            return []

        with self._lock:
            self._recording = False
            events = [e.to_dict() for e in self._events]

        # 停止异步发送线程
        try:
            self._send_queue.put(None)  # 哨兵
            if self._send_thread and self._send_thread.is_alive():
                self._send_thread.join(timeout=1.0)
        except Exception:
            pass

        self._overlay.stop()

        self.logger.info("录制结束：共 %d 条事件", len(events))
        return events

    def save(self, events: list[dict], name: str, description: str = "") -> Path:
        """保存录制脚本到文件。

        文件名格式：{脚本名前缀}_{日期}_{时间}.json
        例如：test_20260623_143025.json
        """
        try:
            import re
            safe_name = re.sub(r"[^\w\-]", "_", name).strip("_")
            # 本地时间生成文件名（用户更直观），UTC 时间记录在 meta.recorded_at
            now_local = datetime.now()
            date_str = now_local.strftime("%Y%m%d")
            time_str = now_local.strftime("%H%M%S")
            filename = f"{safe_name}_{date_str}_{time_str}.json"
            filepath = self.output_dir / filename

            meta = {
                "type": "recorded",
                "version": 2,
                "name": name,
                "recorded_at": datetime.now(timezone.utc).isoformat(),
                "description": description,
                "target_window": self.target_window,
                "system": get_system_info(),
                # 录制开始时的鼠标屏幕坐标；回放前会把鼠标移到这里，再按相对量重放
                "start_cursor_x": getattr(self, "_start_cursor_x", 0),
                "start_cursor_y": getattr(self, "_start_cursor_y", 0),
            }

            payload = {"meta": meta, "events": events}
            with open(filepath, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2, ensure_ascii=False)

            self.logger.info("脚本已保存: %s（%d 字节，%d 条事件）",
                             filepath, filepath.stat().st_size, len(events))
            return filepath
        except Exception as e:
            self.logger.error("保存脚本异常: %s\n%s", e, traceback.format_exc())
            raise

    def is_recording(self) -> bool:
        return self._recording

    def get_event_count(self) -> int:
        return self._overlay.get_count()

    # ---- 捕获循环（核心） ---------------------------------------------------
    def _capture_loop(self):
        """后台线程：捕获键盘 + 鼠标事件。

        所有关键步骤都打日志，异常带 traceback，避免闪退后无法定位。
        """
        self.logger.info("录制捕获线程启动")
        clicker = self._clicker_ref
        if clicker is None or not clicker.is_ready():
            self.logger.error("录制失败：Interception 未就绪 (clicker=%s)", clicker)
            return

        lib = clicker._lib
        if lib is None:
            self.logger.error("录制失败：interception lib 为空")
            return

        try:
            ctx = lib.interception_create_context()
        except Exception as e:
            self.logger.error("录制失败：创建上下文异常: %s\n%s", e, traceback.format_exc())
            return
        if not ctx:
            self.logger.error("录制失败：创建上下文返回 NULL")
            return

        self.logger.info("录制：已创建 Interception 上下文 ctx=%s", ctx)

        try:
            # 设置过滤器：捕获所有键盘 + 鼠标事件
            try:
                lib.interception_set_filter(
                    ctx, lib._is_keyboard_pred, lib.INTERCEPTION_FILTER_KEY_ALL,
                )
                lib.interception_set_filter(
                    ctx, lib._is_mouse_pred, lib.INTERCEPTION_FILTER_MOUSE_ALL,
                )
            except Exception as e:
                self.logger.error("录制失败：设置过滤器异常: %s\n%s", e, traceback.format_exc())
                return

            self.logger.info("录制循环已启动，等待事件…")

            # 用 InterceptionMouseStroke 作为通用缓冲区（两种 struct 中较大的那个）
            mouse_stroke = InterceptionMouseStroke()
            # 批量接收缓冲区：一次最多取 64 个事件，避免驱动队列积压
            BATCH_SIZE = 64
            batch_buf = (InterceptionMouseStroke * BATCH_SIZE)()

            self._overlay.log_event(">>> 录制已开始：按停止键保存 | 按取消键放弃 <<<")

            _loop_count = 0

            while self._recording and not self._hotkey_stop.is_set():
                _loop_count += 1
                try:
                    device = lib.interception_wait(ctx)
                except Exception as e:
                    self.logger.error("录制：interception_wait 异常: %s\n%s", e, traceback.format_exc())
                    break

                if device <= 0:
                    time.sleep(0.001)
                    continue

                # ★ 批量 receive：一次取走队列里所有积压事件
                # 快速移动鼠标时驱动会批量产生 move 事件，一次只取 1 个会来不及，
                # 驱动队列满后会合并/丢弃事件，导致总移动量减少
                try:
                    n = lib.interception_receive(ctx, device, batch_buf, BATCH_SIZE)
                except Exception as e:
                    self.logger.error("录制：interception_receive 异常 (device=%s): %s\n%s",
                                      device, e, traceback.format_exc())
                    continue
                if n <= 0:
                    continue

                try:
                    is_kb = lib.interception_is_keyboard(device)
                    is_ms = lib.interception_is_mouse(device)
                except Exception as e:
                    self.logger.error("录制：is_keyboard/is_mouse 异常: %s\n%s", e, traceback.format_exc())
                    continue

                # 逐个处理本批次的事件
                for i in range(n):
                    t = time.perf_counter() - self._start_time
                    stroke = batch_buf[i]
                    if is_kb:
                        self._handle_keyboard_event(lib, ctx, device, stroke, t)
                    elif is_ms:
                        self._handle_mouse_event(lib, ctx, device, stroke, t)

        except Exception as e:
            self.logger.error("录制循环致命异常: %s\n%s", e, traceback.format_exc())
        finally:
            try:
                lib.interception_destroy_context(ctx)
                self.logger.info("录制：已销毁 Interception 上下文")
            except Exception as e:
                self.logger.error("录制：销毁上下文异常: %s\n%s", e, traceback.format_exc())
            self.logger.info("录制捕获线程退出（共处理 %d 次循环）", _loop_count)

    def _handle_keyboard_event(self, lib, ctx, device, mouse_stroke, t: float):
        """处理键盘事件。所有异常被捕获并记录，不会让录制线程崩溃。"""
        try:
            ks_ptr = ctypes.cast(ctypes.byref(mouse_stroke), ctypes.POINTER(InterceptionKeyStroke))
            ks = ks_ptr[0]
            scan_code = ks.code & 0xFFFF
            state = ks.state & 0xFFFF

            # 忽略 E0/E1 前缀键（扩展键的辅助前缀）
            if scan_code in (0xE0, 0xE1):
                self._enqueue_send(ctx, device, mouse_stroke)
                return

            action = "up" if (state & 0x0001) else "down"

            # ★ 检测停止/取消热键（只在按下时触发，不记录这两个键）
            if action == "down" and scan_code == self.HOTKEY_STOP:
                self.logger.info("录制：检测到停止保存热键 (scan=0x%02X)", scan_code)
                self._overlay.log_event(f"[{t:6.2f}s] ⏹ 停止录制热键")
                self._enqueue_send(ctx, device, mouse_stroke)
                try:
                    if self.on_stop:
                        self.on_stop()
                except Exception as e:
                    self.logger.error("on_stop 回调异常: %s\n%s", e, traceback.format_exc())
                return

            if action == "down" and scan_code == self.HOTKEY_CANCEL:
                self.logger.info("录制：检测到取消热键 (scan=0x%02X)", scan_code)
                self._overlay.log_event(f"[{t:6.2f}s] ✕ 取消录制热键")
                self._enqueue_send(ctx, device, mouse_stroke)
                try:
                    if self.on_cancel:
                        self.on_cancel()
                except Exception as e:
                    self.logger.error("on_cancel 回调异常: %s\n%s", e, traceback.format_exc())
                return

            key_name = SCANCODE_NAMES.get(scan_code, f"scan_{scan_code:02X}")

            with self._lock:
                self._events.append(KeyEvent(
                    t=t, code=scan_code, key=key_name, action=action
                ))
            self._overlay.log_event(
                f"[{t:6.2f}s] ⌨  {key_name:12s}  {action.upper()}"
            )

            # 异步发回系统（不阻塞录制循环）
            self._enqueue_send(ctx, device, mouse_stroke)

        except Exception as e:
            self.logger.error("处理键盘事件异常: %s\n%s", e, traceback.format_exc())

    def _handle_mouse_event(self, lib, ctx, device, mouse_stroke, t: float):
        """处理鼠标事件。所有异常被捕获并记录，不会让录制线程崩溃。"""
        try:
            state = mouse_stroke.state
            flags = mouse_stroke.flags
            x = mouse_stroke.x
            y = mouse_stroke.y
            rolling = mouse_stroke.rolling

            # ★ 鼠标移动事件判断（参考 interception.h）：
            # - MOUSE_MOVE 是 filter 位 (0x1000)，不是 state 位！
            # - 鼠标移动事件的 state=0，通过 flags 区分：
            #   flags=0 → 相对移动，flags=1 → 绝对移动
            # - 判断条件：state 无按钮位 且 x/y 非零（相对移动）
            #   或 flags 含 MOVE_RELATIVE(0) 且 x/y 非零
            is_relative_move = (not (flags & 0x01)) and (x != 0 or y != 0)
            if is_relative_move:
                # 相对移动：记录原始 x/y
                with self._lock:
                    self._events.append(MoveEvent(t=t, x=x, y=y))
                self._overlay.log_event(f"[{t:6.2f}s] 🖱  MOVE  @({x},{y})")

            # 鼠标按钮（state 字段的 bitmask）
            if state & 0x001:  # LEFT_DOWN
                with self._lock:
                    self._events.append(ClickEvent(t=t, x=x, y=y, button="left", action="down"))
                self._overlay.log_event(f"[{t:6.2f}s] 🖱  LEFT  DOWN  @({x},{y})")
            if state & 0x002:  # LEFT_UP
                with self._lock:
                    self._events.append(ClickEvent(t=t, x=x, y=y, button="left", action="up"))
                self._overlay.log_event(f"[{t:6.2f}s] 🖱  LEFT  UP    @({x},{y})")
            if state & 0x004:  # RIGHT_DOWN
                with self._lock:
                    self._events.append(ClickEvent(t=t, x=x, y=y, button="right", action="down"))
                self._overlay.log_event(f"[{t:6.2f}s] 🖱  RIGHT DOWN  @({x},{y})")
            if state & 0x008:  # RIGHT_UP
                with self._lock:
                    self._events.append(ClickEvent(t=t, x=x, y=y, button="right", action="up"))
                self._overlay.log_event(f"[{t:6.2f}s] 🖱  RIGHT UP    @({x},{y})")
            if state & 0x010:  # MIDDLE_DOWN
                with self._lock:
                    self._events.append(ClickEvent(t=t, x=x, y=y, button="middle", action="down"))
                self._overlay.log_event(f"[{t:6.2f}s] 🖱  MID  DOWN  @({x},{y})")
            if state & 0x020:  # MIDDLE_UP
                with self._lock:
                    self._events.append(ClickEvent(t=t, x=x, y=y, button="middle", action="up"))
                self._overlay.log_event(f"[{t:6.2f}s] 🖱  MID  UP    @({x},{y})")

            # 滚轮
            if rolling:
                direction = "UP" if rolling > 0 else "DOWN"
                self._overlay.log_event(f"[{t:6.2f}s] 🖱  WHEEL {direction} ({rolling})")

            # 异步发回系统（不阻塞录制循环）
            self._enqueue_send(ctx, device, mouse_stroke)

        except Exception as e:
            self.logger.error("处理鼠标事件异常: %s\n%s", e, traceback.format_exc())

    def _enqueue_send(self, ctx, device, mouse_stroke):
        """把 stroke 复制一份入队，由 _send_loop 异步发回系统。

        必须复制：mouse_stroke 是复用的缓冲区，下一个事件会覆盖它。
        用 bytes 复制最简单（struct 大小固定）。
        """
        try:
            stroke_bytes = ctypes.string_at(
                ctypes.byref(mouse_stroke), ctypes.sizeof(mouse_stroke)
            )
            self._send_queue.put((ctx, device, stroke_bytes))
        except Exception as e:
            self.logger.error("入队发送事件异常: %s", e)

    # ---- 热键监听循环 -------------------------------------------------------
    def _hotkey_listener(self):
        """独立线程监听 F7/F8/F9（不与录制循环共用上下文）。"""
        lib = self._clicker_ref._lib if self._clicker_ref else None
        if lib is None:
            return

        ctx = lib.interception_create_context()
        if not ctx:
            self.logger.error("热键线程：无法创建上下文")
            return

        try:
            # 只监听键盘按下事件（使用 InterceptionCore 预包装的谓词函数）
            lib.interception_set_filter(
                ctx, lib._is_keyboard_pred, lib.INTERCEPTION_FILTER_KEY_DOWN,
            )

            # 用 InterceptionMouseStroke 作为通用缓冲区
            mouse_stroke = InterceptionMouseStroke()
            self.logger.info("热键监听线程启动：F7=开始，F8=停止保存，F9=取消")

            while not self._hotkey_stop.is_set():
                device = lib.interception_wait_with_timeout(ctx, 50)
                if device <= 0:
                    continue

                n = lib.interception_receive(ctx, device, ctypes.byref(mouse_stroke), 1)
                if n <= 0:
                    continue

                ks_ptr = ctypes.cast(ctypes.byref(mouse_stroke), ctypes.POINTER(InterceptionKeyStroke))
                ks = ks_ptr[0]
                scan_code = ks.code & 0xFFFF

                if scan_code == self.HOTKEY_START and not self._recording:
                    self.logger.info("热键 F7：开始录制")
                    self.start()
                elif scan_code == self.HOTKEY_STOP and self._recording:
                    self.logger.info("热键 F8：停止录制")
                    events = self.stop()
                    name = self._prompt_save_dialog()
                    if name:
                        self.save(events, name)
                    else:
                        self.logger.info("用户取消保存")
                elif scan_code == self.HOTKEY_CANCEL:
                    if self._recording:
                        self.logger.info("热键 F9：取消录制")
                        self.stop()
                    self._events.clear()

        finally:
            lib.interception_destroy_context(ctx)

    def _prompt_save_dialog(self) -> Optional[str]:
        try:
            from tkinter import simpledialog
            root = tk.Tk()
            root.withdraw()
            root.attributes("-topmost", True)
            name = simpledialog.askstring("保存录制脚本", "请输入脚本名称（留空取消）:", parent=root)
            root.destroy()
            return (name or "").strip() or None
        except Exception as e:
            self.logger.warning("无法弹出输入对话框: %s", e)
            return None

    def start_by_hotkey(self):
        if self._hotkey_thread and self._hotkey_thread.is_alive():
            return
        self._hotkey_stop.clear()
        self._hotkey_thread = threading.Thread(target=self._hotkey_listener, daemon=True)
        self._hotkey_thread.start()

    def stop_hotkey_listener(self):
        self._hotkey_stop.set()
        if self._hotkey_thread and self._hotkey_thread.is_alive():
            self._hotkey_thread.join(timeout=2.0)
