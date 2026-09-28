"""GUI 构建冒烟测试。

webview.py 里几百行 Tkinter 代码只有在真正构建窗口时才会执行，单元测试碰不到。
这里用一个假的 js_api 把 _DesktopWindow 完整搭起来（不启动 mainloop），
验证第五栏「MIDI 自动演奏」面板能正常渲染、分析结果能正确驱动按钮状态。

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


if __name__ == "__main__":
    unittest.main(verbosity=2)
