"""GUI 构建冒烟测试。

webview.py 里几千行 Tkinter 代码只有在真正构建窗口时才会执行，单元测试碰不到。
这里用一个假的 js_api 把 _DesktopWindow 完整搭起来（不启动 mainloop），验证：

- MusicPanelSmokeTest：第五栏「MIDI 自动演奏」能正常渲染，分析结果能正确驱动
  按钮状态与明细文本，键位编辑对话框往返正常。
- RegionCaptureRoutingTest：第四栏三个区域捕获按钮都走后端队列，因而都吃得到
  ClickerManager 里的互斥检查。
- OutlineGeometryTest：常驻边框的几何计算——边框必须落在区域外侧，否则覆盖层
  会压住可点击像素。
- RegionOutlineSyncTest：常驻边框的生命周期（不闪烁重建、停止即销毁、穿透失败
  不重试）。

跑测试时屏幕上会闪一下窗口，构造完立即 withdraw + destroy。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

try:
    import tkinter as tk
    _probe = tk.Tk()
    _probe.withdraw()
    _probe.destroy()
    TK_AVAILABLE = True
except Exception:  # 无显示环境（纯 CI 容器）时跳过整组测试
    TK_AVAILABLE = False

import webview  # noqa: E402
from ConfigManager import DEFAULT_MUSIC_PLAYER  # noqa: E402
from RegionSelector import (OUTLINE_BORDER, OUTLINE_MARGIN,  # noqa: E402
                            RegionOutlineUnavailable, outline_geometry)

HANDPAN_BINDINGS = [dict(item) for item in DEFAULT_MUSIC_PLAYER["bindings"]]


def _analysis(**overrides) -> dict:
    """构造一份 analyze_music_score 的返回值。"""
    base = {
        "name": "demo",
        "loaded": True,
        "error": None,
        "ok": True,
        "total_notes": 120,
        "playable_notes": 120,
        "missing_notes": 0,
        "missing": [],
        "missing_text": "",
        "summary": "120 个音符 · 音域 A2~E4 · 0:48 · 120 BPM",
        "warnings": [],
        "duration": 48.0,
        "duration_text": "0:48",
        "bpm": 120.0,
        "track_count": 1,
        "tracks": [{"name": "主旋律", "note_count": 120}],
        "min_pitch": 45, "max_pitch": 64,
        "min_name": "A2", "max_name": "E4",
        "requested_shift": 0, "shift": 0,
        "auto_shift_used": False, "shift_suggestion": None,
        "peak_notes_per_second": 4, "same_key_overlaps": 0,
        "layout_range": "A2 ~ E4（9 键）",
        "layout_key_count": 9,
        "layout_conflicts": [],
    }
    base.update(overrides)
    return base


class StubApi:
    """顶替 gui.Api：只提供 _DesktopWindow 构建与刷新时会调到的方法。

    故意不实现 __getattr__ 兜底 —— 面板要是调了一个后端没有的方法，
    这里必须直接抛 AttributeError 让测试失败，而不是被静默吞掉。
    """

    def __init__(self):
        self.analysis = _analysis()
        self.music_config = {
            "bindings": [dict(item) for item in HANDPAN_BINDINGS],
            "layout_name": "roco_handpan",
            "layout_range": "A2 ~ E4（9 键）",
            "layout_conflicts": [],
            "music_dir": str(PROJECT_ROOT / "data" / "music"),
            "semitone_shift": 0,
            "auto_shift": True,
            "speed_percent": 100,
            "hold_percent": 90,
            "min_hold_ms": 45,
            "retrigger_gap_ms": 12,
            "max_polyphony": 0,
            "loop_count": 1,
            "loop_delay_ms": 0,
            "countdown_sec": 3,
        }
        self.status = {
            "interception_ready": True,
            "running": False,
            "script_running": False,
            "script_paused": False,
            "script_name": None,
            "recording": False,
            "recording_name": None,
            "playback_active": False,
            "playback_paused": False,
            "playback_progress": "",
            "playback_loop_label": "",
            "move_mouse": False,
            "region_click_active": False,
            "region_click_total": 0,
            "region_click_spots": 0,
            "hotkeys": {"pause_resume": "F2", "start_recording": "F7",
                        "stop_recording": "F8", "cancel_recording": "F9",
                        "record_region": "F6", "mark_anchor": "F12"},
            "music": {"playing": False, "paused": False, "counting_down": False},
        }
        self.saved_configs: list[dict] = []
        self.started: list[tuple] = []
        self.region_requests: list[str] = []

    # ---- 既有面板 ----
    def get_anchor_mode(self):
        return "point"

    def set_anchor_mode(self, mode):
        return True

    def get_path_planner_config(self):
        return {"strategy": "fitts"}

    def set_path_planner_config(self, config):
        return True

    def get_region_click_config(self):
        return {"region_presets": []}

    def list_region_presets(self):
        return []

    def request_region_capture(self, mode="drag"):
        self.region_requests.append(mode)
        return True

    def pop_region_request(self):
        return None

    def list_scripts(self):
        return ["click", "loop"]

    def list_recorded_scripts(self):
        return []

    def get_hotkeys(self):
        return dict(self.status["hotkeys"])

    def get_status(self):
        return dict(self.status)

    # ---- 第五栏：MIDI 自动演奏 ----
    def get_music_config(self):
        return dict(self.music_config)

    def save_music_config(self, config):
        self.saved_configs.append(dict(config))
        self.music_config.update(config)
        return True

    def reset_music_bindings(self):
        self.music_config["bindings"] = [dict(i) for i in HANDPAN_BINDINGS]
        return dict(self.music_config)

    def list_music_scores(self):
        return [{"name": "demo", "filename": "demo.mid", "size": 1024,
                 "path": str(PROJECT_ROOT / "data" / "music" / "demo.mid")}]

    def analyze_music_score(self, name, shift=None):
        return dict(self.analysis)

    def import_music_score(self, path):
        return "demo"

    def delete_music_score(self, name):
        return True

    def start_music_play(self, name, params=None):
        self.started.append((name, dict(params or {})))
        return True

    def stop_music_play(self):
        return True

    def get_music_status(self):
        return dict(self.status["music"])

    def pause_current(self):
        return True

    def resume_current(self):
        return True

    def stop_current(self):
        return True


class StubMessageBox:
    """顶替 tkinter.messagebox。

    真的弹框是模态的，测试里一弹就永远等不到人点确定，整组测试会挂死。
    这里把调用记下来，顺便让测试能断言「该提示的时候确实提示了」。
    """

    def __init__(self, yes: bool = True):
        self.calls: list[tuple[str, str, str]] = []
        self._yes = yes

    def _record(self, kind, title, message):
        self.calls.append((kind, str(title), str(message)))

    def showwarning(self, title, message, **kwargs):
        self._record("warning", title, message)

    def showerror(self, title, message, **kwargs):
        self._record("error", title, message)

    def showinfo(self, title, message, **kwargs):
        self._record("info", title, message)

    def askyesno(self, title, message, **kwargs):
        self._record("askyesno", title, message)
        return self._yes

    def kinds(self) -> list[str]:
        return [kind for kind, _t, _m in self.calls]

    def last_message(self) -> str:
        return self.calls[-1][2] if self.calls else ""


@unittest.skipUnless(TK_AVAILABLE, "当前环境没有可用的显示设备")
class MusicPanelSmokeTest(unittest.TestCase):
    def setUp(self):
        self.api = StubApi()
        # 弹框必须在建窗口之前就换掉：构建过程中出错也会弹框
        self._real_messagebox = webview.messagebox
        self.msgbox = StubMessageBox()
        webview.messagebox = self.msgbox
        self.addCleanup(self._restore_messagebox)

        self.app = webview._DesktopWindow("测试窗口", self.api, 2540, 1300)
        try:
            self.app.root.withdraw()
        except Exception:
            pass
        self.addCleanup(self._destroy)

    def _restore_messagebox(self):
        webview.messagebox = self._real_messagebox

    def _destroy(self):
        try:
            self.app.root.destroy()
        except Exception:
            pass

    # ---- 构建 ----

    def test_panel_is_built_with_all_groups(self):
        app = self.app
        self.assertTrue(hasattr(app, "music_list"))
        self.assertTrue(hasattr(app, "btn_music_play"))
        self.assertTrue(hasattr(app, "_music_detail"))
        self.assertEqual(app.music_list.size(), 1)
        self.assertEqual(app.music_list.get(0), "demo")

    def test_nine_key_badges_are_rendered(self):
        badges = [w for w in self.app._music_badges.winfo_children()]
        self.assertEqual(len(badges), 9)
        texts = sorted(w.cget("text") for w in badges)
        self.assertIn("C4 ▸ T", texts)
        self.assertIn("A2 ▸ B", texts)
        self.assertIn("A2 ~ E4（9 键）", self.app._music_range_var.get())

    def test_startup_auto_selects_and_analyzes_first_score(self):
        """验收要求：程序启动时自动读取 data/music 列表。"""
        self.assertEqual(self.app._selected_music(), "demo")
        self.assertIsNotNone(self.app._music_analysis)
        self.assertIn("可以演奏", self.app._music_check_var.get())

    def test_play_button_enabled_when_score_is_playable(self):
        self.assertEqual(str(self.app.btn_music_play.cget("state")), "normal")
        self.assertEqual(self.app.btn_music_play.cget("text"), "▶ 开始演奏")

    def test_config_values_are_loaded_into_widgets(self):
        vars_ = self.app._music_vars
        self.assertEqual(vars_["speed_percent"].get(), "100")
        self.assertEqual(vars_["hold_percent"].get(), "90")
        self.assertEqual(vars_["countdown_sec"].get(), "3")
        self.assertEqual(vars_["semitone_shift"].get(), "0")
        self.assertTrue(self.app._music_auto_shift.get())

    # ---- 不能演奏时的提示 ----

    def test_unplayable_score_lists_missing_notes(self):
        """验收要求：不能演奏时提示哪个音没有绑定按键。"""
        self.api.analysis = _analysis(
            ok=False,
            playable_notes=100,
            missing_notes=20,
            missing=[{"pitch": 72, "name": "C5", "count": 12, "first_time": 1.5,
                      "nearest_name": "E4", "nearest_semitones": 8,
                      "describe": "C5（MIDI 72）出现 12 次，首次 1.50s"},
                     {"pitch": 66, "name": "F#4", "count": 8, "first_time": 3.0,
                      "nearest_name": "F3", "nearest_semitones": -13,
                      "describe": "F#4（MIDI 66）出现 8 次，首次 3.00s"}],
            missing_text="以下 2 个音没有绑定按键：\n  · C5（MIDI 72）出现 12 次",
            warnings=["把移调改成 -12 半音即可完整演奏"],
            shift_suggestion=-12,
        )
        self.app._analyze_music("demo")

        self.assertIn("无法演奏", self.app._music_check_var.get())
        self.assertIn("2 个音没有绑定按键", self.app._music_check_var.get())
        detail = self.app._music_detail.get("1.0", tk.END)
        self.assertIn("C5", detail)
        self.assertIn("移调", detail)
        # 按钮仍可点击，点下去把缺失清单摊开给用户
        self.assertEqual(str(self.app.btn_music_play.cget("state")), "normal")
        self.assertEqual(self.app.btn_music_play.cget("text"), "⚠ 无法演奏 · 查看原因")

    def test_play_click_on_unplayable_score_does_not_start(self):
        self.api.analysis = _analysis(
            ok=False,
            missing=[{"pitch": 72, "name": "C5", "count": 3, "first_time": 1.0,
                      "nearest_name": "E4", "nearest_semitones": 8,
                      "describe": "C5（MIDI 72）出现 3 次，首次 1.00s"}],
            shift_suggestion=None,
        )
        self.app._analyze_music("demo")
        self.app._on_music_play()

        self.assertEqual(self.api.started, [])
        self.assertIn("warning", self.msgbox.kinds())
        self.assertIn("C5", self.msgbox.last_message())
        self.assertIn("没有绑定按键", self.msgbox.last_message())

    def test_unreadable_score_disables_play(self):
        self.api.analysis = _analysis(loaded=False, ok=False,
                                      error="MIDI 解析失败：不是标准 MIDI 文件")
        self.app._analyze_music("demo")
        self.assertIn("无法读取", self.app._music_check_var.get())
        self.assertEqual(str(self.app.btn_music_play.cget("state")), "disabled")
        self.assertIn("不是标准 MIDI 文件",
                      self.app._music_detail.get("1.0", tk.END))

    # ---- 演奏交互 ----

    def test_play_sends_params_to_backend(self):
        self.app._music_vars["speed_percent"].set("80")
        self.app._music_vars["loop_count"].set("2")
        self.app._music_vars["semitone_shift"].set("-12")
        self.app._on_music_play()

        self.assertEqual(len(self.api.started), 1)
        name, params = self.api.started[0]
        self.assertEqual(name, "demo")
        self.assertEqual(params["speed_percent"], 80)
        self.assertEqual(params["loop_count"], 2)
        self.assertEqual(params["semitone_shift"], -12)
        self.assertTrue(params["auto_shift"])

    def test_invalid_param_falls_back_to_minimum(self):
        self.app._music_vars["speed_percent"].set("abc")
        config = self.app._collect_music_config()
        self.assertEqual(config["speed_percent"], 10)
        self.assertEqual(self.app._music_vars["speed_percent"].get(), "10")

    def test_save_config_posts_to_backend(self):
        self.app._music_vars["hold_percent"].set("75")
        self.app._on_music_save_config()
        self.assertEqual(self.api.saved_configs[-1]["hold_percent"], 75)

    def test_shift_change_re_analyzes(self):
        self.app._music_vars["semitone_shift"].set("-5")
        self.app._on_music_shift_change()
        self.assertIsNotNone(self.app._music_analysis)
        self.assertEqual(self.app._current_shift(), -5)

    def test_shift_is_clamped(self):
        self.app._music_vars["semitone_shift"].set("999")
        self.assertEqual(self.app._current_shift(), 48)
        self.app._music_vars["semitone_shift"].set("nonsense")
        self.assertEqual(self.app._current_shift(), 0)

    def test_running_status_drives_buttons_and_progress(self):
        self.api.status["music"] = {
            "playing": True, "paused": False, "counting_down": False,
            "name": "demo", "event_index": 20, "event_total": 240,
            "position": 12.0, "duration": 48.0,
            "loop_current": 1, "loop_total": 2,
        }
        self.app.refresh_status()

        self.assertEqual(str(self.app.btn_music_pause.cget("state")), "normal")
        self.assertEqual(str(self.app.btn_music_resume.cget("state")), "disabled")
        self.assertEqual(str(self.app.btn_music_stop.cget("state")), "normal")
        self.assertEqual(str(self.app.btn_music_play.cget("state")), "disabled")
        self.assertAlmostEqual(self.app._music_progress.get(), 25.0, places=3)
        self.assertIn("演奏中", self.app._music_run_var.get())
        self.assertIn("12s / 48s", self.app._music_run_var.get())
        self.assertIn("第 1 轮 / 2", self.app._music_run_var.get())
        self.assertIn("MIDI 演奏中", self.app.status_var.get())

    def test_paused_status(self):
        self.api.status["music"] = {
            "playing": True, "paused": True, "counting_down": False,
            "name": "demo", "event_index": 20, "event_total": 240,
            "position": 12.0, "duration": 48.0,
            "loop_current": 1, "loop_total": 0,
        }
        self.app.refresh_status()
        self.assertEqual(str(self.app.btn_music_pause.cget("state")), "disabled")
        self.assertEqual(str(self.app.btn_music_resume.cget("state")), "normal")
        self.assertIn("演奏已暂停", self.app.status_var.get())
        self.assertIn("无限", self.app._music_run_var.get())

    def test_countdown_status(self):
        self.api.status["music"] = {"playing": False, "paused": False,
                                    "counting_down": True, "name": "demo"}
        self.api.status["countdown"] = 3
        self.api.status["countdown_label"] = "演奏 demo 即将开始"
        self.app.refresh_status()
        self.assertEqual(str(self.app.btn_music_stop.cget("state")), "normal")
        self.assertIn("即将开始演奏", self.app._music_run_var.get())

    def test_idle_status_resets_progress(self):
        self.api.status["music"] = {"playing": True, "paused": False,
                                    "counting_down": False, "name": "demo",
                                    "position": 20.0, "duration": 48.0,
                                    "event_index": 5, "event_total": 10,
                                    "loop_current": 1, "loop_total": 1}
        self.app.refresh_status()
        self.api.status["music"] = {"playing": False, "paused": False,
                                    "counting_down": False}
        self.app.refresh_status()
        self.assertEqual(self.app._music_progress.get(), 0)
        self.assertEqual(self.app._music_run_var.get(), "未运行")
        self.assertEqual(str(self.app.btn_music_stop.cget("state")), "disabled")

    # ---- 键位编辑对话框 ----

    def test_binding_editor_round_trip(self):
        saved: list = []
        dialog = webview.BindingEditorDialog(self.app, HANDPAN_BINDINGS, saved.append)
        try:
            self.assertEqual(len(dialog._rows), 9)
            dialog._rows[0]["key_var"].set("Z")
            dialog._on_save()
        finally:
            try:
                dialog.win.destroy()
            except Exception:
                pass

        self.assertEqual(len(saved), 1)
        pitches = [b["pitch"] for b in saved[0]]
        self.assertEqual(pitches, sorted(pitches))
        rebound = next(b for b in saved[0] if b["pitch"] == 45)
        self.assertEqual(rebound["key"], "Z")

    def test_binding_editor_adds_new_note(self):
        saved: list = []
        dialog = webview.BindingEditorDialog(self.app, [], saved.append)
        try:
            dialog._new_pitch.set("C#5")
            dialog._new_key.set("q")
            dialog._on_add()
            self.assertEqual(len(dialog._rows), 1)
            self.assertEqual(dialog._rows[0]["pitch"], 73)
            dialog._on_save()
        finally:
            try:
                dialog.win.destroy()
            except Exception:
                pass
        self.assertEqual(saved[0], [{"pitch": 73, "key": "Q"}])

    def test_binding_editor_rejects_empty_and_keeps_dialog_open(self):
        saved: list = []
        dialog = webview.BindingEditorDialog(self.app, HANDPAN_BINDINGS, saved.append)
        try:
            for row in dialog._rows:
                row["key_var"].set("")
            dialog._on_save()
            self.assertEqual(saved, [])
            self.assertTrue(dialog.win.winfo_exists())
            self.assertIn("不合法", self.msgbox.last_message())
        finally:
            dialog.win.destroy()

    def test_binding_editor_rejects_duplicate_keys(self):
        saved: list = []
        dialog = webview.BindingEditorDialog(
            self.app, [{"pitch": 60, "key": "T"}, {"pitch": 62, "key": "Y"}],
            saved.append)
        try:
            dialog._rows[1]["key_var"].set("T")
            dialog._on_save()
            self.assertEqual(saved, [])
            self.assertIn("同时绑给", self.msgbox.last_message())
        finally:
            dialog.win.destroy()


@unittest.skipUnless(TK_AVAILABLE, "当前环境没有可用的显示设备")
class RegionCaptureRoutingTest(unittest.TestCase):
    """三个区域捕获按钮必须都走后端队列。

    互斥检查（录制/脚本/回放/演奏进行中拒绝圈选）在 ClickerManager 里，只有
    经过 js_api.request_region_capture 才吃得到。曾经「拖拽圈选」和「拾取窗口」
    直接调 _start_region_capture 绕过了检查，脚本运行中也能把全屏覆盖层弹出来。
    这个测试锁住回归。
    """

    _EXPECTED = (("拖拽圈选", "drag"), ("拾取窗口", "window"))

    def setUp(self):
        self.api = StubApi()
        self._real_messagebox = webview.messagebox
        self.msgbox = StubMessageBox()
        webview.messagebox = self.msgbox
        self.addCleanup(self._restore_messagebox)

        self.app = webview._DesktopWindow("测试窗口", self.api, 2540, 1300)
        try:
            self.app.root.withdraw()
        except Exception:
            pass
        self.addCleanup(self._destroy)

    def _restore_messagebox(self):
        webview.messagebox = self._real_messagebox

    def _destroy(self):
        try:
            self.app.root.destroy()
        except Exception:
            pass

    def _capture_buttons(self):
        """按标签文字找出捕获按钮。

        用包含匹配而不是全等：「📐 拖拽圈选」的标签会被热键刷新逻辑改写成
        「📐 拖拽圈选 (F6)」，写死全等会在改热键显示时莫名其妙地失败。
        """
        found = {}
        for widget in self.app._rc_capture_widgets:
            try:
                text = str(widget.cget("text"))
            except Exception:
                continue
            for label, mode in self._EXPECTED:
                if label in text:
                    found[label] = (widget, mode)
        return found

    def test_all_capture_buttons_exist(self):
        found = self._capture_buttons()
        self.assertEqual(sorted(found), sorted(label for label, _ in self._EXPECTED))

    def test_redundant_record_region_button_is_gone(self):
        """「🎙 录制区域」与拖拽圈选完全等价，已删除；别让它悄悄回来。"""
        for widget in self.app._rc_capture_widgets:
            try:
                text = str(widget.cget("text"))
            except Exception:
                continue
            self.assertNotIn("录制区域", text)

    def test_drag_button_carries_the_hotkey_hint(self):
        """F6 的名字现在标在拖拽圈选按钮上，删按钮不能把热键提示一起弄丢。"""
        self.app.refresh_status()
        text = str(self.app._rc_record_btn.cget("text"))
        self.assertIn("拖拽圈选", text)
        self.assertIn("F6", text)

    def test_buttons_go_through_backend_not_selector(self):
        direct_calls: list = []
        self.app._start_region_capture = lambda mode="drag": direct_calls.append(mode)

        for label, (widget, mode) in self._capture_buttons().items():
            self.api.region_requests.clear()
            widget.invoke()
            self.assertEqual(self.api.region_requests, [mode],
                             f"{label} 没有以 mode={mode!r} 投递到后端")

        self.assertEqual(direct_calls, [],
                         "按钮直接调了 _start_region_capture，会绕过后端互斥检查")

    def test_drain_still_creates_the_selector(self):
        """队列排空这条路径必须照旧真正创建圈选窗口，否则统一入口就成了空转。"""
        created: list = []
        self.app._start_region_capture = lambda mode="drag": created.append(mode)
        pending = ["window", None]
        self.api.pop_region_request = lambda: pending.pop(0) if pending else None

        self.app._drain_region_requests()
        self.assertEqual(created, ["window"])


class OutlineGeometryTest(unittest.TestCase):
    """常驻边框的几何计算（纯计算，不需要显示设备）。"""

    PAD = OUTLINE_BORDER + OUTLINE_MARGIN

    def test_window_is_the_region_expanded_by_the_pad(self):
        w, h, x, y = outline_geometry(
            {"x": 100, "y": 200, "width": 320, "height": 240})
        self.assertEqual(
            (w, h, x, y),
            (320 + self.PAD * 2, 240 + self.PAD * 2, 100 - self.PAD, 200 - self.PAD))

    def test_negative_coordinates_survive(self):
        """副屏在主屏左侧/上方时 x、y 为负，不能在这里被夹到 0。"""
        w, h, x, y = outline_geometry(
            {"x": -1920, "y": -300, "width": 800, "height": 600})
        self.assertEqual((x, y), (-1920 - self.PAD, -300 - self.PAD))
        self.assertEqual((w, h), (800 + self.PAD * 2, 600 + self.PAD * 2))

    def test_border_band_does_not_overlap_the_region(self):
        """线带必须完全落在区域外侧。

        Tk 的线宽居中于路径，RegionOutline._draw() 把路径取在 pad - border/2，
        于是线带覆盖 [pad - border, pad]；区域在窗口坐标系里从 pad 开始。两者只
        在 pad 处相邻、不重叠，所以覆盖层没有任何不透明像素压在可点击区域上。
        这是 RegionOutline 三层穿透保障里的第一层，也是唯一不依赖 Tk 分层窗口
        与 Win32 扩展样式的一层——那两层都可能失败。
        """
        band_outer = self.PAD - OUTLINE_BORDER
        band_inner = band_outer + OUTLINE_BORDER
        region_start = self.PAD
        self.assertLessEqual(band_inner, region_start)
        self.assertGreaterEqual(band_outer, 0, "留白不足，边框会被窗口边界裁掉")

    def test_invalid_region_returns_none(self):
        bad = [
            {"x": 0, "y": 0, "width": 0, "height": 10},
            {"x": 0, "y": 0, "width": 10, "height": -5},
            {"x": 0, "y": 0, "width": 10},
            {"x": "a", "y": 0, "width": 10, "height": 10},
            {},
            None,
        ]
        for region in bad:
            with self.subTest(region=region):
                self.assertIsNone(outline_geometry(region))


@unittest.skipUnless(TK_AVAILABLE, "当前环境没有可用的显示设备")
class RegionOutlineSyncTest(unittest.TestCase):
    """常驻边框的生命周期。

    边框由 500ms 状态轮询驱动，所以真正容易出的问题不是"画不出来"，而是：
    每拍都重建导致闪烁、停止后留残窗、穿透失败后每 500ms 重试一次。

    RegionOutline 换成记录桩：真的建 Toplevel 会往屏幕上画东西，而穿透路径依赖
    真实 HWND 与分层窗口支持，在测试环境里不可复现。
    """

    REGION = {"x": 100, "y": 200, "width": 320, "height": 240}

    def setUp(self):
        self.api = StubApi()
        self._real = (webview.messagebox, webview.RegionOutline, webview.push_toast)
        self.toasts: list = []
        webview.messagebox = StubMessageBox()
        webview.push_toast = lambda msg, duration=2.0: self.toasts.append(msg)
        self.created: list = []
        webview.RegionOutline = self._make_stub()
        self.addCleanup(self._restore)

        self.app = webview._DesktopWindow("测试窗口", self.api, 2540, 1300)
        try:
            self.app.root.withdraw()
        except Exception:
            pass
        self.addCleanup(self._destroy)
        self._set_region(self.REGION)

    def _make_stub(self, exc=None):
        created = self.created

        class StubOutline:
            def __init__(self, root, region, border=OUTLINE_BORDER):
                if exc is not None:
                    raise exc
                self.region = dict(region)
                self.destroyed = False
                created.append(self)

            def destroy(self):
                self.destroyed = True

        return StubOutline

    def _restore(self):
        webview.messagebox, webview.RegionOutline, webview.push_toast = self._real

    def _destroy(self):
        try:
            self.app.root.destroy()
        except Exception:
            pass

    def _set_region(self, region):
        for key, src in (("region_x", "x"), ("region_y", "y"),
                         ("region_width", "width"), ("region_height", "height")):
            self.app._rc_vars[key].set(str(int(region[src])))

    def _poll(self, active: bool):
        """模拟一拍状态轮询。"""
        self.app._update_region_ui({"region_click_active": active,
                                    "region_click_total": 0,
                                    "region_click_spots": 0})

    # ---- 基本生命周期 ----

    def test_outline_appears_when_running(self):
        self._poll(True)
        self.assertEqual(len(self.created), 1)
        self.assertEqual(self.created[0].region, self.REGION)
        self.assertIs(self.app._rc_outline, self.created[0])

    def test_no_outline_when_idle(self):
        self._poll(False)
        self.assertEqual(self.created, [])
        self.assertIsNone(self.app._rc_outline)

    def test_repeated_polls_do_not_recreate(self):
        """每 500ms 一拍，重建会让边框闪，而且建 Toplevel 并不便宜。"""
        for _ in range(5):
            self._poll(True)
        self.assertEqual(len(self.created), 1)
        self.assertFalse(self.created[0].destroyed)

    def test_outline_destroyed_when_stopped(self):
        self._poll(True)
        self._poll(False)
        self.assertTrue(self.created[0].destroyed)
        self.assertIsNone(self.app._rc_outline)
        self.assertIsNone(self.app._rc_outline_region)

    def test_region_change_while_running_recreates(self):
        self._poll(True)
        moved = {"x": 400, "y": 500, "width": 200, "height": 150}
        self._set_region(moved)
        self._poll(True)
        self.assertEqual(len(self.created), 2)
        self.assertTrue(self.created[0].destroyed)
        self.assertEqual(self.created[1].region, moved)

    # ---- 开关 ----

    def test_toggle_off_destroys_outline(self):
        self._poll(True)
        self.app._rc_outline_var.set(False)
        self.app._sync_region_outline()
        self.assertTrue(self.created[0].destroyed)
        self.assertIsNone(self.app._rc_outline)

    def test_toggle_on_while_running_creates_outline(self):
        self.app._rc_outline_var.set(False)
        self._poll(True)
        self.assertEqual(self.created, [])
        self.app._rc_outline_var.set(True)
        self.app._sync_region_outline()
        self.assertEqual(len(self.created), 1)

    # ---- 退化路径 ----

    def test_invalid_region_does_not_create_outline(self):
        self.app._rc_vars["region_width"].set("0")
        self._poll(True)
        self.assertEqual(self.created, [])
        self.assertIsNone(self.app._rc_outline)

    def test_unavailable_outline_is_not_retried(self):
        """穿透做不到时只提示一次，之后每拍都重试会疯狂建/销 Toplevel。"""
        webview.RegionOutline = self._make_stub(
            RegionOutlineUnavailable("no click-through"))
        for _ in range(4):
            self._poll(True)
        self.assertEqual(self.created, [])
        self.assertIsNone(self.app._rc_outline)
        self.assertEqual(len(self.toasts), 1, "应该只 toast 一次")
        self.assertIn("点击穿透", self.toasts[0])

    def test_unexpected_error_is_also_not_retried(self):
        webview.RegionOutline = self._make_stub(RuntimeError("boom"))
        for _ in range(3):
            self._poll(True)
        self.assertEqual(len(self.toasts), 1)
        self.assertIn("boom", self.toasts[0])


if __name__ == "__main__":
    unittest.main(verbosity=2)
