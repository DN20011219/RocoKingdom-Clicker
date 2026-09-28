from __future__ import annotations

import json
import threading
import tkinter as tk
from tkinter import ttk, messagebox, simpledialog, filedialog
import queue

from RegionSelector import RegionSelector, format_region
from MidiScore import normalize_key_label, note_name, parse_note_name


# ──────────────────────────────────────────────
# Floating toast notification (top-left corner)
# ──────────────────────────────────────────────
class ToastWindow:
    """Borderless toast that appears at screen top-left and auto-dismisses."""

    _fade_steps = 6
    _fade_ms = 25

    def __init__(self, root: tk.Tk, text: str, duration: float = 3.0,
                 bg: str = "#1e293b", fg: str = "#f1f5f9",
                 accent: str = "#3b82f6",
                 font: tuple = ("Segoe UI", 11, "bold")):
        self.duration = int(duration * 1000)
        self.win = tk.Toplevel(root)
        self.win.overrideredirect(True)
        self.win.attributes("-topmost", True)
        self.win.configure(bg=bg)
        self.win.resizable(False, False)
        try:
            self.win.attributes("-alpha", 0.0)
        except Exception:
            pass

        # 左侧彩色竖条 + 文本
        bar = tk.Frame(self.win, bg=accent, width=4)
        bar.pack(side="left", fill="y")
        label = tk.Label(self.win, text=text, bg=bg, fg=fg, font=font,
                         justify="left", anchor="w", padx=16, pady=12)
        label.pack(side="left", fill="both", expand=True)

        sx = root.winfo_screenwidth()
        sy = root.winfo_screenheight()
        self.win.update_idletasks()
        w = min(label.winfo_reqwidth() + 40, sx - 40)
        h = label.winfo_reqheight() + 24
        self.win.geometry(f"{w}x{h}+24+24")
        self._fade_in()

    def _fade_in(self, step: int = 0):
        if step > self._fade_steps:
            self.win.after(self.duration, self._fade_out)
            return
        alpha = round(step / self._fade_steps * 0.9, 2)
        try:
            self.win.attributes("-alpha", alpha)
        except Exception:
            pass
        self.win.after(self._fade_ms, lambda: self._fade_in(step + 1))

    def _fade_out(self, step: int = 0):
        if step > self._fade_steps:
            self.win.destroy()
            return
        alpha = round(0.9 * (1 - step / self._fade_steps), 2)
        try:
            self.win.attributes("-alpha", alpha)
        except Exception:
            pass
        self.win.after(self._fade_ms, lambda: self._fade_out(step + 1))


# ──────────────────────────────────────────────
# Toast queue — backend pushes, GUI pops
# ──────────────────────────────────────────────
_toast_queue: queue.Queue = queue.Queue()


def push_toast(text: str, duration: float = 3.0):
    _toast_queue.put((text, duration))


_window_config = None


class BindingEditorDialog:
    """「音高 → 按键」绑定编辑对话框（第五栏「编辑键位」）。

    对话框只负责编辑与校验，落盘通过 on_save 回调交回 _DesktopWindow，
    这样它不直接依赖 js_api，配色也复用主窗口的那一套主题常量。
    """

    def __init__(self, owner, bindings: list, on_save):
        self.owner = owner
        self.on_save = on_save
        self._rows: list[dict] = []

        self.win = tk.Toplevel(owner.root)
        self.win.title("编辑键位绑定")
        self.win.configure(bg=owner.BG)
        self.win.geometry("460x600")
        self.win.minsize(420, 420)
        self.win.transient(owner.root)

        tk.Label(self.win, text="把每个音绑定到一个键盘按键",
                 font=("Segoe UI", 12, "bold"), fg=owner.TEXT,
                 bg=owner.BG).pack(anchor="w", padx=14, pady=(12, 2))
        tk.Label(self.win,
                 text="音名支持 C4 / C#4 / Db4 这类写法，也可以直接填 MIDI 音高数字"
                      "（中央 C = 60）。按键只能是字母、数字、F1-F12 或标点。",
                 font=("Segoe UI", 8), fg=owner.TEXT_MUTED, bg=owner.BG,
                 wraplength=420, justify="left").pack(anchor="w", padx=14, pady=(0, 8))

        self._build_table()
        self._build_footer()

        for item in bindings or ():
            try:
                self._append_row(int(item.get("pitch")), str(item.get("key", "")),
                                 str(item.get("note") or ""))
            except (AttributeError, TypeError, ValueError):
                continue

        # 主窗口被最小化时 grab_set 会抛 "window not viewable"，
        # 抢不到焦点不影响编辑，不能让它把对话框整个搞崩
        try:
            self.win.grab_set()
        except Exception:
            pass
        self.win.protocol("WM_DELETE_WINDOW", self._on_cancel)

    # ---- 布局 ----

    def _build_table(self):
        owner = self.owner
        wrap = tk.Frame(self.win, bg=owner.BG)
        wrap.pack(fill="both", expand=True, padx=14, pady=(0, 8))

        canvas = tk.Canvas(wrap, bg=owner.BG, highlightthickness=0, bd=0)
        vsb = ttk.Scrollbar(wrap, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)

        self._body = tk.Frame(canvas, bg=owner.BG)
        self._body_id = canvas.create_window((0, 0), window=self._body, anchor="nw")
        self._body.bind("<Configure>",
                        lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>",
                    lambda e: canvas.itemconfigure(self._body_id, width=e.width))

        def _on_wheel(event):
            canvas.yview_scroll(int(-event.delta / 120), "units")
        canvas.bind("<Enter>", lambda e: canvas.bind_all("<MouseWheel>", _on_wheel))
        canvas.bind("<Leave>", lambda e: canvas.unbind_all("<MouseWheel>"))

        for col, text in enumerate(("音名", "MIDI", "按键", "")):
            tk.Label(self._body, text=text, font=("Segoe UI", 8, "bold"),
                     fg=owner.TEXT_MUTED, bg=owner.BG, anchor="w"
                     ).grid(row=0, column=col, sticky="w", padx=(0, 8), pady=(0, 4))
        self._body.columnconfigure(0, weight=2)
        self._body.columnconfigure(1, weight=1)
        self._body.columnconfigure(2, weight=2)

    def _build_footer(self):
        owner = self.owner
        add_frame = tk.Frame(self.win, bg=owner.PANEL_BG, padx=10, pady=8)
        add_frame.pack(fill="x", padx=14, pady=(0, 8))

        tk.Label(add_frame, text="音名/MIDI", font=("Segoe UI", 9),
                 fg=owner.TEXT, bg=owner.PANEL_BG).grid(row=0, column=0, sticky="w")
        self._new_pitch = tk.StringVar(value="")
        tk.Entry(add_frame, textvariable=self._new_pitch, width=10,
                 font=("Segoe UI", 9, "bold"), bg=owner.CARD_BG, fg=owner.TEXT,
                 insertbackground=owner.TEXT, relief="flat", bd=0
                 ).grid(row=0, column=1, sticky="w", padx=(6, 12))

        tk.Label(add_frame, text="按键", font=("Segoe UI", 9),
                 fg=owner.TEXT, bg=owner.PANEL_BG).grid(row=0, column=2, sticky="w")
        self._new_key = tk.StringVar(value="")
        tk.Entry(add_frame, textvariable=self._new_key, width=6,
                 font=("Segoe UI", 9, "bold"), bg=owner.CARD_BG, fg=owner.TEXT,
                 insertbackground=owner.TEXT, relief="flat", bd=0
                 ).grid(row=0, column=3, sticky="w", padx=(6, 12))

        ttk.Button(add_frame, text="➕ 添加", command=self._on_add).grid(row=0, column=4)

        buttons = tk.Frame(self.win, bg=owner.BG)
        buttons.pack(fill="x", padx=14, pady=(0, 12))
        ttk.Button(buttons, text="↺ 恢复默认",
                   command=self._on_restore_defaults).pack(side="left", expand=True,
                                                           fill="x", padx=(0, 4))
        ttk.Button(buttons, text="取消",
                   command=self._on_cancel).pack(side="left", expand=True,
                                                 fill="x", padx=4)
        ttk.Button(buttons, text="💾 保存", style="Primary.TButton",
                   command=self._on_save).pack(side="left", expand=True,
                                               fill="x", padx=(4, 0))

    # ---- 行操作 ----

    def _append_row(self, pitch: int, key: str, note: str = "") -> None:
        owner = self.owner
        row_index = len(self._rows) + 1
        frame = tk.Frame(self._body, bg=owner.BG)
        frame.grid(row=row_index, column=0, columnspan=4, sticky="ew", pady=1)

        key_var = tk.StringVar(value=key)
        tk.Label(frame, text=note_name(pitch), font=("Segoe UI", 9),
                 fg=owner.TEXT, bg=owner.BG, width=6, anchor="w").pack(side="left")
        tk.Label(frame, text=str(pitch), font=("Segoe UI", 9),
                 fg=owner.TEXT_MUTED, bg=owner.BG, width=5, anchor="w").pack(side="left",
                                                                            padx=(0, 8))
        entry = tk.Entry(frame, textvariable=key_var, width=6,
                         font=("Segoe UI", 9, "bold"), bg=owner.CARD_BG,
                         fg=owner.TEXT, insertbackground=owner.TEXT,
                         relief="flat", bd=0, justify="center")
        entry.pack(side="left")
        ttk.Button(frame, text="🗑", width=3,
                   command=lambda: self._remove_row(pitch)).pack(side="left", padx=(8, 0))

        self._rows.append({"pitch": pitch, "key_var": key_var,
                           "note": note, "frame": frame, "entry": entry})

    def _remove_row(self, pitch: int) -> None:
        for row in self._rows:
            if row["pitch"] == pitch:
                row["frame"].destroy()
                self._rows.remove(row)
                break
        # grid 行号是按插入顺序排的，删掉一行后必须重排，否则会留下空行
        for index, row in enumerate(self._rows, start=1):
            row["frame"].grid_configure(row=index)

    def _on_add(self) -> None:
        pitch = parse_note_name(self._new_pitch.get())
        if pitch is None:
            messagebox.showwarning("音名无法识别",
                                   "请填写 C4 / C#4 / Db4 这样的音名，或 0~127 的 MIDI 音高数字。",
                                   parent=self.win)
            return
        if normalize_key_label(self._new_key.get()) is None:
            messagebox.showwarning("按键无法识别",
                                   "按键只能是字母、数字、F1-F12、SPACE 或标点符号。",
                                   parent=self.win)
            return
        if any(row["pitch"] == pitch for row in self._rows):
            self._remove_row(pitch)
        self._append_row(pitch, normalize_key_label(self._new_key.get()))
        self._new_pitch.set("")
        self._new_key.set("")

    def _on_restore_defaults(self) -> None:
        from MidiScore import LAYOUT_ROCO_HANDPAN
        if not messagebox.askyesno("恢复默认", "确定恢复成默认的洛克手碟九键吗？",
                                   parent=self.win):
            return
        for row in list(self._rows):
            row["frame"].destroy()
        self._rows.clear()
        for item in LAYOUT_ROCO_HANDPAN:
            self._append_row(int(item["pitch"]), str(item["key"]), str(item.get("note") or ""))

    # ---- 保存 / 取消 ----

    def _on_save(self) -> None:
        bindings: list[dict] = []
        seen_keys: dict[str, int] = {}
        for row in self._rows:
            raw = row["key_var"].get()
            key = normalize_key_label(raw)
            if key is None:
                row["entry"].configure(highlightthickness=1,
                                       highlightbackground=self.owner.DANGER)
                messagebox.showwarning(
                    "按键无法识别",
                    f"{note_name(row['pitch'])} 绑定的按键「{raw or '（空）'}」不合法。\n"
                    "按键只能是字母、数字、F1-F12、SPACE 或标点符号。",
                    parent=self.win)
                return
            if key in seen_keys:
                messagebox.showwarning(
                    "按键冲突",
                    f"按键 {key} 同时绑给了 {note_name(seen_keys[key])} 和 "
                    f"{note_name(row['pitch'])}，实际只会发出音高较低的那个。",
                    parent=self.win)
                return
            seen_keys[key] = row["pitch"]
            item = {"pitch": row["pitch"], "key": key}
            if row.get("note"):
                item["note"] = row["note"]
            bindings.append(item)

        if not bindings:
            messagebox.showwarning("键位为空",
                                   "至少需要绑定一个音，否则任何曲谱都无法演奏。",
                                   parent=self.win)
            return

        bindings.sort(key=lambda b: b["pitch"])
        self.win.destroy()
        self.on_save(bindings)

    def _on_cancel(self) -> None:
        self.win.destroy()


class _DesktopWindow:
    # ── 配色方案（现代深蓝/靛蓝主题） ──
    BG          = "#0f172a"  # 主背景（深蓝灰）
    PANEL_BG    = "#1e293b"  # 面板背景
    CARD_BG     = "#334155"  # 卡片/输入框背景
    ACCENT      = "#3b82f6"  # 主强调色（蓝）
    ACCENT_HOVER= "#2563eb"  # 强调色悬停
    SUCCESS     = "#22c55e"  # 成功/运行中（绿）
    DANGER      = "#ef4444"  # 危险/录制（红）
    WARNING     = "#f59e0b"  # 警告（橙）
    TEXT        = "#f1f5f9"  # 主文字
    TEXT_MUTED  = "#94a3b8"  # 次要文字
    BORDER      = "#475569"  # 边框
    SELECT_BG   = "#1d4ed8"  # 选中背景

    def __init__(self, title: str, js_api, width: int, height: int):
        self.title = title
        self.js_api = js_api
        self.width = width
        self.height = height
        self.root = tk.Tk()
        self.root.title(title)
        self.root.geometry(f"{width}x{height}")
        self.root.minsize(2460, 900)
        self.root.configure(bg=self.BG)

        self.status_var = tk.StringVar(value="准备就绪")
        self.move_mouse_var = tk.BooleanVar(value=False)

        # 显示名 → (stem, type) 映射，避免靠字符串分割还原脚本名
        # type: "action" 或 "recorded"
        self._script_map: dict[str, tuple[str, str]] = {}
        # 抑制列表选择事件（刷新时避免触发切换确认弹窗）
        self._suppress_select_event: bool = False

        self._build_ui()
        self.refresh_all()

    def _build_ui(self):
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except Exception:
            pass

        # ── 全局样式 ──
        style.configure(".", background=self.BG, foreground=self.TEXT,
                        font=("Segoe UI", 10))
        style.configure("TFrame", background=self.BG)
        style.configure("TLabel", background=self.BG, foreground=self.TEXT)
        style.configure("TLabelframe", background=self.BG, foreground=self.TEXT,
                        borderwidth=1, relief="solid")
        style.configure("TLabelframe.Label", background=self.BG,
                        foreground=self.ACCENT, font=("Segoe UI", 11, "bold"))
        style.configure("TButton", background=self.CARD_BG, foreground=self.TEXT,
                        borderwidth=0, padding=(10, 8), font=("Segoe UI", 10))
        style.map("TButton",
                  background=[("active", self.BORDER), ("disabled", "#2d3a4f")],
                  foreground=[("disabled", "#64748b")])
        style.configure("Primary.TButton", background=self.ACCENT,
                        foreground="#ffffff", font=("Segoe UI", 10, "bold"),
                        padding=(12, 10))
        style.map("Primary.TButton",
                  background=[("active", self.ACCENT_HOVER),
                              ("disabled", "#1e3a5f")])
        style.configure("Danger.TButton", background=self.DANGER,
                        foreground="#ffffff", font=("Segoe UI", 10, "bold"),
                        padding=(10, 8))
        style.map("Danger.TButton",
                  background=[("active", "#dc2626"), ("disabled", "#7f1d1d")])
        style.configure("Success.TButton", background=self.SUCCESS,
                        foreground="#ffffff", font=("Segoe UI", 10, "bold"),
                        padding=(10, 8))
        style.map("Success.TButton",
                  background=[("active", "#16a34a"), ("disabled", "#14532d")])
        style.configure("Warning.TButton", background=self.WARNING,
                        foreground="#ffffff", font=("Segoe UI", 10, "bold"),
                        padding=(10, 8))
        style.map("Warning.TButton",
                  background=[("active", "#d97706"), ("disabled", "#78350f")])
        style.configure("Secondary.TButton", background=self.BORDER,
                        foreground="#ffffff", font=("Segoe UI", 10, "bold"),
                        padding=(10, 8))
        style.map("Secondary.TButton",
                  background=[("active", self.TEXT_MUTED), ("disabled", "#2d3a4f")])
        style.configure("TEntry", fieldbackground=self.CARD_BG,
                        foreground=self.TEXT, insertcolor=self.TEXT,
                        borderwidth=0, padding=8)
        style.configure("TCheckbutton", background=self.BG, foreground=self.TEXT,
                        indicatorbackground=self.CARD_BG,
                        indicatorforeground=self.ACCENT)
        style.map("TCheckbutton",
                  background=[("active", self.BG)])
        style.configure("TProgressbar", background=self.ACCENT,
                        troughcolor=self.CARD_BG, borderwidth=0)
        style.configure("TSeparator", background=self.BORDER)
        style.configure("Vertical.TScrollbar", background=self.CARD_BG,
                        troughcolor=self.PANEL_BG, borderwidth=0,
                        arrowcolor=self.TEXT)
        style.configure("Header.TLabel", font=("Segoe UI", 20, "bold"),
                        foreground=self.TEXT, background=self.BG)
        style.configure("Sub.TLabel", font=("Segoe UI", 9),
                        foreground=self.TEXT_MUTED, background=self.BG)
        style.configure("Card.TLabel", background=self.CARD_BG,
                        foreground=self.TEXT)
        style.configure("Card.TFrame", background=self.CARD_BG)
        # Combobox 深色主题样式
        style.configure("TCombobox",
                        fieldbackground=self.CARD_BG,
                        background=self.CARD_BG,
                        foreground=self.TEXT,
                        arrowcolor=self.TEXT,
                        borderwidth=1,
                        relief="solid",
                        padding=(6, 4))
        style.map("TCombobox",
                  fieldbackground=[("readonly", self.CARD_BG),
                                   ("disabled", "#2d3a4f")],
                  foreground=[("readonly", self.TEXT),
                              ("disabled", "#64748b")],
                  arrowcolor=[("disabled", "#64748b")],
                  bordercolor=[("focus", self.ACCENT),
                               ("!focus", self.BORDER)])

        header = ttk.Frame(self.root, padding=(22, 18, 22, 8))
        header.pack(fill="x")
        ttk.Label(header, text="RocoKingdom Clicker",
                  style="Header.TLabel").pack(anchor="w")
        ttk.Label(header,
                  text="基于 Interception 驱动的输入录制与回放工具",
                  style="Sub.TLabel").pack(anchor="w", pady=(2, 0))

        body = ttk.Frame(self.root, padding=(22, 8, 22, 18))
        body.pack(fill="both", expand=True)
        body.columnconfigure(0, weight=3)
        body.columnconfigure(1, weight=5)
        body.columnconfigure(2, weight=3)
        body.columnconfigure(3, weight=3)
        body.columnconfigure(4, weight=3)
        body.rowconfigure(0, weight=1)

        # ── 左栏：状态与控制（内含三组） ─────────────────────────
        left = ttk.Frame(body)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 12))

        # ---- 组1：状态信息（只读） ----
        info_frame = ttk.LabelFrame(left, text="  状态信息  ", padding=12)
        info_frame.pack(fill="x", pady=(0, 12))

        ttk.Label(info_frame, text="当前选择脚本",
                  font=("Segoe UI", 9, "bold"),
                  foreground=self.TEXT_MUTED).pack(anchor="w", pady=(0, 6))
        self.selected_script_var = tk.StringVar(value="(未选择)")
        sel_frame = tk.Frame(info_frame, bg=self.CARD_BG, bd=0,
                             highlightbackground=self.ACCENT,
                             highlightthickness=2)
        sel_frame.pack(fill="x", pady=(0, 12))
        tk.Label(sel_frame, textvariable=self.selected_script_var,
                 font=("Segoe UI", 13, "bold"), foreground=self.TEXT,
                 bg=self.CARD_BG, padx=14, pady=10, anchor="w",
                 justify="left").pack(fill="x")

        ttk.Label(info_frame, text="状态",
                  font=("Segoe UI", 9, "bold"),
                  foreground=self.TEXT_MUTED).pack(anchor="w", pady=(0, 6))
        self.status_label = tk.Label(
            info_frame, textvariable=self.status_var,
            font=("Segoe UI", 12, "bold"), foreground=self.SUCCESS,
            bg=self.BG, anchor="w",
        )
        self.status_label.pack(anchor="w")

        # ---- 组2：全局控制选项 ----
        ctrl_frame = ttk.LabelFrame(left, text="  全局控制选项  ", padding=12)
        ctrl_frame.pack(fill="x", pady=(0, 12))
        self.move_mouse_check = tk.Checkbutton(
            ctrl_frame,
            text="控制自定义脚本鼠标移动（不影响录制脚本）",
            variable=self.move_mouse_var, command=self.on_toggle_move_mouse,
            wraplength=280, justify="left",
            bg=self.BG, fg=self.TEXT, selectcolor=self.CARD_BG,
            activebackground=self.BG, activeforeground=self.TEXT,
            highlightthickness=0, bd=0,
        )
        self.move_mouse_check.pack(anchor="w")

        # ---- 组3：输入录制 ----
        rec_frame = ttk.LabelFrame(left, text="  输入录制  ", padding=12)
        rec_frame.pack(fill="x")

        rec_header = tk.Frame(rec_frame, bg=self.BG)
        rec_header.pack(fill="x", pady=(0, 8))
        self.recording_indicator = tk.Label(
            rec_header, text="", foreground=self.DANGER,
            font=("Segoe UI", 9, "bold"), bg=self.BG,
        )
        self.recording_indicator.pack(side="right")

        self.recording_name_var = tk.StringVar(value="")
        self.recording_entry = ttk.Entry(rec_frame, textvariable=self.recording_name_var,
                                         font=("Segoe UI", 10))
        self.recording_entry.insert(0, "recording")
        self.recording_entry.pack(fill="x", pady=(0, 4))
        ttk.Label(rec_frame, text="录制脚本名（可编辑）",
                  foreground=self.TEXT_MUTED,
                  font=("Segoe UI", 8)).pack(anchor="w", pady=(0, 8))

        rec_btns = ttk.Frame(rec_frame)
        rec_btns.pack(fill="x")
        rec_btns.columnconfigure(0, weight=1)
        rec_btns.columnconfigure(1, weight=1)
        rec_btns.columnconfigure(2, weight=1)
        self.btn_rec_start = ttk.Button(rec_btns, text="● 录制",
                                        style="Danger.TButton",
                                        command=self.on_rec_start)
        self.btn_rec_start.grid(row=0, column=0, sticky="ew", padx=(0, 4))
        self.btn_rec_stop = ttk.Button(rec_btns, text="■ 保存",
                                       style="Success.TButton",
                                       command=self.on_rec_stop, state="disabled")
        self.btn_rec_stop.grid(row=0, column=1, sticky="ew", padx=4)
        self.btn_rec_cancel = ttk.Button(rec_btns, text="✕ 取消",
                                         style="Secondary.TButton",
                                         command=self.on_rec_cancel, state="disabled")
        self.btn_rec_cancel.grid(row=0, column=2, sticky="ew", padx=(4, 0))

        # 锚点模式选择
        anchor_mode_frame = tk.Frame(rec_frame, bg=self.BG)
        anchor_mode_frame.pack(fill="x", pady=(10, 0))
        ttk.Label(anchor_mode_frame, text="锚点模式",
                  font=("Segoe UI", 9, "bold"),
                  foreground=self.TEXT_MUTED).pack(anchor="w", pady=(0, 4))
        self.anchor_mode_var = tk.StringVar(value="point")
        self.anchor_mode_combo = ttk.Combobox(
            anchor_mode_frame,
            textvariable=self.anchor_mode_var,
            values=["point", "segment"],
            state="readonly",
            width=20,
            font=("Segoe UI", 10),
        )
        self.anchor_mode_combo.pack(fill="x")
        self.anchor_mode_combo.bind("<<ComboboxSelected>>", self._on_anchor_mode_change)
        # 锚点模式说明标签
        self.anchor_mode_desc_var = tk.StringVar(value="")
        ttk.Label(anchor_mode_frame, textvariable=self.anchor_mode_desc_var,
                  foreground=self.TEXT_MUTED,
                  font=("Segoe UI", 8), wraplength=320).pack(anchor="w", pady=(4, 0))
        # 初始化锚点模式
        self._init_anchor_mode()

        # ── 中栏：脚本列表（统一） ─────────────────
        center = ttk.LabelFrame(body, text="  脚本列表（双击执行）  ", padding=16)
        center.grid(row=0, column=1, sticky="nsew", padx=(0, 12))
        center.rowconfigure(0, weight=1)
        center.rowconfigure(2, weight=0)
        center.rowconfigure(3, weight=0)
        center.rowconfigure(4, weight=0)
        center.columnconfigure(0, weight=1)

        # 脚本列表
        list_frame = tk.Frame(center, bg=self.BG)
        list_frame.grid(row=0, column=0, sticky="nsew")

        scroll_y = ttk.Scrollbar(list_frame, orient="vertical")
        scroll_y.pack(side="right", fill="y")

        self.script_list = tk.Listbox(
            list_frame, activestyle="none",
            font=("Segoe UI", 11),
            bg=self.CARD_BG, fg=self.TEXT,
            selectbackground=self.SELECT_BG, selectforeground="#ffffff",
            highlightthickness=0, bd=0, relief="flat",
        )
        self.script_list.pack(side="left", fill="both", expand=True)
        self.script_list.configure(yscrollcommand=scroll_y.set)
        scroll_y.configure(command=self.script_list.yview)
        self.script_list.bind("<<ListboxSelect>>", self._on_list_select)
        self.script_list.bind("<Double-Button-1>", self._on_double_click)

        # 进度条（回放时显示）
        prog_frame = tk.Frame(center, bg=self.BG)
        prog_frame.grid(row=1, column=0, sticky="ew", pady=(8, 0))
        self.progress_var = tk.DoubleVar(value=0)
        self.progress_bar = ttk.Progressbar(
            prog_frame, variable=self.progress_var,
            maximum=100, mode="determinate",
        )
        self.progress_bar.pack(fill="x")
        self.progress_label = tk.Label(
            prog_frame, text="", font=("Segoe UI", 9),
            foreground=self.TEXT_MUTED, anchor="w", bg=self.BG,
        )
        self.progress_label.pack(fill="x")

        # ── 循环回放选项 ──────────────────────────────────────────
        loop_frame = ttk.LabelFrame(center, text="  循环回放  ", padding=10)
        loop_frame.grid(row=2, column=0, sticky="ew", pady=(8, 0))

        # 第一行：循环次数 + 无限循环复选框
        loop_row1 = tk.Frame(loop_frame, bg=self.BG)
        loop_row1.pack(fill="x", pady=(0, 6))

        tk.Label(loop_row1, text="循环次数:", font=("Segoe UI", 9),
                 fg=self.TEXT, bg=self.BG).pack(side="left")
        self.loop_count_var = tk.StringVar(value="1")
        self.loop_count_spin = tk.Spinbox(
            loop_row1, from_=0, to=99999, width=7,
            textvariable=self.loop_count_var, font=("Segoe UI", 10, "bold"),
            bg=self.CARD_BG, fg=self.TEXT, buttonbackground=self.ACCENT,
            relief="flat", bd=0,
        )
        self.loop_count_spin.pack(side="left", padx=(6, 8))
        tk.Label(loop_row1, text="(0 = 无限循环)", font=("Segoe UI", 8),
                 fg=self.TEXT_MUTED, bg=self.BG).pack(side="left")

        # 第二行：循环间隔
        loop_row2 = tk.Frame(loop_frame, bg=self.BG)
        loop_row2.pack(fill="x")

        tk.Label(loop_row2, text="每轮间隔:", font=("Segoe UI", 9),
                 fg=self.TEXT, bg=self.BG).pack(side="left")
        self.loop_delay_var = tk.StringVar(value="0")
        self.loop_delay_entry = tk.Entry(
            loop_row2, textvariable=self.loop_delay_var, width=8,
            font=("Segoe UI", 10, "bold"),
            bg=self.CARD_BG, fg=self.TEXT, insertbackground=self.TEXT,
            relief="flat", bd=0,
        )
        self.loop_delay_entry.pack(side="left", padx=(6, 4))
        tk.Label(loop_row2, text="秒", font=("Segoe UI", 9),
                 fg=self.TEXT_MUTED, bg=self.BG).pack(side="left")

        # 循环进度标签（回放时显示）
        self.loop_progress_label = tk.Label(
            loop_frame, text="", font=("Segoe UI", 9, "bold"),
            fg=self.ACCENT, bg=self.BG, anchor="w",
        )
        self.loop_progress_label.pack(fill="x", pady=(6, 0))

        # 列表操作按钮（两行）
        script_btns = ttk.Frame(center)
        script_btns.grid(row=3, column=0, sticky="ew", pady=(12, 0))
        script_btns.columnconfigure(0, weight=1)
        script_btns.columnconfigure(1, weight=1)
        script_btns.columnconfigure(2, weight=1)
        script_btns.columnconfigure(3, weight=1)

        # 第一行：执行 / 暂停 / 继续 / 停止
        ttk.Button(script_btns, text="▶ 执行", style="Primary.TButton",
                   command=self._on_play_selected).grid(row=0, column=0, sticky="ew", padx=(0, 4))
        self.btn_pause = ttk.Button(script_btns, text="⏸ 暂停", style="Warning.TButton",
                                     command=self._on_pause, state="disabled")
        self.btn_pause.grid(row=0, column=1, sticky="ew", padx=4)
        self.btn_resume = ttk.Button(script_btns, text="▶ 继续", style="Success.TButton",
                                      command=self._on_resume, state="disabled")
        self.btn_resume.grid(row=0, column=2, sticky="ew", padx=4)
        self.btn_stop = ttk.Button(script_btns, text="⏹ 停止", style="Danger.TButton",
                                    command=self._on_stop_current, state="disabled")
        self.btn_stop.grid(row=0, column=3, sticky="ew", padx=(4, 0))

        # 第二行：删除 / 刷新
        script_btns2 = ttk.Frame(center)
        script_btns2.grid(row=4, column=0, sticky="ew", pady=(8, 0))
        script_btns2.columnconfigure(0, weight=1)
        script_btns2.columnconfigure(1, weight=1)
        ttk.Button(script_btns2, text="🗑 删除",
                   command=self._on_delete_selected).grid(row=0, column=0, sticky="ew", padx=(0, 4))
        ttk.Button(script_btns2, text="↻ 刷新",
                   command=self.refresh_all).grid(row=0, column=1, sticky="ew", padx=(4, 0))

        # ── 右栏：快捷键设置 + 路径扰动算法 ─────────
        right = ttk.Frame(body)
        right.grid(row=0, column=2, sticky="nsew", padx=(0, 0))

        hotkey_panel = ttk.LabelFrame(right, text="  快捷键  ", padding=16)
        hotkey_panel.pack(fill="x", pady=(0, 12))
        hotkey_panel.columnconfigure(0, weight=1)

        # ---- 快捷键设置区 ----
        ttk.Label(hotkey_panel, text="设置（F1-F12）",
                  font=("Segoe UI", 9, "bold"),
                  foreground=self.TEXT_MUTED).pack(anchor="w", pady=(0, 8))

        # F-key 选项列表
        self._fkey_options = [f"F{i}" for i in range(1, 13)]

        # 热键配置项
        self._hotkey_vars: dict[str, tk.StringVar] = {}
        self._hotkey_combos: dict[str, ttk.Combobox] = {}
        # 脏标记：用户修改了下拉框但未保存时为 True，刷新时跳过同步避免覆盖
        self._hotkey_dirty: bool = False
        self._hotkey_labels = {
            "pause_resume": "暂停/继续",
            "start_recording": "开始录制",
            "stop_recording": "停止录制并保存",
            "cancel_recording": "取消录制",
            "record_region": "录制区域（圈选）",
            "mark_anchor": "标记锚点（录制中）",
        }
        for key_name, label_text in self._hotkey_labels.items():
            row = ttk.Frame(hotkey_panel)
            row.pack(fill="x", pady=(0, 6))
            ttk.Label(row, text=label_text, font=("Segoe UI", 9),
                      foreground=self.TEXT).pack(side="left")
            var = tk.StringVar(value="F2")
            self._hotkey_vars[key_name] = var
            combo = ttk.Combobox(row, textvariable=var, values=self._fkey_options,
                                 width=6, state="readonly", font=("Segoe UI", 9, "bold"))
            combo.pack(side="right")
            # 用户修改下拉框时设置脏标记
            combo.bind("<<ComboboxSelected>>",
                       lambda e, c=combo: self._on_hotkey_combo_changed(c))
            self._hotkey_combos[key_name] = combo

        # 保存热键按钮
        ttk.Button(hotkey_panel, text="💾 保存热键设置",
                   style="Primary.TButton",
                   command=self._on_save_hotkeys).pack(fill="x", pady=(8, 12))

        # 分隔线
        ttk.Separator(hotkey_panel, orient="horizontal").pack(fill="x", pady=(0, 12))

        # ---- 当前热键显示区（彩色按键徽章） ----
        ttk.Label(hotkey_panel, text="当前设置",
                  font=("Segoe UI", 9, "bold"),
                  foreground=self.TEXT_MUTED).pack(anchor="w", pady=(0, 8))

        # 每个热键一行：左侧功能名 + 右侧彩色按键徽章
        self._hotkey_badges: dict[str, tk.Label] = {}
        for key_name, label_text in self._hotkey_labels.items():
            row = tk.Frame(hotkey_panel, bg=self.BG)
            row.pack(fill="x", pady=(0, 6))
            tk.Label(row, text=label_text, font=("Segoe UI", 9),
                     fg=self.TEXT_MUTED, bg=self.BG).pack(side="left")
            badge = tk.Label(row, text="F?", font=("Segoe UI", 9, "bold"),
                            fg="#ffffff", bg=self.ACCENT,
                            padx=10, pady=2, bd=0)
            badge.pack(side="right")
            self._hotkey_badges[key_name] = badge

        # ---- 路径扰动算法配置区 ----
        path_panel = ttk.LabelFrame(right, text="  拟人化路径算法  ", padding=14)
        path_panel.pack(fill="both", expand=True)

        # 策略选择行（grid 布局避免遮挡）
        strat_row = tk.Frame(path_panel, bg=self.BG)
        strat_row.pack(fill="x", pady=(0, 6))
        strat_row.columnconfigure(1, weight=1)
        tk.Label(strat_row, text="策略", font=("Segoe UI", 9, "bold"),
                 fg=self.TEXT, bg=self.BG).grid(row=0, column=0, sticky="w", padx=(0, 8))
        self._pp_strategy_var = tk.StringVar(value="fitts")
        self._pp_strategy_combo = ttk.Combobox(
            strat_row, textvariable=self._pp_strategy_var,
            values=["sine", "fitts", "neuromotor", "straight"],
            state="readonly", width=14, font=("Segoe UI", 9, "bold"),
        )
        self._pp_strategy_combo.grid(row=0, column=1, sticky="e")
        self._pp_strategy_combo.bind("<<ComboboxSelected>>", self._on_pp_strategy_change)

        # 策略说明（紧凑单行）
        self._pp_desc_var = tk.StringVar(value="")
        tk.Label(path_panel, textvariable=self._pp_desc_var,
                 font=("Segoe UI", 8), fg=self.TEXT_MUTED, bg=self.BG,
                 wraplength=280, justify="left", anchor="w").pack(fill="x", pady=(0, 10))

        # 参数输入区（grid 布局：标签 | 输入框 | 范围提示）
        self._pp_params_frame = tk.Frame(path_panel, bg=self.BG)
        self._pp_params_frame.pack(fill="x")

        # 参数定义：{strategy: [(key, label, min, max, step), ...]}
        self._pp_param_defs = {
            "sine": [
                ("sine_amplitude_px", "振幅 (px)", 1.0, 50.0, 1.0),
                ("sine_frequency", "频率 (周期)", 1, 10, 1),
            ],
            "fitts": [
                ("fitts_jitter_px", "微抖动 (px)", 0.5, 10.0, 0.5),
                ("fitts_arc_px", "弧线偏移 (px)", 1.0, 30.0, 1.0),
                ("fitts_overshoot_chance", "过冲概率", 0.0, 1.0, 0.05),
                ("fitts_overshoot_px", "过冲距离 (px)", 1.0, 50.0, 1.0),
            ],
            "neuromotor": [
                ("nm_lognormal_sigma", "速度剖面宽度", 0.4, 0.9, 0.05),
                ("nm_perception_noise", "感知噪声", 0.01, 0.1, 0.01),
                ("nm_entropy_alpha", "熵控强度", 0.0, 1.0, 0.1),
                ("nm_lateral_drift", "横向漂移", 0.01, 0.2, 0.01),
                ("nm_max_corrections", "最大修正次数", 1, 5, 1),
            ],
        }
        self._pp_param_vars: dict[str, tk.StringVar] = {}
        self._pp_param_widgets: list = []

        # 保存按钮
        ttk.Button(path_panel, text="💾 保存路径配置",
                   style="Primary.TButton",
                   command=self._on_save_path_planner).pack(fill="x", pady=(12, 0))

        # 初始化路径扰动配置
        self._init_path_planner()

        # ── 第四栏：窗口区域连点 ─────────────────────────
        right2 = ttk.Frame(body)
        right2.grid(row=0, column=3, sticky="nsew", padx=(12, 0))
        self._build_region_panel(right2)

        # ── 第五栏：MIDI 自动演奏 ─────────────────────────
        right3 = ttk.Frame(body)
        right3.grid(row=0, column=4, sticky="nsew", padx=(12, 0))
        self._build_music_panel(right3)

        footer = ttk.Frame(self.root, padding=(22, 0, 22, 16))
        footer.pack(fill="x")
        self.footer_var = tk.StringVar(value="")
        ttk.Label(
            footer,
            textvariable=self.footer_var,
            style="Sub.TLabel",
        ).pack(anchor="w")

        self.root.after(500, self._refresh_loop)

    # ── 事件处理 ────────────────────────────────────

    def _selected_script(self) -> tuple[str, str] | None:
        """返回当前选中脚本的 (stem, type)，未选中返回 None。"""
        selection = self.script_list.curselection()
        if not selection:
            return None
        display = self.script_list.get(selection[0])
        return self._script_map.get(display)

    def on_toggle_move_mouse(self):
        try:
            new_value = self.js_api.toggle_move_mouse()
            self.move_mouse_var.set(bool(new_value))
            self.refresh_status()
        except Exception as exc:
            messagebox.showerror("操作失败", str(exc))

    def _on_list_select(self, event=None):
        # 刷新期间抑制选择事件，避免触发切换确认弹窗
        if self._suppress_select_event:
            return
        try:
            sel = self._selected_script()
            if not sel:
                return

            # 检查当前脚本是否暂停：如果是，弹窗确认是否切换
            try:
                status = self.js_api.get_status() or {}
            except Exception:
                status = {}

            script_paused = bool(status.get("script_paused"))
            current_name = status.get("script_name")

            if script_paused and current_name:
                new_stem = sel[0]
                if new_stem != current_name:
                    # 弹窗确认：切换将停止当前脚本且不保留进度
                    if not messagebox.askyesno(
                        "确认切换",
                        f"当前脚本「{current_name}」已暂停。\n"
                        f"切换到「{new_stem}」将停止当前脚本且不保留执行进度。\n\n"
                        f"是否切换？"
                    ):
                        # 用户取消：恢复选中项到当前脚本
                        self._restore_selection(current_name)
                        return
                    # 用户确认：停止当前脚本
                    try:
                        self.js_api.stop_current()
                    except Exception:
                        pass

            self.selected_script_var.set(sel[0])
            self.root.after(150, self.refresh_status)
        except Exception:
            pass

    def _on_double_click(self, event=None):
        """双击列表项：执行脚本（两种类型均可）。"""
        self._on_play_selected()

    def _on_play_selected(self):
        sel = self._selected_script()
        if not sel:
            messagebox.showinfo("提示", "先选择一个脚本")
            return
        stem, _script_type = sel

        # 检查当前是否有脚本/回放正在运行或暂停
        try:
            status = self.js_api.get_status() or {}
        except Exception:
            status = {}

        script_running = bool(status.get("script_running"))
        script_paused = bool(status.get("script_paused"))
        playback_active = bool(status.get("playback_active"))
        current_name = status.get("script_name")

        if (script_running or playback_active) and current_name != stem:
            # 有脚本/回放在运行或暂停，且要执行的不是当前脚本
            if script_paused:
                msg = (f"当前脚本「{current_name}」已暂停。\n"
                       f"执行「{stem}」将停止当前脚本且不保留执行进度。\n\n"
                       f"是否继续？")
            else:
                msg = (f"当前有脚本/回放正在运行。\n"
                       f"执行「{stem}」将停止当前运行的任务。\n\n"
                       f"是否继续？")
            if not messagebox.askyesno("确认执行", msg):
                return
            # 停止当前任务
            try:
                self.js_api.stop_current()
            except Exception:
                pass

        try:
            # 立即弹 toast 反馈，让用户知道点击生效了
            try:
                from webview import push_toast
                push_toast(f"▶ 点击执行: {stem}", duration=2.0)
            except Exception:
                pass
            # 读取循环配置
            try:
                loop_count = int(self.loop_count_var.get())
                if loop_count < 0:
                    loop_count = 1
            except (ValueError, tk.TclError):
                loop_count = 1
            try:
                loop_delay = float(self.loop_delay_var.get())
                if loop_delay < 0:
                    loop_delay = 0.0
            except (ValueError, tk.TclError):
                loop_delay = 0.0
            self.js_api.play_recording(stem, loop_count=loop_count, loop_delay=loop_delay)
        except Exception as exc:
            messagebox.showerror("执行失败", str(exc))

    def _on_delete_selected(self):
        sel = self._selected_script()
        if not sel:
            messagebox.showinfo("提示", "先选择一个脚本")
            return
        stem, _script_type = sel
        if not messagebox.askyesno("确认删除", f"确定删除脚本 {stem} 吗？"):
            return
        try:
            if self.js_api.delete_script(stem):
                self.refresh_scripts()
            else:
                messagebox.showwarning("删除失败", "脚本删除失败或文件不存在")
        except Exception as exc:
            messagebox.showerror("删除失败", str(exc))

    # ── 暂停/继续/停止按钮 ──────────────────────────

    def _on_pause(self):
        try:
            self.js_api.pause_current()
        except Exception as exc:
            messagebox.showerror("暂停失败", str(exc))

    def _on_resume(self):
        try:
            self.js_api.resume_current()
        except Exception as exc:
            messagebox.showerror("继续失败", str(exc))

    def _on_stop_current(self):
        try:
            self.js_api.stop_current()
        except Exception as exc:
            messagebox.showerror("停止失败", str(exc))

    # ── 热键设置 ──────────────────────────────────

    def _on_hotkey_combo_changed(self, combo: ttk.Combobox):
        """用户修改了下拉框：设置脏标记，防止刷新循环覆盖未保存的修改。"""
        self._hotkey_dirty = True

    def _on_save_hotkeys(self):
        """保存热键设置：收集下拉框的值，调用 API 保存。"""
        hotkeys = {}
        for key_name, var in self._hotkey_vars.items():
            hotkeys[key_name] = var.get()

        # 检查是否有重复按键
        used = list(hotkeys.values())
        if len(used) != len(set(used)):
            messagebox.showwarning("按键冲突", "不同功能不能使用同一个按键，请修改后重试。")
            return

        try:
            ok = self.js_api.set_hotkeys(hotkeys)
            if ok:
                # 保存成功：清除脏标记，立即刷新一次让 UI 同步最新值
                self._hotkey_dirty = False
                self.refresh_status()
            else:
                messagebox.showwarning("保存失败", "热键保存失败，请检查输入。")
        except Exception as exc:
            messagebox.showerror("保存失败", str(exc))

    def _restore_selection(self, stem_to_restore: str | None):
        """把列表选中项恢复到指定 stem（用于取消切换时回退）。"""
        if not stem_to_restore:
            return
        for display, (stem, _t) in self._script_map.items():
            if stem == stem_to_restore:
                try:
                    idx = self.script_list.get(0, tk.END).index(display)
                    self.script_list.selection_clear(0, tk.END)
                    self.script_list.selection_set(idx)
                    self.script_list.see(idx)
                except (ValueError, Exception):
                    pass
                break

    # ── 录制控制 ──────────────────────────────────

    def on_rec_start(self):
        name = self.recording_name_var.get().strip() or "recording"
        try:
            self.js_api.start_recording(name)
            self._update_rec_ui(recording=True)
        except Exception as exc:
            messagebox.showerror("录制失败", str(exc))

    def on_rec_stop(self):
        name = self.recording_name_var.get().strip() or "recording"
        try:
            saved = self.js_api.stop_recording_and_save(name)
            if saved:
                messagebox.showinfo("已保存", f"录制已保存为：{saved}")
                self.refresh_scripts()
            else:
                messagebox.showinfo("提示", "录制结束，无事件记录（未检测到输入）")
        except Exception as exc:
            messagebox.showerror("保存失败", str(exc))
        finally:
            self._update_rec_ui(recording=False)

    def on_rec_cancel(self):
        try:
            self.js_api.cancel_recording()
        except Exception:
            pass
        finally:
            self._update_rec_ui(recording=False)

    def _update_rec_ui(self, recording: bool):
        if recording:
            self.btn_rec_start.configure(state="disabled")
            self.btn_rec_stop.configure(state="normal")
            self.btn_rec_cancel.configure(state="normal")
            self.recording_entry.configure(state="disabled")
            self.recording_indicator.configure(text="● 录制中", foreground=self.DANGER)
        else:
            self.btn_rec_start.configure(state="normal")
            self.btn_rec_stop.configure(state="disabled")
            self.btn_rec_cancel.configure(state="disabled")
            self.recording_entry.configure(state="normal")
            self.recording_indicator.configure(text="", foreground=self.DANGER)

    # ── 锚点模式 ────────────────────────────────

    _ANCHOR_MODE_DESC = {
        "point": "点锚点：按 F12 标记锚点，锚点间鼠标路径拟人化扰动",
        "segment": "段锚点：长按 F12 划定保护段，段内操作原样回放，段间路径扰动",
    }

    def _init_anchor_mode(self):
        """初始化锚点模式（从配置加载）。"""
        try:
            mode = self.js_api.get_anchor_mode()
            if mode not in ("point", "segment"):
                mode = "point"
        except Exception:
            mode = "point"
        self.anchor_mode_var.set(mode)
        self.anchor_mode_desc_var.set(self._ANCHOR_MODE_DESC.get(mode, ""))

    def _on_anchor_mode_change(self, event=None):
        """锚点模式切换事件处理。"""
        mode = self.anchor_mode_var.get()
        try:
            ok = self.js_api.set_anchor_mode(mode)
            if ok:
                self.anchor_mode_desc_var.set(self._ANCHOR_MODE_DESC.get(mode, ""))
            else:
                messagebox.showwarning("设置失败", "锚点模式保存失败")
        except Exception as exc:
            messagebox.showerror("设置失败", str(exc))

    # ── 路径扰动算法配置 ────────────────────────

    _PP_STRATEGY_DESC = {
        "sine": "正弦垂直扰动：平滑弧线，适合简单场景",
        "fitts": "Fitts 拟人化：加减速 + 过冲 + 微抖动，更自然",
        "neuromotor": "间歇预测控制：基于 2021-2025 顶会研究的前沿模型",
        "straight": "纯直线：零扰动，用于调试桥接路径",
    }

    def _init_path_planner(self):
        """初始化路径扰动配置（从配置加载）。"""
        try:
            config = self.js_api.get_path_planner_config()
        except Exception:
            config = {}
        strategy = config.get("strategy", "fitts")
        if strategy not in ("sine", "fitts", "neuromotor", "straight"):
            strategy = "fitts"
        self._pp_strategy_var.set(strategy)
        self._pp_desc_var.set(self._PP_STRATEGY_DESC.get(strategy, ""))

        # 初始化所有参数变量（保存全部策略的参数，切换时不丢失）
        for strat, params in self._pp_param_defs.items():
            for key, label, lo, hi, step in params:
                val = config.get(key, lo)
                self._pp_param_vars[key] = tk.StringVar(value=str(val))

        self._rebuild_pp_params(strategy)

    def _on_pp_strategy_change(self, event=None):
        """策略切换时重建参数输入区并自动保存。"""
        strategy = self._pp_strategy_var.get()
        self._pp_desc_var.set(self._PP_STRATEGY_DESC.get(strategy, ""))
        self._rebuild_pp_params(strategy)
        # 策略切换自动保存，避免用户忘记点保存
        self._on_save_path_planner(silent=True)

    def _rebuild_pp_params(self, strategy: str):
        """根据当前策略重建参数输入区（grid 布局，避免遮挡）。"""
        # 清除旧 widget
        for w in self._pp_param_widgets:
            w.destroy()
        self._pp_param_widgets.clear()

        params = self._pp_param_defs.get(strategy, [])
        if not params:
            # straight 无参数，显示提示
            hint = tk.Label(self._pp_params_frame, text="无参数（纯直线输出）",
                            font=("Segoe UI", 9), fg=self.TEXT_MUTED, bg=self.BG)
            hint.grid(row=0, column=0, columnspan=3, sticky="w", pady=4)
            self._pp_param_widgets.append(hint)
            return

        # 表头
        for col, (text, anchor) in enumerate([("参数", "w"), ("值", "e"), ("范围", "w")]):
            lbl = tk.Label(self._pp_params_frame, text=text, font=("Segoe UI", 8),
                           fg=self.TEXT_MUTED, bg=self.BG, anchor=anchor)
            lbl.grid(row=0, column=col, sticky=anchor, padx=(0, 6), pady=(0, 4))
            self._pp_param_widgets.append(lbl)

        for row_idx, (key, label, lo, hi, step) in enumerate(params, start=1):
            # 标签列
            lbl = tk.Label(self._pp_params_frame, text=label, font=("Segoe UI", 9),
                           fg=self.TEXT, bg=self.BG, anchor="w")
            lbl.grid(row=row_idx, column=0, sticky="w", pady=3, padx=(0, 8))
            self._pp_param_widgets.append(lbl)

            # 输入框列
            var = self._pp_param_vars.get(key)
            if var is None:
                var = tk.StringVar(value=str(lo))
                self._pp_param_vars[key] = var
            spin = tk.Spinbox(
                self._pp_params_frame, from_=lo, to=hi, increment=step, width=7,
                textvariable=var, font=("Segoe UI", 9, "bold"),
                bg=self.CARD_BG, fg=self.TEXT, buttonbackground=self.ACCENT,
                relief="flat", bd=0,
            )
            spin.grid(row=row_idx, column=1, sticky="e", pady=3)
            self._pp_param_widgets.append(spin)

            # 范围提示列
            range_text = f"{lo}~{hi}"
            range_lbl = tk.Label(self._pp_params_frame, text=range_text,
                                 font=("Segoe UI", 8), fg=self.TEXT_MUTED, bg=self.BG)
            range_lbl.grid(row=row_idx, column=2, sticky="w", padx=(8, 0), pady=3)
            self._pp_param_widgets.append(range_lbl)

        # 配置列权重：标签列自适应，输入框列固定
        self._pp_params_frame.columnconfigure(0, weight=1)

    def _on_save_path_planner(self, silent: bool = False):
        """保存路径扰动配置（包含所有策略的参数，切换不丢失）。"""
        strategy = self._pp_strategy_var.get()
        config = {"strategy": strategy}

        # 收集所有策略的参数（而非仅当前策略，避免切换后丢失）
        for strat, params in self._pp_param_defs.items():
            for key, label, lo, hi, step in params:
                var = self._pp_param_vars.get(key)
                if var is None:
                    continue
                try:
                    val = float(var.get())
                    val = max(lo, min(hi, val))
                    if isinstance(step, int):
                        val = int(val)
                    config[key] = val
                except (ValueError, tk.TclError):
                    config[key] = lo

        try:
            ok = self.js_api.set_path_planner_config(config)
            if ok:
                if not silent:
                    push_toast("✅ 路径扰动配置已保存", duration=2.5)
            else:
                if not silent:
                    messagebox.showwarning("保存失败", "路径扰动配置保存失败")
        except Exception as exc:
            if not silent:
                messagebox.showerror("保存失败", str(exc))

    # ── 第四栏：窗口区域连点 ──────────────────────

    # (key, 标签, 抖动 key 或 None)
    _RC_CLICK_ROWS = [
        ("clicks_per_spot", "每点点击次数", "clicks_per_spot_jitter"),
        ("interval_ms", "点击间隔 (ms)", "interval_jitter_ms"),
        ("hold_ms", "按住时长 (ms)", "hold_jitter_ms"),
        ("x_jitter_px", "落点抖动 X (px)", None),
        ("y_jitter_px", "落点抖动 Y (px)", None),
    ]
    _RC_MOVE_ROWS = [
        ("move_duration_ms", "移动耗时 (ms)", "move_duration_jitter_ms"),
        ("path_steps", "路径步数 (0=自动)", None),
        ("min_spot_distance_px", "最小点距 (px)", None),
        ("margin_px", "区域内边距 (px)", None),
        ("spot_pause_ms", "换点停顿 (ms)", "spot_pause_jitter_ms"),
        ("correct_drift_px", "漂移校正阈值 (0=关)", None),
    ]
    _RC_STRATEGIES = ["global", "sine", "fitts", "neuromotor", "straight"]
    _RC_BUTTONS = ["left", "right", "middle"]
    _RC_NO_PRESET = "— 无 —"

    def _make_scrollable(self, parent):
        """返回一个可垂直滚动的内层 Frame（第四栏控件较多）。"""
        canvas = tk.Canvas(parent, bg=self.BG, highlightthickness=0, bd=0)
        vsb = ttk.Scrollbar(parent, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)

        inner = ttk.Frame(canvas)
        inner_id = canvas.create_window((0, 0), window=inner, anchor="nw")

        def _on_inner_configure(event=None):
            canvas.configure(scrollregion=canvas.bbox("all"))
        inner.bind("<Configure>", _on_inner_configure)

        def _on_canvas_configure(event):
            canvas.itemconfigure(inner_id, width=event.width)
        canvas.bind("<Configure>", _on_canvas_configure)

        def _on_wheel(event):
            canvas.yview_scroll(int(-event.delta / 120), "units")

        # 仅在鼠标进入第四栏时接管滚轮，避免劫持其他区域
        canvas.bind("<Enter>", lambda e: canvas.bind_all("<MouseWheel>", _on_wheel))
        canvas.bind("<Leave>", lambda e: canvas.unbind_all("<MouseWheel>"))
        return inner

    def _build_region_panel(self, parent):
        """构建第四栏「窗口区域连点」面板。"""
        panel = self._make_scrollable(parent)

        self._rc_vars: dict[str, tk.StringVar] = {}
        self._rc_entries: dict[str, tk.Entry] = {}
        self._rc_capture_widgets: list = []
        self._rc_forever_var = tk.BooleanVar(value=True)
        self._rc_region_status = tk.StringVar(value="当前区域：未设置")
        self._rc_run_status = tk.StringVar(value="未运行")
        self._rc_selector = RegionSelector(self.root)

        # ---- 组 1：目标区域 ----
        region_frame = ttk.LabelFrame(panel, text="  目标区域  ", padding=12)
        region_frame.pack(fill="x", pady=(0, 10))

        preset_row = tk.Frame(region_frame, bg=self.BG)
        preset_row.pack(fill="x", pady=(0, 6))
        tk.Label(preset_row, text="预设", font=("Segoe UI", 9, "bold"),
                 fg=self.TEXT, bg=self.BG).pack(side="left", padx=(0, 6))
        self._rc_preset_var = tk.StringVar(value=self._RC_NO_PRESET)
        self._rc_preset_combo = ttk.Combobox(
            preset_row, textvariable=self._rc_preset_var,
            values=[self._RC_NO_PRESET], state="readonly", width=14,
            font=("Segoe UI", 9, "bold"))
        self._rc_preset_combo.pack(side="left", fill="x", expand=True)
        self._rc_preset_combo.bind("<<ComboboxSelected>>",
                                   self._on_region_preset_selected)
        self._rc_capture_widgets.append(self._rc_preset_combo)

        btn_row = tk.Frame(region_frame, bg=self.BG)
        btn_row.pack(fill="x", pady=(0, 8))
        ttk.Button(btn_row, text="💾 存为预设",
                   command=self._on_save_region_preset
                   ).pack(side="left", expand=True, fill="x", padx=(0, 4))
        ttk.Button(btn_row, text="🗑 删除",
                   command=self._on_delete_region_preset
                   ).pack(side="left", expand=True, fill="x")

        # X / Y / 宽 / 高（两行两列）
        xy_grid = tk.Frame(region_frame, bg=self.BG)
        xy_grid.pack(fill="x", pady=(0, 8))
        for idx, (key, label) in enumerate([("region_x", "X"), ("region_y", "Y"),
                                            ("region_width", "宽"),
                                            ("region_height", "高")]):
            r, c = divmod(idx, 2)
            cell = tk.Frame(xy_grid, bg=self.BG)
            cell.grid(row=r, column=c, sticky="ew", padx=(0, 6), pady=2)
            tk.Label(cell, text=label, font=("Segoe UI", 9), fg=self.TEXT_MUTED,
                     bg=self.BG, width=2, anchor="w").pack(side="left")
            var = tk.StringVar(value="0")
            self._rc_vars[key] = var
            entry = tk.Entry(cell, textvariable=var, width=8,
                             font=("Segoe UI", 9, "bold"), bg=self.CARD_BG,
                             fg=self.TEXT, insertbackground=self.TEXT,
                             relief="flat", bd=0, justify="right")
            entry.pack(side="left", fill="x", expand=True)
            self._rc_entries[key] = entry
        xy_grid.columnconfigure(0, weight=1)
        xy_grid.columnconfigure(1, weight=1)

        cap_row = tk.Frame(region_frame, bg=self.BG)
        cap_row.pack(fill="x", pady=(0, 6))
        for text, mode in (("📐 拖拽圈选", "drag"), ("🪟 拾取窗口", "window")):
            btn = ttk.Button(cap_row, text=text,
                             command=lambda m=mode: self._start_region_capture(m))
            btn.pack(side="left", expand=True, fill="x", padx=(0, 4))
            self._rc_capture_widgets.append(btn)
        hotkey_btn = ttk.Button(cap_row, text="🎙 录制区域",
                                command=self._on_record_region_request)
        hotkey_btn.pack(side="left", expand=True, fill="x")
        self._rc_capture_widgets.append(hotkey_btn)
        self._rc_record_btn = hotkey_btn

        tk.Label(region_frame, textvariable=self._rc_region_status,
                 font=("Segoe UI", 9), fg=self.TEXT_MUTED, bg=self.BG,
                 anchor="w", justify="left").pack(fill="x")

        # ---- 组 2：点击参数 ----
        click_frame = ttk.LabelFrame(panel, text="  点击参数  ", padding=12)
        click_frame.pack(fill="x", pady=(0, 10))
        # 数值行用 grid、总次数行用 pack：Tkinter 禁止同一容器混用两种布局，
        # 因此把 grid 行收进一个子 Frame
        click_rows = tk.Frame(click_frame, bg=self.BG)
        click_rows.pack(fill="x")
        for row_idx, (key, label, jkey) in enumerate(self._RC_CLICK_ROWS):
            self._rc_add_row(click_rows, row_idx, label, key, jkey)

        total_row = tk.Frame(click_frame, bg=self.BG)
        total_row.pack(fill="x", pady=(8, 0))
        tk.Label(total_row, text="总点击次数 (0=无限)", font=("Segoe UI", 9),
                 fg=self.TEXT, bg=self.BG).pack(side="left")
        clicks_var = tk.StringVar(value="0")
        self._rc_vars["clicks"] = clicks_var
        clicks_entry = tk.Entry(total_row, textvariable=clicks_var, width=7,
                                font=("Segoe UI", 9, "bold"), bg=self.CARD_BG,
                                fg=self.TEXT, insertbackground=self.TEXT,
                                relief="flat", bd=0, justify="right")
        clicks_entry.pack(side="left", padx=(8, 8))
        self._rc_entries["clicks"] = clicks_entry
        ttk.Checkbutton(total_row, text="无限循环",
                        variable=self._rc_forever_var,
                        command=self._on_rc_forever_toggle).pack(side="left")

        # ---- 组 3：移动参数 ----
        move_frame = ttk.LabelFrame(panel, text="  移动参数  ", padding=12)
        move_frame.pack(fill="x", pady=(0, 10))
        row_idx = 0
        for key, label, jkey in self._RC_MOVE_ROWS:
            self._rc_add_row(move_frame, row_idx, label, key, jkey)
            row_idx += 1
        self._rc_add_combo_row(move_frame, row_idx, "路径策略",
                               "path_strategy", self._RC_STRATEGIES)
        row_idx += 1
        self._rc_add_combo_row(move_frame, row_idx, "鼠标按键",
                               "button", self._RC_BUTTONS)
        row_idx += 1
        tk.Label(move_frame,
                 text="策略参数（振幅/弧线/过冲等）沿用第三栏『拟人化路径算法』的配置；"
                      "global = 直接使用第三栏选定的策略。",
                 font=("Segoe UI", 8), fg=self.TEXT_MUTED, bg=self.BG,
                 wraplength=300, justify="left", anchor="w"
                 ).grid(row=row_idx, column=0, columnspan=3, sticky="w", pady=(8, 0))

        # ---- 组 4：运行控制 ----
        run_frame = ttk.LabelFrame(panel, text="  运行控制  ", padding=12)
        run_frame.pack(fill="x")
        self.btn_rc_start = ttk.Button(run_frame, text="▶ 开始窗口连点",
                                       style="Primary.TButton",
                                       command=self._on_start_region_click)
        self.btn_rc_start.pack(fill="x", pady=(0, 6))

        rc_ctrl = tk.Frame(run_frame, bg=self.BG)
        rc_ctrl.pack(fill="x", pady=(0, 6))
        self.btn_rc_pause = ttk.Button(rc_ctrl, text="⏸ 暂停",
                                       command=self._on_pause, state="disabled")
        self.btn_rc_pause.pack(side="left", expand=True, fill="x", padx=(0, 4))
        self.btn_rc_resume = ttk.Button(rc_ctrl, text="▶ 继续",
                                        command=self._on_resume, state="disabled")
        self.btn_rc_resume.pack(side="left", expand=True, fill="x", padx=(0, 4))
        self.btn_rc_stop = ttk.Button(rc_ctrl, text="⏹ 停止",
                                      style="Danger.TButton",
                                      command=self._on_stop_region_click,
                                      state="disabled")
        self.btn_rc_stop.pack(side="left", expand=True, fill="x")

        rc_save = tk.Frame(run_frame, bg=self.BG)
        rc_save.pack(fill="x", pady=(0, 8))
        ttk.Button(rc_save, text="💾 保存参数",
                   command=self._on_save_region_config
                   ).pack(side="left", expand=True, fill="x", padx=(0, 4))
        ttk.Button(rc_save, text="💾 存为脚本",
                   command=self._on_save_region_as_script
                   ).pack(side="left", expand=True, fill="x")

        self._rc_run_label = tk.Label(run_frame, textvariable=self._rc_run_status,
                                      font=("Segoe UI", 9, "bold"),
                                      fg=self.TEXT_MUTED, bg=self.BG,
                                      anchor="w", justify="left")
        self._rc_run_label.pack(fill="x")

        self._init_region_click()

    def _rc_add_row(self, parent, row_idx: int, label: str, key: str,
                    jitter_key: str | None = None):
        """添加一行：标签 | 输入框 | （可选）±抖动输入框。"""
        tk.Label(parent, text=label, font=("Segoe UI", 9), fg=self.TEXT,
                 bg=self.BG, anchor="w"
                 ).grid(row=row_idx, column=0, sticky="w", pady=2, padx=(0, 6))

        var = self._rc_vars.get(key)
        if var is None:
            var = tk.StringVar(value="0")
            self._rc_vars[key] = var
        entry = tk.Entry(parent, textvariable=var, width=8,
                         font=("Segoe UI", 9, "bold"), bg=self.CARD_BG,
                         fg=self.TEXT, insertbackground=self.TEXT,
                         relief="flat", bd=0, justify="right")
        entry.grid(row=row_idx, column=1, sticky="e", pady=2)
        self._rc_entries[key] = entry

        if jitter_key:
            jvar = self._rc_vars.get(jitter_key)
            if jvar is None:
                jvar = tk.StringVar(value="0")
                self._rc_vars[jitter_key] = jvar
            jcell = tk.Frame(parent, bg=self.BG)
            jcell.grid(row=row_idx, column=2, sticky="e", pady=2, padx=(6, 0))
            tk.Label(jcell, text="±", font=("Segoe UI", 9),
                     fg=self.TEXT_MUTED, bg=self.BG).pack(side="left")
            jentry = tk.Entry(jcell, textvariable=jvar, width=5,
                              font=("Segoe UI", 9, "bold"), bg=self.CARD_BG,
                              fg=self.TEXT, insertbackground=self.TEXT,
                              relief="flat", bd=0, justify="right")
            jentry.pack(side="left")
            self._rc_entries[jitter_key] = jentry

        parent.columnconfigure(0, weight=1)

    def _rc_add_combo_row(self, parent, row_idx: int, label: str, key: str,
                          values: list):
        """添加一行：标签 | 下拉选择。"""
        tk.Label(parent, text=label, font=("Segoe UI", 9), fg=self.TEXT,
                 bg=self.BG, anchor="w"
                 ).grid(row=row_idx, column=0, sticky="w", pady=2, padx=(0, 6))
        var = tk.StringVar(value=values[0])
        self._rc_vars[key] = var
        combo = ttk.Combobox(parent, textvariable=var, values=values,
                             state="readonly", width=12,
                             font=("Segoe UI", 9, "bold"))
        combo.grid(row=row_idx, column=1, columnspan=2, sticky="e", pady=2)
        parent.columnconfigure(0, weight=1)

    # 参与收集/回填的整数字段
    _RC_INT_KEYS = (
        "region_x", "region_y", "region_width", "region_height",
        "clicks", "clicks_per_spot", "clicks_per_spot_jitter",
        "interval_ms", "interval_jitter_ms", "hold_ms", "hold_jitter_ms",
        "move_duration_ms", "move_duration_jitter_ms", "margin_px",
        "min_spot_distance_px", "x_jitter_px", "y_jitter_px",
        "spot_pause_ms", "spot_pause_jitter_ms", "path_steps", "correct_drift_px",
    )

    def _init_region_click(self):
        """从配置加载区域连点参数到第四栏控件。"""
        try:
            cfg = self.js_api.get_region_click_config() or {}
        except Exception:
            cfg = {}
        for key in self._RC_INT_KEYS:
            var = self._rc_vars.get(key)
            if var is None or key not in cfg:
                continue
            try:
                var.set(str(int(cfg[key])))
            except (TypeError, ValueError):
                pass
        if cfg.get("path_strategy") in self._RC_STRATEGIES:
            self._rc_vars["path_strategy"].set(cfg["path_strategy"])
        if cfg.get("button") in self._RC_BUTTONS:
            self._rc_vars["button"].set(cfg["button"])
        self._rc_forever_var.set(bool(cfg.get("forever", True)))
        self._on_rc_forever_toggle()
        self._refresh_region_presets(select=str(cfg.get("region_preset") or ""))
        self._refresh_region_status_label()

    def _on_rc_forever_toggle(self):
        """勾选无限循环时禁用总次数输入框。"""
        entry = self._rc_entries.get("clicks")
        if entry is None:
            return
        disabled = bool(self._rc_forever_var.get())
        try:
            entry.configure(state="disabled" if disabled else "normal",
                            fg=self.TEXT_MUTED if disabled else self.TEXT)
        except Exception:
            pass

    def _rc_flash_invalid(self, key: str):
        """非法输入框瞬时红框提示。"""
        entry = self._rc_entries.get(key)
        if entry is None:
            return
        try:
            entry.configure(highlightthickness=1, highlightbackground=self.DANGER)
            self.root.after(1600,
                            lambda e=entry: e.configure(highlightthickness=0))
        except Exception:
            pass

    def _collect_region_config(self, require_region: bool = True):
        """从输入框收集区域连点参数；非法值红框提示并返回 None。"""
        cfg: dict = {}
        bad_key = None
        for key in self._RC_INT_KEYS:
            var = self._rc_vars.get(key)
            if var is None:
                continue
            try:
                cfg[key] = int(float(var.get()))
            except (ValueError, tk.TclError):
                if bad_key is None:
                    bad_key = key
                cfg[key] = 0
        if bad_key:
            self._rc_flash_invalid(bad_key)
            push_toast(f"❌ 「{bad_key}」不是合法整数", duration=3.0)
            return None

        cfg["forever"] = bool(self._rc_forever_var.get())
        strategy = self._rc_vars.get("path_strategy")
        cfg["path_strategy"] = (strategy.get() if strategy else "global")
        if cfg["path_strategy"] not in self._RC_STRATEGIES:
            cfg["path_strategy"] = "global"
        button = self._rc_vars.get("button")
        cfg["button"] = (button.get() if button else "left")
        if cfg["button"] not in self._RC_BUTTONS:
            cfg["button"] = "left"
        preset = self._rc_preset_var.get()
        cfg["region_preset"] = "" if preset == self._RC_NO_PRESET else preset

        if require_region and not cfg["region_preset"]:
            if cfg["region_width"] <= 0 or cfg["region_height"] <= 0:
                push_toast("❌ 请先圈选区域或填写宽度/高度", duration=3.0)
                return None
        return cfg

    def _current_region(self):
        """读取输入框里的区域（尺寸非法返回 None）。"""
        try:
            x = int(float(self._rc_vars["region_x"].get()))
            y = int(float(self._rc_vars["region_y"].get()))
            w = int(float(self._rc_vars["region_width"].get()))
            h = int(float(self._rc_vars["region_height"].get()))
        except (KeyError, ValueError, tk.TclError):
            return None
        if w <= 0 or h <= 0:
            return None
        return {"x": x, "y": y, "width": w, "height": h}

    def _apply_region(self, region):
        """把区域字典回填到 X/Y/宽/高 输入框并刷新状态标签。"""
        if not region:
            return
        for key, src in (("region_x", "x"), ("region_y", "y"),
                         ("region_width", "width"), ("region_height", "height")):
            var = self._rc_vars.get(key)
            if var is None:
                continue
            try:
                var.set(str(int(region.get(src, 0))))
            except (TypeError, ValueError):
                var.set("0")
        self._refresh_region_status_label()

    def _refresh_region_status_label(self):
        region = self._current_region()
        if region:
            self._rc_region_status.set(f"当前区域：{format_region(region)}")
        else:
            self._rc_region_status.set("当前区域：未设置")

    def _refresh_region_presets(self, select: str = ""):
        """刷新预设下拉框（保留当前选中项）。"""
        try:
            names = list(self.js_api.list_region_presets() or [])
        except Exception:
            names = []
        values = [self._RC_NO_PRESET] + names
        try:
            self._rc_preset_combo.configure(values=values)
        except Exception:
            return
        if select and select in names:
            self._rc_preset_var.set(select)
        elif self._rc_preset_var.get() not in values:
            self._rc_preset_var.set(self._RC_NO_PRESET)

    def _on_region_preset_selected(self, event=None):
        name = self._rc_preset_var.get()
        if not name or name == self._RC_NO_PRESET:
            return
        try:
            region = self.js_api.get_region_preset(name)
        except Exception:
            region = None
        if not region:
            push_toast(f"❌ 预设「{name}」不存在", duration=2.5)
            return
        self._apply_region(region)
        push_toast(f"✓ 已载入预设「{name}」 {format_region(region)}", duration=2.5)

    def _on_save_region_preset(self):
        region = self._current_region()
        if not region:
            push_toast("❌ 请先圈选或填写有效的区域（宽/高需大于 0）", duration=3.0)
            return
        current = self._rc_preset_var.get()
        initial = "" if current == self._RC_NO_PRESET else current
        name = simpledialog.askstring("存为预设", "预设名称：",
                                     parent=self.root, initialvalue=initial)
        if not name or not name.strip():
            return
        name = name.strip()
        try:
            ok = self.js_api.save_region_preset(name, region)
        except Exception as exc:
            messagebox.showerror("保存失败", str(exc))
            return
        if ok:
            self._refresh_region_presets(select=name)

    def _on_delete_region_preset(self):
        name = self._rc_preset_var.get()
        if not name or name == self._RC_NO_PRESET:
            push_toast("先在下拉框中选择一个预设", duration=2.5)
            return
        if not messagebox.askyesno("确认删除", f"确定删除区域预设「{name}」吗？"):
            return
        try:
            ok = self.js_api.delete_region_preset(name)
        except Exception as exc:
            messagebox.showerror("删除失败", str(exc))
            return
        if ok:
            self._rc_preset_var.set(self._RC_NO_PRESET)
            self._refresh_region_presets()

    def _on_record_region_request(self):
        """「🎙 录制区域」按钮：走与热键完全相同的请求队列路径。"""
        try:
            self.js_api.request_region_capture("drag")
        except Exception as exc:
            messagebox.showerror("录制失败", str(exc))

    def _start_region_capture(self, mode: str = "drag"):
        """在主线程启动区域录制（Tk 窗口不可在子线程创建）。"""
        try:
            if self._rc_selector.busy:
                return
        except Exception:
            pass
        if mode == "window":
            self._rc_selector.pick_window(on_done=self._on_region_captured)
        else:
            self._rc_selector.select_by_drag(on_done=self._on_region_captured)

    def _on_region_captured(self, region):
        """区域录制回调（region 为 None 表示取消或无效）。"""
        if not region:
            push_toast("已取消区域录制", duration=2.0)
            return
        self._apply_region(region)
        title = region.get("title")
        suffix = f"（{title}）" if title else ""
        push_toast(f"✓ 已录制区域 {format_region(region)}{suffix}", duration=3.0)

    def _on_start_region_click(self):
        cfg = self._collect_region_config(require_region=True)
        if cfg is None:
            return
        try:
            ok = self.js_api.start_region_click(cfg)
        except Exception as exc:
            messagebox.showerror("启动失败", str(exc))
            return
        if ok:
            self.root.after(200, self.refresh_status)

    def _on_stop_region_click(self):
        try:
            self.js_api.stop_region_click()
        except Exception as exc:
            messagebox.showerror("停止失败", str(exc))

    def _on_save_region_config(self):
        cfg = self._collect_region_config(require_region=False)
        if cfg is None:
            return
        try:
            ok = self.js_api.save_region_click_config(cfg)
        except Exception as exc:
            messagebox.showerror("保存失败", str(exc))
            return
        push_toast("✅ 区域连点参数已保存" if ok else "❌ 参数保存失败", duration=2.5)

    def _on_save_region_as_script(self):
        cfg = self._collect_region_config(require_region=True)
        if cfg is None:
            return
        name = simpledialog.askstring("存为脚本", "脚本名称：",
                                     parent=self.root,
                                     initialvalue="region_click")
        if not name or not name.strip():
            return
        try:
            ok = self.js_api.save_region_click_as_script(name.strip(), cfg)
        except Exception as exc:
            messagebox.showerror("保存失败", str(exc))
            return
        if ok:
            self.refresh_scripts()

    def _update_region_ui(self, status: dict):
        """同步第四栏的运行状态、统计与按钮可用性。"""
        try:
            region_active = bool(status.get("region_click_active"))
            script_running = bool(status.get("script_running"))
            script_paused = bool(status.get("script_paused"))
            playback_active = bool(status.get("playback_active"))
            busy = region_active or script_running or playback_active

            self.btn_rc_start.configure(state="disabled" if busy else "normal")
            for widget in self._rc_capture_widgets:
                try:
                    if busy:
                        widget.configure(state="disabled")
                    elif isinstance(widget, ttk.Combobox):
                        widget.configure(state="readonly")
                    else:
                        widget.configure(state="normal")
                except Exception:
                    pass

            if region_active and script_paused:
                self.btn_rc_pause.configure(state="disabled")
                self.btn_rc_resume.configure(state="normal")
                self.btn_rc_stop.configure(state="normal")
            elif region_active:
                self.btn_rc_pause.configure(state="normal")
                self.btn_rc_resume.configure(state="disabled")
                self.btn_rc_stop.configure(state="normal")
            else:
                self.btn_rc_pause.configure(state="disabled")
                self.btn_rc_resume.configure(state="disabled")
                self.btn_rc_stop.configure(state="disabled")

            total = int(status.get("region_click_total", 0) or 0)
            spots = int(status.get("region_click_spots", 0) or 0)
            if region_active:
                text = f"● 运行中 · 已点击 {total} 次 · 已换点 {spots} 次"
                color = self.WARNING if script_paused else self.SUCCESS
            elif total or spots:
                text = f"已停止 · 本次共点击 {total} 次 · 换点 {spots} 次"
                color = self.TEXT_MUTED
            else:
                text = "未运行"
                color = self.TEXT_MUTED
            self._rc_run_status.set(text)
            self._rc_run_label.configure(foreground=color)
        except Exception:
            pass

    # ── 第五栏：MIDI 自动演奏 ──────────────────────

    # (配置字段, 标签, 最小值, 最大值)
    _MUSIC_PARAM_ROWS = [
        ("speed_percent", "演奏速度 (%)", 10, 400),
        ("hold_percent", "按键保持 (%)", 5, 100),
        ("countdown_sec", "开演倒计时 (秒)", 0, 30),
        ("loop_count", "循环次数 (0=无限)", 0, 99999),
        ("loop_delay_ms", "每轮间隔 (ms)", 0, 600000),
        ("max_polyphony", "最多同时按键 (0=不限)", 0, 16),
    ]
    _MUSIC_NO_SCORE = "（未选择曲谱）"

    def _build_music_panel(self, parent):
        """构建第五栏「MIDI 自动演奏」面板。"""
        panel = self._make_scrollable(parent)

        self._music_map: dict[str, str] = {}
        self._music_analysis: dict | None = None
        self._music_bindings: list[dict] = []
        self._music_vars: dict[str, tk.StringVar] = {}
        self._music_auto_shift = tk.BooleanVar(value=True)
        self._music_dir_var = tk.StringVar(value="")
        self._music_check_var = tk.StringVar(value="尚未检查")
        self._music_summary_var = tk.StringVar(value="")
        self._music_range_var = tk.StringVar(value="")
        self._music_run_var = tk.StringVar(value="未运行")
        self._music_progress = tk.DoubleVar(value=0)

        # ---- 组 1：曲谱库 ----
        lib_frame = ttk.LabelFrame(panel, text="  曲谱库（data/music）  ", padding=12)
        lib_frame.pack(fill="x", pady=(0, 10))

        list_box = tk.Frame(lib_frame, bg=self.BG)
        list_box.pack(fill="x", pady=(0, 8))
        music_scroll = ttk.Scrollbar(list_box, orient="vertical")
        music_scroll.pack(side="right", fill="y")
        self.music_list = tk.Listbox(
            list_box, activestyle="none", height=7,
            font=("Segoe UI", 10),
            bg=self.CARD_BG, fg=self.TEXT,
            selectbackground=self.SELECT_BG, selectforeground="#ffffff",
            highlightthickness=0, bd=0, relief="flat",
        )
        self.music_list.pack(side="left", fill="both", expand=True)
        self.music_list.configure(yscrollcommand=music_scroll.set)
        music_scroll.configure(command=self.music_list.yview)
        self.music_list.bind("<<ListboxSelect>>", lambda e: self._on_music_selected())
        self.music_list.bind("<Double-Button-1>", lambda e: self._on_music_play())

        lib_btns = tk.Frame(lib_frame, bg=self.BG)
        lib_btns.pack(fill="x", pady=(0, 6))
        ttk.Button(lib_btns, text="📂 导入 MIDI",
                   command=self._on_music_import).pack(side="left", expand=True,
                                                       fill="x", padx=(0, 4))
        ttk.Button(lib_btns, text="↻ 刷新",
                   command=self.refresh_music_scores).pack(side="left", expand=True,
                                                           fill="x", padx=4)
        ttk.Button(lib_btns, text="🗑 删除",
                   command=self._on_music_delete).pack(side="left", expand=True,
                                                       fill="x", padx=(4, 0))
        tk.Label(lib_frame, textvariable=self._music_dir_var, font=("Segoe UI", 8),
                 fg=self.TEXT_MUTED, bg=self.BG, anchor="w", justify="left",
                 wraplength=300).pack(fill="x")

        # ---- 组 2：键位绑定 ----
        bind_frame = ttk.LabelFrame(panel, text="  键位绑定  ", padding=12)
        bind_frame.pack(fill="x", pady=(0, 10))
        self._music_badges = tk.Frame(bind_frame, bg=self.BG)
        self._music_badges.pack(fill="x", pady=(0, 6))
        tk.Label(bind_frame, textvariable=self._music_range_var,
                 font=("Segoe UI", 9), fg=self.TEXT_MUTED, bg=self.BG,
                 anchor="w", justify="left", wraplength=300).pack(fill="x", pady=(0, 8))

        bind_btns = tk.Frame(bind_frame, bg=self.BG)
        bind_btns.pack(fill="x")
        ttk.Button(bind_btns, text="✏ 编辑键位",
                   command=self._on_music_edit_bindings).pack(side="left", expand=True,
                                                              fill="x", padx=(0, 4))
        ttk.Button(bind_btns, text="↺ 恢复默认",
                   command=self._on_music_reset_bindings).pack(side="left", expand=True,
                                                               fill="x", padx=(4, 0))

        # ---- 组 3：曲谱检查 ----
        check_frame = ttk.LabelFrame(panel, text="  曲谱检查  ", padding=12)
        check_frame.pack(fill="x", pady=(0, 10))

        shift_row = tk.Frame(check_frame, bg=self.BG)
        shift_row.pack(fill="x", pady=(0, 6))
        tk.Label(shift_row, text="移调 (半音)", font=("Segoe UI", 9),
                 fg=self.TEXT, bg=self.BG).pack(side="left")
        self._music_vars["semitone_shift"] = tk.StringVar(value="0")
        shift_spin = tk.Spinbox(
            shift_row, from_=-48, to=48, width=6,
            textvariable=self._music_vars["semitone_shift"],
            font=("Segoe UI", 9, "bold"),
            bg=self.CARD_BG, fg=self.TEXT, buttonbackground=self.ACCENT,
            relief="flat", bd=0, justify="right",
            command=self._on_music_shift_change,
        )
        shift_spin.pack(side="left", padx=(8, 8))
        shift_spin.bind("<Return>", lambda e: self._on_music_shift_change())
        shift_spin.bind("<FocusOut>", lambda e: self._on_music_shift_change())
        ttk.Checkbutton(shift_row, text="自动移调",
                        variable=self._music_auto_shift,
                        command=self._on_music_shift_change).pack(side="left")

        self._music_check_label = tk.Label(
            check_frame, textvariable=self._music_check_var,
            font=("Segoe UI", 11, "bold"), fg=self.TEXT_MUTED, bg=self.BG,
            anchor="w", justify="left", wraplength=300,
        )
        self._music_check_label.pack(fill="x", pady=(0, 4))
        tk.Label(check_frame, textvariable=self._music_summary_var,
                 font=("Segoe UI", 9), fg=self.TEXT_MUTED, bg=self.BG,
                 anchor="w", justify="left", wraplength=300).pack(fill="x", pady=(0, 6))

        detail_box = tk.Frame(check_frame, bg=self.BG)
        detail_box.pack(fill="x")
        detail_scroll = ttk.Scrollbar(detail_box, orient="vertical")
        detail_scroll.pack(side="right", fill="y")
        self._music_detail = tk.Text(
            detail_box, height=8, wrap="word",
            font=("Segoe UI", 9), bg=self.CARD_BG, fg=self.TEXT,
            insertbackground=self.TEXT, relief="flat", bd=0, padx=8, pady=6,
            state="disabled",
        )
        self._music_detail.pack(side="left", fill="both", expand=True)
        self._music_detail.configure(yscrollcommand=detail_scroll.set)
        detail_scroll.configure(command=self._music_detail.yview)
        self._music_detail.tag_configure("bad", foreground="#fca5a5")
        self._music_detail.tag_configure("warn", foreground="#fcd34d")
        self._music_detail.tag_configure("good", foreground="#86efac")

        # ---- 组 4：演奏控制 ----
        run_frame = ttk.LabelFrame(panel, text="  演奏控制  ", padding=12)
        run_frame.pack(fill="x")

        param_grid = tk.Frame(run_frame, bg=self.BG)
        param_grid.pack(fill="x", pady=(0, 8))
        for row_idx, (key, label, low, high) in enumerate(self._MUSIC_PARAM_ROWS):
            tk.Label(param_grid, text=label, font=("Segoe UI", 9), fg=self.TEXT,
                     bg=self.BG, anchor="w").grid(row=row_idx, column=0, sticky="w",
                                                  pady=2, padx=(0, 6))
            self._music_vars[key] = tk.StringVar(value=str(low))
            tk.Spinbox(param_grid, from_=low, to=high, width=8,
                       textvariable=self._music_vars[key],
                       font=("Segoe UI", 9, "bold"),
                       bg=self.CARD_BG, fg=self.TEXT, buttonbackground=self.ACCENT,
                       relief="flat", bd=0, justify="right"
                       ).grid(row=row_idx, column=1, sticky="e", pady=2)
        param_grid.columnconfigure(0, weight=1)

        self.btn_music_play = ttk.Button(run_frame, text="▶ 开始演奏",
                                         style="Primary.TButton",
                                         command=self._on_music_play, state="disabled")
        self.btn_music_play.pack(fill="x", pady=(0, 6))

        music_ctrl = tk.Frame(run_frame, bg=self.BG)
        music_ctrl.pack(fill="x", pady=(0, 6))
        self.btn_music_pause = ttk.Button(music_ctrl, text="⏸ 暂停",
                                          command=self._on_music_pause, state="disabled")
        self.btn_music_pause.pack(side="left", expand=True, fill="x", padx=(0, 4))
        self.btn_music_resume = ttk.Button(music_ctrl, text="▶ 继续",
                                           command=self._on_music_resume, state="disabled")
        self.btn_music_resume.pack(side="left", expand=True, fill="x", padx=4)
        self.btn_music_stop = ttk.Button(music_ctrl, text="⏹ 停止",
                                         style="Danger.TButton",
                                         command=self._on_music_stop, state="disabled")
        self.btn_music_stop.pack(side="left", expand=True, fill="x", padx=(4, 0))

        ttk.Button(run_frame, text="💾 保存演奏参数",
                   command=self._on_music_save_config).pack(fill="x", pady=(0, 8))

        ttk.Progressbar(run_frame, variable=self._music_progress, maximum=100,
                        mode="determinate").pack(fill="x")
        self._music_run_label = tk.Label(run_frame, textvariable=self._music_run_var,
                                         font=("Segoe UI", 9, "bold"),
                                         fg=self.TEXT_MUTED, bg=self.BG,
                                         anchor="w", justify="left", wraplength=300)
        self._music_run_label.pack(fill="x", pady=(4, 0))

        self._init_music_panel()

    def _init_music_panel(self):
        """从配置初始化第五栏，并读取曲谱库列表。"""
        try:
            cfg = self.js_api.get_music_config() or {}
        except Exception:
            cfg = {}
        self._music_bindings = list(cfg.get("bindings") or [])
        self._music_auto_shift.set(bool(cfg.get("auto_shift", True)))
        for key, _label, low, high in self._MUSIC_PARAM_ROWS:
            var = self._music_vars.get(key)
            if var is None:
                continue
            try:
                value = int(cfg.get(key, low))
            except (TypeError, ValueError):
                value = low
            var.set(str(max(low, min(high, value))))
        shift_var = self._music_vars.get("semitone_shift")
        if shift_var is not None:
            try:
                shift_var.set(str(int(cfg.get("semitone_shift", 0))))
            except (TypeError, ValueError):
                shift_var.set("0")
        music_dir = cfg.get("music_dir") or ""
        self._music_dir_var.set(f"曲谱目录：{music_dir}" if music_dir else "")
        self._refresh_music_badges(cfg)
        self.refresh_music_scores()

    def _refresh_music_badges(self, cfg: dict | None = None):
        """重绘键位徽章（音名 ▸ 按键）。"""
        if cfg is None:
            try:
                cfg = self.js_api.get_music_config() or {}
            except Exception:
                cfg = {}
        self._music_bindings = list(cfg.get("bindings") or [])
        for child in self._music_badges.winfo_children():
            child.destroy()

        if not self._music_bindings:
            tk.Label(self._music_badges, text="尚未绑定任何按键",
                     font=("Segoe UI", 9), fg=self.WARNING,
                     bg=self.BG).grid(row=0, column=0, sticky="w")
        else:
            for index, binding in enumerate(self._music_bindings):
                row, col = divmod(index, 3)
                pitch = binding.get("pitch")
                text = f"{note_name(pitch)} ▸ {binding.get('key', '?')}"
                tk.Label(self._music_badges, text=text,
                         font=("Segoe UI", 9, "bold"), fg="#ffffff",
                         bg=self.ACCENT, padx=6, pady=2
                         ).grid(row=row, column=col, sticky="ew", padx=2, pady=2)
            for col in range(3):
                self._music_badges.columnconfigure(col, weight=1)

        conflicts = cfg.get("layout_conflicts") or []
        range_text = cfg.get("layout_range") or "未绑定任何按键"
        if conflicts:
            range_text += f"\n⚠ 按键冲突：{', '.join(conflicts)}"
        self._music_range_var.set(f"音域：{range_text}")

    # ---- 曲谱库操作 ----

    def refresh_music_scores(self, select: str | None = None):
        """刷新曲谱列表（程序启动时自动调用一次）。"""
        try:
            scores = self.js_api.list_music_scores() or []
        except Exception:
            scores = []

        previous = self._selected_music()
        self._music_map.clear()
        self.music_list.delete(0, tk.END)
        for item in scores:
            name = item.get("name") if isinstance(item, dict) else str(item)
            if not name:
                continue
            self._music_map[name] = name
            self.music_list.insert(tk.END, name)

        target = select or previous
        if target and target in self._music_map:
            self._select_music(target)
        elif self.music_list.size():
            self._select_music(self.music_list.get(0))
        else:
            self._music_analysis = None
            self._set_music_check(None)

    def _select_music(self, name: str):
        index = self.music_list.get(0, tk.END)
        if name in index:
            self.music_list.selection_clear(0, tk.END)
            self.music_list.selection_set(index.index(name))
            self.music_list.see(index.index(name))
        self._analyze_music(name)

    def _selected_music(self) -> str | None:
        selection = self.music_list.curselection()
        if not selection:
            return None
        return self._music_map.get(self.music_list.get(selection[0]))

    def _on_music_selected(self):
        name = self._selected_music()
        if name:
            self._analyze_music(name)

    def _on_music_import(self):
        path = filedialog.askopenfilename(
            parent=self.root, title="导入 MIDI 曲谱",
            filetypes=[("MIDI 曲谱", "*.mid *.midi"), ("所有文件", "*.*")],
        )
        if not path:
            return
        try:
            name = self.js_api.import_music_score(path)
        except Exception as exc:
            messagebox.showerror("导入失败", str(exc))
            return
        if name:
            self.refresh_music_scores(select=name)

    def _on_music_delete(self):
        name = self._selected_music()
        if not name:
            push_toast("先选择一首曲谱", duration=2.0)
            return
        if not messagebox.askyesno("确认删除", f"确定从曲谱库删除「{name}」吗？\n（只删除副本，不影响原文件）"):
            return
        try:
            self.js_api.delete_music_score(name)
        except Exception as exc:
            messagebox.showerror("删除失败", str(exc))
            return
        self.refresh_music_scores()

    # ---- 检查与分析 ----

    def _current_shift(self) -> int:
        var = self._music_vars.get("semitone_shift")
        try:
            return max(-48, min(48, int(float(var.get()))))
        except (AttributeError, ValueError, tk.TclError):
            return 0

    def _analyze_music(self, name: str):
        """调用后端检查曲谱能否完整演奏，并刷新检查结果区。"""
        try:
            analysis = self.js_api.analyze_music_score(name, self._current_shift())
        except Exception as exc:
            analysis = {"name": name, "loaded": False, "ok": False,
                        "error": str(exc), "warnings": [str(exc)],
                        "missing": [], "missing_text": "", "summary": ""}
        self._music_analysis = analysis
        self._set_music_check(analysis)

    def _set_music_check(self, analysis: dict | None):
        """把检查结果写进状态行、摘要行与明细框。"""
        if not analysis:
            self._music_check_var.set("尚未检查")
            self._music_check_label.configure(foreground=self.TEXT_MUTED)
            self._music_summary_var.set("")
            self._set_music_detail("")
            self.btn_music_play.configure(state="disabled", text="▶ 开始演奏",
                                          style="Primary.TButton")
            return

        self._music_summary_var.set(analysis.get("summary") or "")
        warnings = list(analysis.get("warnings") or [])

        if not analysis.get("loaded"):
            self._music_check_var.set("❌ 曲谱无法读取")
            self._music_check_label.configure(foreground=self.DANGER)
            self._set_music_detail(analysis.get("error") or "曲谱无法读取", "bad")
            self.btn_music_play.configure(state="disabled", text="▶ 开始演奏",
                                          style="Primary.TButton")
            return

        if analysis.get("ok"):
            shift = int(analysis.get("shift") or 0)
            extra = f"（已自动移调 {shift:+d}）" if analysis.get("auto_shift_used") else ""
            self._music_check_var.set(f"✅ 可以演奏{extra}")
            self._music_check_label.configure(foreground=self.SUCCESS)
            detail = "\n".join(f"⚠ {line}" for line in warnings) if warnings else \
                "曲谱里的每个音都已绑定按键，可以直接演奏。"
            self._set_music_detail(detail, "warn" if warnings else "good")
            self.btn_music_play.configure(state="normal", text="▶ 开始演奏",
                                          style="Primary.TButton")
            return

        missing = analysis.get("missing") or []
        self._music_check_var.set(
            f"❌ 无法演奏：{len(missing)} 个音没有绑定按键"
            f"（{analysis.get('missing_notes', 0)} 个音符）")
        self._music_check_label.configure(foreground=self.DANGER)
        self._set_music_detail(
            "\n".join([analysis.get("missing_text") or ""] +
                      [f"⚠ {line}" for line in warnings]).strip(),
            "bad")
        # 按钮保持可点：点下去直接把「哪些音没绑定」摊开给用户看
        self.btn_music_play.configure(state="normal",
                                      text="⚠ 无法演奏 · 查看原因",
                                      style="Danger.TButton")

    def _set_music_detail(self, text: str, tag: str = ""):
        widget = self._music_detail
        widget.configure(state="normal")
        widget.delete("1.0", tk.END)
        if text:
            widget.insert(tk.END, text, tag)
        widget.configure(state="disabled")

    def _on_music_shift_change(self):
        name = self._selected_music()
        if name:
            self._analyze_music(name)

    def _collect_music_config(self) -> dict:
        """收集面板上的演奏参数（含移调与自动移调开关）。"""
        cfg: dict = {"auto_shift": bool(self._music_auto_shift.get()),
                     "semitone_shift": self._current_shift()}
        for key, _label, low, high in self._MUSIC_PARAM_ROWS:
            var = self._music_vars.get(key)
            if var is None:
                continue
            try:
                cfg[key] = max(low, min(high, int(float(var.get()))))
            except (ValueError, tk.TclError):
                cfg[key] = low
                var.set(str(low))
        return cfg

    def _on_music_save_config(self):
        try:
            ok = self.js_api.save_music_config(self._collect_music_config())
        except Exception as exc:
            messagebox.showerror("保存失败", str(exc))
            return
        push_toast("✅ 演奏参数已保存" if ok else "❌ 演奏参数保存失败", duration=2.5)

    # ---- 键位编辑 ----

    def _on_music_reset_bindings(self):
        if not messagebox.askyesno("恢复默认键位",
                                   "确定把键位绑定恢复成默认的洛克手碟九键吗？"):
            return
        try:
            cfg = self.js_api.reset_music_bindings() or {}
        except Exception as exc:
            messagebox.showerror("恢复失败", str(exc))
            return
        self._refresh_music_badges(cfg)
        name = self._selected_music()
        if name:
            self._analyze_music(name)

    def _on_music_edit_bindings(self):
        BindingEditorDialog(self, list(self._music_bindings),
                            self._on_bindings_edited)

    def _on_bindings_edited(self, bindings: list[dict]):
        """键位编辑对话框保存后的回调。"""
        try:
            ok = self.js_api.save_music_config({"bindings": bindings})
        except Exception as exc:
            messagebox.showerror("保存失败", str(exc))
            return
        if not ok:
            push_toast("❌ 键位保存失败", duration=3.0)
            return
        try:
            cfg = self.js_api.get_music_config() or {}
        except Exception:
            cfg = {}
        self._refresh_music_badges(cfg)
        name = self._selected_music()
        if name:
            self._analyze_music(name)

    # ---- 演奏控制 ----

    def _on_music_play(self):
        name = self._selected_music()
        if not name:
            push_toast("先选择一首曲谱", duration=2.0)
            return

        analysis = self._music_analysis
        if analysis is not None and not analysis.get("ok"):
            # 验收要求：不能演奏时必须明确指出是哪些音没有绑定按键
            self._show_missing_notes(analysis)
            return

        try:
            status = self.js_api.get_status() or {}
        except Exception:
            status = {}
        if status.get("script_running") or status.get("playback_active"):
            if not messagebox.askyesno(
                    "确认演奏",
                    "当前有脚本或回放在运行。\n开始演奏前会先停止它，且不保留进度。\n\n是否继续？"):
                return
            try:
                self.js_api.stop_current()
            except Exception:
                pass

        try:
            ok = self.js_api.start_music_play(name, self._collect_music_config())
        except Exception as exc:
            messagebox.showerror("演奏失败", str(exc))
            return
        if ok:
            self.root.after(200, self.refresh_status)

    def _show_missing_notes(self, analysis: dict):
        """弹框列出所有没有绑定按键的音。"""
        missing = analysis.get("missing") or []
        lines = [f"曲谱：{analysis.get('name', '')}",
                 f"{analysis.get('summary', '')}", "",
                 f"以下 {len(missing)} 个音没有绑定按键："]
        lines.extend(f"  · {item.get('describe', '')}" for item in missing)
        suggestion = analysis.get("shift_suggestion")
        if suggestion is not None:
            lines += ["", f"把移调改成 {suggestion:+d} 半音即可完整演奏。"]
        else:
            lines += ["", "没有任何移调量能完整演奏这首曲谱，",
                      "请在「编辑键位」里为上面这些音补上按键。"]
        messagebox.showwarning("无法演奏", "\n".join(lines))

    def _on_music_pause(self):
        try:
            self.js_api.pause_current()
        except Exception as exc:
            messagebox.showerror("暂停失败", str(exc))

    def _on_music_resume(self):
        try:
            self.js_api.resume_current()
        except Exception as exc:
            messagebox.showerror("继续失败", str(exc))

    def _on_music_stop(self):
        try:
            self.js_api.stop_music_play()
        except Exception as exc:
            messagebox.showerror("停止失败", str(exc))

    def _update_music_ui(self, status: dict):
        """同步第五栏的运行状态、进度与按钮可用性。"""
        try:
            music = status.get("music") or {}
            playing = bool(music.get("playing"))
            paused = bool(music.get("paused"))
            counting_down = bool(music.get("counting_down"))
            busy = playing or counting_down

            analysis_ok = bool(self._music_analysis and self._music_analysis.get("ok"))
            play_text = "▶ 开始演奏" if analysis_ok else "⚠ 无法演奏 · 查看原因"
            self.btn_music_play.configure(
                state="disabled" if busy or not self._music_analysis else "normal",
                text=play_text,
                style="Primary.TButton" if analysis_ok else "Danger.TButton",
            )
            self.btn_music_pause.configure(
                state="normal" if playing and not paused else "disabled")
            self.btn_music_resume.configure(
                state="normal" if playing and paused else "disabled")
            self.btn_music_stop.configure(state="normal" if busy else "disabled")

            position = float(music.get("position") or 0)
            duration = float(music.get("duration") or 0)
            if playing and duration > 0:
                self._music_progress.set(min(100.0, position / duration * 100))
            elif not busy:
                self._music_progress.set(0)

            if counting_down:
                self._music_run_var.set(f"⏳ 即将开始演奏：{music.get('name') or ''}")
                color = self.WARNING
            elif playing:
                loop_total = int(music.get("loop_total") or 0)
                loop_text = ("无限" if loop_total == 0 else str(loop_total))
                self._music_run_var.set(
                    f"{'⏸' if paused else '●'} 演奏中 · "
                    f"{int(position)}s / {int(duration)}s · "
                    f"第 {int(music.get('loop_current') or 1)} 轮 / {loop_text} · "
                    f"按键 {int(music.get('event_index') or 0)}/{int(music.get('event_total') or 0)}")
                color = self.WARNING if paused else self.SUCCESS
            else:
                self._music_run_var.set("未运行")
                color = self.TEXT_MUTED
            self._music_run_label.configure(foreground=color)
        except Exception:
            pass

    # ── 刷新逻辑 ──────────────────────────────────

    def refresh_scripts(self):
        """刷新统一脚本列表：动作脚本 + 录制脚本，带类型标签。

        用 self._script_map 维护「显示名 → (stem, type)」映射，
        避免后续靠字符串分割还原脚本名。
        """
        # 获取动作脚本
        try:
            action_scripts = self.js_api.list_scripts() or []
        except Exception:
            action_scripts = []

        # 获取录制脚本
        try:
            recorded_scripts = self.js_api.list_recorded_scripts() or []
        except Exception:
            recorded_scripts = []

        # 记录当前选中 stem，刷新后恢复
        prev_sel = self._selected_script()
        prev_stem = prev_sel[0] if prev_sel else None

        # 抑制选择事件，避免刷新期间触发切换确认弹窗
        self._suppress_select_event = True
        try:
            self._script_map.clear()
            self.script_list.delete(0, tk.END)

            # 先显示动作脚本（⚙）
            for stem in action_scripts:
                display = f"⚙  {stem}"
                self._script_map[display] = (stem, "action")
                self.script_list.insert(tk.END, display)

            # 再显示录制脚本（🎙）
            for stem in recorded_scripts:
                display = f"🎙  {stem}"
                self._script_map[display] = (stem, "recorded")
                self.script_list.insert(tk.END, display)

            # 恢复选中状态（按 stem 匹配）
            if prev_stem:
                for display, (stem, _t) in self._script_map.items():
                    if stem == prev_stem:
                        try:
                            idx = self.script_list.get(0, tk.END).index(display)
                            self.script_list.selection_set(idx)
                            self.script_list.see(idx)
                        except (ValueError, Exception):
                            pass
                        break
        finally:
            self._suppress_select_event = False

    def refresh_status(self):
        try:
            status = self.js_api.get_status() or {}
        except Exception:
            status = {}

        running = bool(status.get("running"))

        sel = self._selected_script()
        # 直接用映射里的 stem
        sel_stem = sel[0] if sel else None
        self.selected_script_var.set(sel_stem if sel_stem else "(未选择)")

        script_running = bool(status.get("script_running"))
        script_paused = bool(status.get("script_paused"))
        music_status = status.get("music") or {}
        music_playing = bool(music_status.get("playing"))
        music_paused = bool(music_status.get("paused"))

        countdown = status.get("countdown")
        countdown_label = status.get("countdown_label")
        if countdown is not None and countdown > 0:
            label = countdown_label or "即将启动"
            status_text = f"{label} ({countdown}s)"
            status_color = self.WARNING
        elif music_playing and music_paused:
            status_text = "演奏已暂停"
            status_color = self.WARNING
        elif music_playing:
            status_text = f"MIDI 演奏中：{music_status.get('name') or ''}".rstrip("：")
            status_color = self.SUCCESS
        elif script_running and script_paused:
            status_text = "脚本已暂停"
            status_color = self.WARNING
        elif script_running:
            status_text = "脚本运行中"
            status_color = self.SUCCESS
        elif running:
            status_text = "连点器运行中"
            status_color = self.SUCCESS
        else:
            status_text = "已停止"
            status_color = self.TEXT_MUTED

        self.status_var.set(status_text)
        try:
            self.status_label.configure(foreground=status_color)
        except Exception:
            pass
        self.move_mouse_var.set(bool(status.get("move_mouse", False)))

        # 录制 UI 同步
        recording = bool(status.get("recording"))
        self._update_rec_ui(recording=recording)
        rec_name = status.get("recording_name")
        if rec_name:
            self.recording_name_var.set(rec_name)

        # 进度条更新
        playback_active = bool(status.get("playback_active"))
        playback_paused = bool(status.get("playback_paused"))
        progress_info = status.get("playback_progress", "")
        loop_label = status.get("playback_loop_label", "")
        if playback_active and progress_info:
            display_text = f"{progress_info}  {loop_label}".strip() if loop_label else progress_info
            self.progress_label.config(text=display_text)
            self.progress_bar.config(mode="determinate")
            try:
                parts = progress_info.split("/")
                if len(parts) == 2:
                    current = float(parts[0].strip().split()[-1])
                    total = float(parts[1].strip())
                    self.progress_var.set(current / total * 100)
            except Exception:
                self.progress_var.set(0)
        elif playback_active:
            self.progress_label.config(text=f"回放中…  {loop_label}".strip())
            self.progress_bar.config(mode="indeterminate")
            self.progress_bar.start(200)
        else:
            self.progress_label.config(text="")
            self.progress_bar.config(mode="determinate")
            self.progress_var.set(0)
            if playback_active is False:
                self.progress_bar.stop()

        # 循环进度标签
        if playback_active and loop_label:
            self.loop_progress_label.config(text=f"🔄 {loop_label}", foreground=self.ACCENT)
        else:
            self.loop_progress_label.config(text="")

        # 回放时禁用循环配置输入（防止用户中途修改）
        try:
            if playback_active:
                self.loop_count_spin.configure(state="disabled", fg="#64748b")
                self.loop_delay_entry.configure(state="disabled", fg="#64748b")
            else:
                self.loop_count_spin.configure(state="normal", fg=self.TEXT)
                self.loop_delay_entry.configure(state="normal", fg=self.TEXT)
        except Exception:
            pass

        # ---- 第四栏：窗口区域连点状态 ----
        self._update_region_ui(status)

        # ---- 第五栏：MIDI 自动演奏状态 ----
        self._update_music_ui(status)

        # ---- 暂停/继续/停止 按钮状态管理 ----
        # 判断当前是否有东西在运行（脚本、回放或 MIDI 演奏）
        something_running = script_running or playback_active or music_playing
        something_paused = ((script_running and script_paused) or playback_paused
                            or music_paused)

        if something_paused:
            # 暂停状态：继续可用，暂停禁用，停止可用
            self.btn_pause.configure(state="disabled")
            self.btn_resume.configure(state="normal")
            self.btn_stop.configure(state="normal")
        elif something_running:
            # 运行中（未暂停）：暂停可用，继续禁用，停止可用
            self.btn_pause.configure(state="normal")
            self.btn_resume.configure(state="disabled")
            self.btn_stop.configure(state="normal")
        else:
            # 没有运行：全部禁用
            self.btn_pause.configure(state="disabled")
            self.btn_resume.configure(state="disabled")
            self.btn_stop.configure(state="disabled")

        # ---- 热键配置 ----
        hotkeys = status.get("hotkeys", {})
        if not hotkeys:
            # 尝试直接从 API 获取
            try:
                hotkeys = self.js_api.get_hotkeys() or {}
            except Exception:
                hotkeys = {}

        # ---- 更新按钮 label，标注对应热键 ----
        pause_key = hotkeys.get("pause_resume", "F2")
        rec_key = hotkeys.get("start_recording", "F7")
        stop_key = hotkeys.get("stop_recording", "F8")
        cancel_key = hotkeys.get("cancel_recording", "F9")
        try:
            self.btn_pause.configure(text=f"⏸ 暂停 ({pause_key})")
            self.btn_resume.configure(text=f"▶ 继续 ({pause_key})")
            self.btn_rec_start.configure(text=f"● 录制 ({rec_key})")
            self.btn_rec_stop.configure(text=f"■ 保存 ({stop_key})")
            self.btn_rec_cancel.configure(text=f"✕ 取消 ({cancel_key})")
            self._rc_record_btn.configure(
                text=f"🎙 录制区域 ({hotkeys.get('record_region', 'F6')})")
        except Exception:
            pass

        # 同步下拉框：有未保存的修改（脏标记）或下拉框有焦点时跳过，避免覆盖用户选择
        focus_widget = None
        try:
            focus_widget = self.root.focus_get()
        except Exception:
            pass
        for key_name, var in self._hotkey_vars.items():
            combo = self._hotkey_combos.get(key_name)
            # 脏标记为 True（有未保存修改）或下拉框有焦点时跳过同步
            if self._hotkey_dirty:
                continue
            if combo is not None and focus_widget is combo:
                continue
            current_val = hotkeys.get(key_name, "")
            if current_val and var.get() != current_val:
                try:
                    var.set(current_val)
                except Exception:
                    pass

        # 更新热键显示区（彩色按键徽章）
        for key_name in self._hotkey_labels:
            key_val = hotkeys.get(key_name, "?")
            badge = self._hotkey_badges.get(key_name)
            if badge:
                try:
                    badge.configure(text=key_val)
                except Exception:
                    pass

        # 更新底部快捷键提示
        region_key = hotkeys.get("record_region", "F6")
        self.footer_var.set(
            f"快捷键：{pause_key} 暂停/继续  |  {rec_key} 开始录制  |  "
            f"{stop_key} 停止录制并保存  |  {cancel_key} 取消录制  |  "
            f"{region_key} 录制窗口区域"
        )

    def refresh_all(self):
        self.refresh_scripts()
        self.refresh_music_scores()
        self.refresh_status()

    def _refresh_loop(self):
        self.refresh_status()
        # 脚本列表每 2 秒刷新一次（避免频繁刷新干扰用户操作）
        self._refresh_counter = getattr(self, '_refresh_counter', 0) + 1
        if self._refresh_counter >= 4:
            self._refresh_counter = 0
            self.refresh_scripts()
            self._refresh_region_presets()
        self._drain_toasts()
        self._drain_region_requests()
        self.root.after(500, self._refresh_loop)

    def _drain_region_requests(self):
        """排空后台线程投递的区域录制请求（Tk 窗口只能在主线程创建）。"""
        while True:
            try:
                mode = self.js_api.pop_region_request()
            except Exception:
                return
            if not mode:
                return
            self._start_region_capture(mode)

    def _drain_toasts(self):
        while True:
            try:
                text, duration = _toast_queue.get_nowait()
                ToastWindow(self.root, text, duration)
            except queue.Empty:
                break

    def run(self):
        self.root.mainloop()


def create_window(title, url=None, js_api=None, width=1680, height=920):
    global _window_config
    _window_config = {
        "title": title,
        "js_api": js_api,
        "width": width,
        "height": height,
    }
    return _window_config


def start():
    if not _window_config:
        raise RuntimeError("No window has been created")
    if _window_config["js_api"] is None:
        raise RuntimeError("js_api is required")

    app = _DesktopWindow(
        title=_window_config["title"],
        js_api=_window_config["js_api"],
        width=_window_config["width"],
        height=_window_config["height"],
    )
    app.run()
