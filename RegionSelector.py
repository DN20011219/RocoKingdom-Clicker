"""
RegionSelector — 窗口区域录制器（只录矩形，不录任何输入事件）。

提供两种取区域的方式：
  1) select_by_drag()：全屏拖拽圈选，覆盖整个虚拟屏（支持多显示器与负坐标）
  2) pick_window()：3 秒倒计时后拾取光标下的顶层窗口，优先使用 DWM 扩展边界
     （去掉阴影/不可见边框，结果更贴合用户看到的窗口范围）

两个方法都是异步的：立即返回，结果通过 on_done(region | None) 回调。
必须在 Tk 主线程调用（在子线程创建 Tk 窗口会崩溃，见 InputRecorder.DebugOverlay 的注释）。
"""

from __future__ import annotations

import ctypes
import logging
import tkinter as tk
from typing import Callable, Optional

user32 = ctypes.windll.user32
try:
    # 与 InterceptionCore 保持一致：保证窗口矩形取到的是物理像素
    user32.SetProcessDPIAware()
except Exception:
    pass

# GetSystemMetrics 索引
SM_XVIRTUALSCREEN = 76
SM_YVIRTUALSCREEN = 77
SM_CXVIRTUALSCREEN = 78
SM_CYVIRTUALSCREEN = 79

GA_ROOT = 2
DWMWA_EXTENDED_FRAME_BOUNDS = 9

# 圈选结果的最小有效尺寸（低于此值视为误操作）
MIN_REGION_SIZE = 4


class RECT(ctypes.Structure):
    _fields_ = [
        ("left", ctypes.c_long),
        ("top", ctypes.c_long),
        ("right", ctypes.c_long),
        ("bottom", ctypes.c_long),
    ]


class POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


def get_cursor_pos() -> tuple[int, int]:
    """读取当前光标位置（虚拟屏绝对坐标）。"""
    point = POINT()
    try:
        if user32.GetCursorPos(ctypes.byref(point)):
            return int(point.x), int(point.y)
    except Exception:
        pass
    return 0, 0


def get_virtual_screen() -> tuple[int, int, int, int]:
    """返回覆盖全部显示器的虚拟屏 (x, y, width, height)。"""
    try:
        x = int(user32.GetSystemMetrics(SM_XVIRTUALSCREEN))
        y = int(user32.GetSystemMetrics(SM_YVIRTUALSCREEN))
        w = int(user32.GetSystemMetrics(SM_CXVIRTUALSCREEN))
        h = int(user32.GetSystemMetrics(SM_CYVIRTUALSCREEN))
        if w > 0 and h > 0:
            return x, y, w, h
    except Exception:
        pass
    # 回退到主屏
    return 0, 0, int(user32.GetSystemMetrics(0)), int(user32.GetSystemMetrics(1))


def make_region(x: int, y: int, width: int, height: int, **extra) -> dict:
    """构造标准区域字典。"""
    region = {"x": int(x), "y": int(y), "width": int(width), "height": int(height)}
    region.update(extra)
    return region


def normalize_region(x0: int, y0: int, x1: int, y1: int) -> Optional[dict]:
    """把两个对角点归一化为区域字典，尺寸过小返回 None。"""
    left, right = min(x0, x1), max(x0, x1)
    top, bottom = min(y0, y1), max(y0, y1)
    width, height = right - left, bottom - top
    if width < MIN_REGION_SIZE or height < MIN_REGION_SIZE:
        return None
    return make_region(left, top, width, height)


def format_region(region: Optional[dict]) -> str:
    """区域的可读表示：800x600 @ (100,200)。"""
    if not region:
        return "未设置"
    try:
        return "{}x{} @ ({},{})".format(
            int(region.get("width", 0)), int(region.get("height", 0)),
            int(region.get("x", 0)), int(region.get("y", 0)),
        )
    except (TypeError, ValueError):
        return "未设置"


def capture_window_under_cursor() -> Optional[dict]:
    """拾取当前光标下的顶层窗口矩形（优先 DWM 扩展边界）。"""
    point = POINT()
    if not user32.GetCursorPos(ctypes.byref(point)):
        return None

    hwnd = None
    try:
        user32.WindowFromPoint.restype = ctypes.c_void_p
        user32.WindowFromPoint.argtypes = [POINT]
        hwnd = user32.WindowFromPoint(point)
    except Exception:
        hwnd = None
    if not hwnd:
        # 兜底：取前台窗口
        try:
            user32.GetForegroundWindow.restype = ctypes.c_void_p
            hwnd = user32.GetForegroundWindow()
        except Exception:
            hwnd = None
    if not hwnd:
        return None

    try:
        user32.GetAncestor.restype = ctypes.c_void_p
        user32.GetAncestor.argtypes = [ctypes.c_void_p, ctypes.c_uint]
        root_hwnd = user32.GetAncestor(hwnd, GA_ROOT) or hwnd
    except Exception:
        root_hwnd = hwnd

    rect = RECT()
    captured = False
    try:
        dwm = ctypes.windll.dwmapi
        dwm.DwmGetWindowAttribute.restype = ctypes.c_long
        dwm.DwmGetWindowAttribute.argtypes = [
            ctypes.c_void_p, ctypes.c_uint, ctypes.c_void_p, ctypes.c_uint,
        ]
        result = dwm.DwmGetWindowAttribute(
            root_hwnd, DWMWA_EXTENDED_FRAME_BOUNDS,
            ctypes.byref(rect), ctypes.sizeof(rect),
        )
        captured = (result == 0)
    except Exception:
        captured = False
    if not captured:
        try:
            user32.GetWindowRect.argtypes = [ctypes.c_void_p, ctypes.POINTER(RECT)]
            if not user32.GetWindowRect(root_hwnd, ctypes.byref(rect)):
                return None
        except Exception:
            return None

    width = int(rect.right - rect.left)
    height = int(rect.bottom - rect.top)
    if width < MIN_REGION_SIZE or height < MIN_REGION_SIZE:
        return None

    return make_region(int(rect.left), int(rect.top), width, height,
                       source="window", title=_window_title(root_hwnd))


def _window_title(hwnd) -> str:
    """读取窗口标题（失败返回空串）。"""
    try:
        length = user32.GetWindowTextLengthW(hwnd)
        if length <= 0:
            return ""
        buffer = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buffer, length + 1)
        return buffer.value or ""
    except Exception:
        return ""


class RegionSelector:
    """区域录制器：拖拽圈选 / 窗口拾取，结果为 {x, y, width, height}。"""

    BG = "#0f172a"
    FG = "#e2e8f0"
    ACCENT = "#22d3ee"

    def __init__(self, root: tk.Misc):
        self.root = root
        self.logger = logging.getLogger("region_selector")
        self._busy = False

    # ---- 对外 API ----

    @property
    def busy(self) -> bool:
        return self._busy

    def select_by_drag(self, on_done: Optional[Callable[[Optional[dict]], None]] = None,
                       title: str = "拖拽圈选目标区域") -> None:
        """全屏拖拽圈选。完成后调用 on_done(region | None)。"""
        if self._busy:
            self.logger.warning("区域录制已在进行中，忽略本次请求")
            return
        self._busy = True

        vx, vy, vw, vh = get_virtual_screen()
        overlay = tk.Toplevel(self.root)
        overlay.overrideredirect(True)
        overlay.configure(bg="black", cursor="cross")
        overlay.geometry(f"{vw}x{vh}+{vx}+{vy}")
        try:
            overlay.attributes("-topmost", True)
            overlay.attributes("-alpha", 0.3)
        except tk.TclError:
            pass

        canvas = tk.Canvas(overlay, bg="black", highlightthickness=0, bd=0)
        canvas.pack(fill="both", expand=True)

        # 独立的提示/尺寸浮窗（不透明，保证文字可读）
        info = tk.Toplevel(self.root)
        info.overrideredirect(True)
        info.configure(bg=self.BG)
        try:
            info.attributes("-topmost", True)
        except tk.TclError:
            pass
        info_text = tk.StringVar(value=f"{title} · Esc/右键取消")
        tk.Label(info, textvariable=info_text, font=("Segoe UI", 11, "bold"),
                 fg=self.FG, bg=self.BG, padx=14, pady=8, justify="left").pack()

        state = {"press": None, "rect": None, "done": False}

        def cleanup():
            state["done"] = True
            for win in (info, overlay):
                try:
                    win.grab_release()
                except Exception:
                    pass
                try:
                    win.destroy()
                except Exception:
                    pass
            self._busy = False

        def emit(region: Optional[dict]):
            if state["done"]:
                return
            cleanup()
            if on_done:
                try:
                    on_done(region)
                except Exception as e:
                    self.logger.error("区域录制回调异常: %s", e)

        def place_info():
            """把提示浮窗放到光标右下方，避免遮住选框。"""
            try:
                px, py = self.root.winfo_pointerxy()
                info.update_idletasks()
                w = info.winfo_width() or 200
                h = info.winfo_height() or 40
                screen_w = user32.GetSystemMetrics(0)
                screen_h = user32.GetSystemMetrics(1)
                left = px + 18
                top = py + 18
                if left + w > screen_w:
                    left = max(0, px - w - 18)
                if top + h > screen_h:
                    top = max(0, py - h - 18)
                info.geometry(f"+{left}+{top}")
            except Exception:
                pass

        def local(abs_x: int, abs_y: int) -> tuple[int, int]:
            return abs_x - vx, abs_y - vy

        def on_press(_event):
            px, py = overlay.winfo_pointerxy()
            state["press"] = (px, py)
            lx, ly = local(px, py)
            state["rect"] = canvas.create_rectangle(
                lx, ly, lx, ly, outline=self.ACCENT, width=2, dash=(6, 3))

        def on_motion(_event):
            px, py = overlay.winfo_pointerxy()
            if state["press"] is None:
                info_text.set(f"{title} · 当前 ({px},{py}) · Esc/右键取消")
                place_info()
                return
            ax, ay = state["press"]
            region = normalize_region(min(ax, px), min(ay, py), max(ax, px), max(ay, py))
            if state["rect"] is not None:
                lx0, ly0 = local(min(ax, px), min(ay, py))
                lx1, ly1 = local(max(ax, px), max(ay, py))
                canvas.coords(state["rect"], lx0, ly0, lx1, ly1)
            info_text.set(format_region(region) if region else "继续拖拽…")
            place_info()

        def on_release(_event):
            if state["press"] is None:
                return
            ax, ay = state["press"]
            px, py = overlay.winfo_pointerxy()
            region = normalize_region(min(ax, px), min(ay, py), max(ax, px), max(ay, py))
            emit(region)

        def on_cancel(_event=None):
            emit(None)

        canvas.bind("<ButtonPress-1>", on_press)
        canvas.bind("<B1-Motion>", on_motion)
        canvas.bind("<ButtonRelease-1>", on_release)
        for target in (overlay, canvas):
            target.bind("<Escape>", on_cancel)
            target.bind("<Button-3>", on_cancel)

        try:
            overlay.focus_force()
            overlay.grab_set()
        except tk.TclError:
            pass
        place_info()
        self.logger.info("区域圈选已启动：虚拟屏 %dx%d @ (%d,%d)", vw, vh, vx, vy)

    def pick_window(self, delay: float = 3.0,
                    on_done: Optional[Callable[[Optional[dict]], None]] = None) -> None:
        """倒计时后拾取光标下的窗口。完成后调用 on_done(region | None)。"""
        if self._busy:
            self.logger.warning("区域录制已在进行中，忽略本次请求")
            return
        self._busy = True

        box = tk.Toplevel(self.root)
        box.overrideredirect(True)
        box.configure(bg=self.BG)
        try:
            box.attributes("-topmost", True)
        except tk.TclError:
            pass
        label = tk.Label(box, text="", font=("Segoe UI", 13, "bold"),
                         fg=self.FG, bg=self.BG, padx=20, pady=14, justify="center")
        label.pack()

        # 放到主屏顶部居中，尽量不遮挡用户想拾取的窗口
        try:
            box.update_idletasks()
            bw = box.winfo_reqwidth() or 320
            screen_w = user32.GetSystemMetrics(0)
            box.geometry(f"+{max(0, (screen_w - bw) // 2)}+12")
        except Exception:
            pass

        state = {"remain": max(1, int(round(delay))), "done": False}

        def cleanup():
            state["done"] = True
            try:
                box.destroy()
            except Exception:
                pass
            self._busy = False

        def emit(region: Optional[dict]):
            if state["done"]:
                return
            cleanup()
            if on_done:
                try:
                    on_done(region)
                except Exception as e:
                    self.logger.error("窗口拾取回调异常: %s", e)

        def tick():
            if state["done"]:
                return
            if state["remain"] > 0:
                label.config(text="把鼠标移到目标窗口上…\n{} 秒后拾取（Esc 取消）".format(state["remain"]))
                state["remain"] -= 1
                try:
                    box.lift()
                except Exception:
                    pass
                self.root.after(1000, tick)
                return
            emit(capture_window_under_cursor())

        box.bind("<Escape>", lambda _e: emit(None))
        try:
            box.focus_force()
        except tk.TclError:
            pass
        self.logger.info("窗口拾取已启动：%.1f 秒倒计时", delay)
        tick()
