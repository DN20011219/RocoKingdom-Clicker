from __future__ import annotations

import json
import threading
import tkinter as tk
from tkinter import ttk, messagebox
import queue


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
        self.root.minsize(1400, 860)
        self.root.configure(bg=self.BG)

        self.status_var = tk.StringVar(value="准备就绪")
        self.move_mouse_var = tk.BooleanVar(value=False)

        # 显示名 → (stem, type) 映射，避免靠字符串分割还原脚本名
        # type: "action" 或 "recorded"
        self._script_map: dict[str, tuple[str, str]] = {}

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
        body.columnconfigure(2, weight=2)
        body.rowconfigure(0, weight=1)

        # ── 左栏：控制面板 ─────────────────────────
        left = ttk.LabelFrame(body, text="  控制  ", padding=16)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 12))

        # 当前选择脚本（突出显示）
        ttk.Label(left, text="当前选择脚本", font=("Segoe UI", 9, "bold"),
                  foreground=self.TEXT_MUTED).pack(anchor="w", pady=(0, 6))
        self.selected_script_var = tk.StringVar(value="(未选择)")
        sel_frame = tk.Frame(left, bg=self.CARD_BG, bd=0,
                             highlightbackground=self.ACCENT,
                             highlightthickness=2)
        sel_frame.pack(fill="x", pady=(0, 16))
        tk.Label(sel_frame, textvariable=self.selected_script_var,
                 font=("Segoe UI", 13, "bold"), foreground=self.TEXT,
                 bg=self.CARD_BG, padx=14, pady=10, anchor="w",
                 justify="left").pack(fill="x")

        # 状态
        ttk.Label(left, text="状态", font=("Segoe UI", 9, "bold"),
                  foreground=self.TEXT_MUTED).pack(anchor="w", pady=(0, 6))
        self.status_label = tk.Label(
            left, textvariable=self.status_var,
            font=("Segoe UI", 12, "bold"), foreground=self.SUCCESS,
            bg=self.BG, anchor="w",
        )
        self.status_label.pack(anchor="w", pady=(0, 16))

        # 控制自定义脚本鼠标移动
        move_row = tk.Frame(left, bg=self.BG)
        move_row.pack(fill="x", pady=(0, 10))
        self.move_mouse_check = tk.Checkbutton(
            move_row,
            text="控制自定义脚本鼠标移动（不影响录制脚本）",
            variable=self.move_mouse_var, command=self.on_toggle_move_mouse,
            wraplength=320, justify="left",
            bg=self.BG, fg=self.TEXT, selectcolor=self.CARD_BG,
            activebackground=self.BG, activeforeground=self.TEXT,
            highlightthickness=0, bd=0,
        )
        self.move_mouse_check.pack(anchor="w")

        # ── 录制区域 ──────────────────────────────
        sep = ttk.Separator(left, orient="horizontal")
        sep.pack(fill="x", pady=(16, 12))

        rec_header = tk.Frame(left, bg=self.BG)
        rec_header.pack(fill="x", pady=(0, 8))
        tk.Label(rec_header, text="输入录制",
                 font=("Segoe UI", 10, "bold"), fg=self.ACCENT,
                 bg=self.BG).pack(side="left")
        self.recording_indicator = tk.Label(
            rec_header, text="", foreground=self.DANGER,
            font=("Segoe UI", 9, "bold"), bg=self.BG,
        )
        self.recording_indicator.pack(side="right")

        self.recording_name_var = tk.StringVar(value="")
        self.recording_entry = ttk.Entry(left, textvariable=self.recording_name_var,
                                         font=("Segoe UI", 10))
        self.recording_entry.insert(0, "recording")
        self.recording_entry.pack(fill="x", pady=(0, 4))
        ttk.Label(left, text="录制脚本名（可编辑）",
                  foreground=self.TEXT_MUTED,
                  font=("Segoe UI", 8)).pack(anchor="w", pady=(0, 8))

        rec_btns = ttk.Frame(left)
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
                                         command=self.on_rec_cancel, state="disabled")
        self.btn_rec_cancel.grid(row=0, column=2, sticky="ew", padx=(4, 0))

        # ── 中栏：脚本列表（统一） ─────────────────
        center = ttk.LabelFrame(body, text="  脚本列表（双击执行）  ", padding=16)
        center.grid(row=0, column=1, sticky="nsew", padx=(0, 12))
        center.rowconfigure(0, weight=1)
        center.rowconfigure(2, weight=0)
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

        # 列表操作按钮
        script_btns = ttk.Frame(center)
        script_btns.grid(row=2, column=0, sticky="ew", pady=(12, 0))
        script_btns.columnconfigure(0, weight=1)
        script_btns.columnconfigure(1, weight=1)
        script_btns.columnconfigure(2, weight=1)
        ttk.Button(script_btns, text="▶ 执行", style="Primary.TButton",
                   command=self._on_play_selected).grid(row=0, column=0, sticky="ew", padx=(0, 6))
        ttk.Button(script_btns, text="🗑 删除",
                   command=self._on_delete_selected).grid(row=0, column=1, sticky="ew", padx=6)
        ttk.Button(script_btns, text="↻ 刷新",
                   command=self.refresh_all).grid(row=0, column=2, sticky="ew", padx=(6, 0))

        # ── 右栏：运行状态 ─────────────────────────
        right = ttk.LabelFrame(body, text="  运行状态  ", padding=16)
        right.grid(row=0, column=2, sticky="nsew")
        right.rowconfigure(0, weight=1)
        right.columnconfigure(0, weight=1)

        self.config_text_widget = tk.Text(
            right, height=28, width=30, wrap="word",
            relief="flat", bg=self.CARD_BG, fg=self.TEXT,
            font=("Consolas", 10), bd=0,
            highlightthickness=0, insertbackground=self.TEXT,
            padx=12, pady=12,
        )
        self.config_text_widget.grid(row=0, column=0, sticky="nsew")
        self.config_text_widget.configure(state="disabled")

        footer = ttk.Frame(self.root, padding=(22, 0, 22, 16))
        footer.pack(fill="x")
        ttk.Label(
            footer,
            text="快捷键：F1 继续脚本  |  F2 暂停脚本/回放  |  F7 开始录制  |  F8 停止录制并保存  |  F9 取消录制",
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
        try:
            sel = self._selected_script()
            self.selected_script_var.set(sel[0] if sel else "(未选择)")
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
        try:
            # 立即弹 toast 反馈，让用户知道点击生效了
            try:
                from webview import push_toast
                push_toast(f"▶ 点击执行: {stem}", duration=2.0)
            except Exception:
                pass
            self.js_api.play_recording(stem)
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
                    idx = self.script_list.get(0, tk.END).index(display)
                    self.script_list.selection_set(idx)
                    self.script_list.see(idx)
                    break

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

        countdown = status.get("countdown")
        countdown_label = status.get("countdown_label")
        if countdown is not None and countdown > 0:
            label = countdown_label or "即将启动"
            status_text = f"{label} ({countdown}s)"
            status_color = self.WARNING
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
        progress_info = status.get("playback_progress", "")
        if playback_active and progress_info:
            self.progress_label.config(text=progress_info)
            self.progress_bar.config(mode="determinate")
            # progress_info 格式: "事件 42 / 128"
            try:
                parts = progress_info.split("/")
                if len(parts) == 2:
                    current = float(parts[0].strip().split()[-1])
                    total = float(parts[1].strip())
                    self.progress_var.set(current / total * 100)
            except Exception:
                self.progress_var.set(0)
        elif playback_active:
            self.progress_label.config(text="回放中…")
            self.progress_bar.config(mode="indeterminate")
            self.progress_bar.start(200)
        else:
            self.progress_label.config(text="")
            self.progress_bar.config(mode="determinate")
            self.progress_var.set(0)
            if playback_active is False:
                self.progress_bar.stop()

        # 运行状态面板（显示回放/录制/连点器的关键信息）
        lines = []
        lines.append(f"【状态】{status_text}")
        lines.append("")

        # 回放信息
        playback_active = bool(status.get("playback_active"))
        if playback_active:
            lines.append("【回放】")
            progress_info = status.get("playback_progress", "")
            if progress_info:
                lines.append(f"  进度: {progress_info}")
            pb_paused = bool(status.get("playback_paused"))
            lines.append(f"  暂停: {'是（F2 恢复）' if pb_paused else '否'}")
            lines.append("")
        else:
            lines.append("【回放】未运行")
            lines.append("")

        # 录制信息
        if recording:
            lines.append("【录制】● 进行中")
            rec_count = status.get("recording_event_count")
            if rec_count is not None:
                lines.append(f"  已捕获: {rec_count} 条")
            lines.append("  F8 保存 / F9 取消")
            lines.append("")
        else:
            lines.append("【录制】未运行")
            lines.append("")

        # 连点器信息（精简）
        if running:
            lines.append("【连点器】运行中")
            ci = status.get("click_interval")
            if ci is not None:
                lines.append(f"  间隔: {ci} ms")
            mm = status.get("move_mouse")
            if mm:
                lines.append("  鼠标移动: 已启用")
        else:
            lines.append("【连点器】未运行")

        config_text = "\n".join(lines)
        self.config_text_widget.configure(state="normal")
        self.config_text_widget.delete("1.0", tk.END)
        self.config_text_widget.insert("1.0", config_text)
        self.config_text_widget.configure(state="disabled")

    def refresh_all(self):
        self.refresh_scripts()
        self.refresh_status()

    def _refresh_loop(self):
        self.refresh_status()
        self.refresh_scripts()  # 刷新列表（新录制的脚本要及时出现）
        self._drain_toasts()
        self.root.after(500, self._refresh_loop)  # 500ms 刷新一次，toast 更及时

    def _drain_toasts(self):
        while True:
            try:
                text, duration = _toast_queue.get_nowait()
                ToastWindow(self.root, text, duration)
            except queue.Empty:
                break

    def run(self):
        self.root.mainloop()


def create_window(title, url=None, js_api=None, width=1480, height=900):
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
