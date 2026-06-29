import json
import threading
from pathlib import Path

import webview

from Clicker import ClickerManager
from ConfigManager import ConfigManager

# Toast helper — available when webview.py (Tkinter GUI) is loaded.
try:
    from webview import push_toast
except ImportError:
    push_toast = None


APP_DIR = Path(__file__).resolve().parent
WEB_DIR = APP_DIR / "web"


class Api:
    def __init__(self, manager: ClickerManager):
        self.manager = manager

    def list_scripts(self):
        try:
            return self.manager.action_manager.list_scripts()
        except Exception:
            return []

    def run_script(self, name: str):
        # run in background thread to avoid blocking UI
        def worker():
            actions = self.manager.action_manager.load_script(name)
            if actions:
                self.manager._run_script_session(name, actions)

        threading.Thread(target=worker, daemon=True).start()
        return True

    def delete_script(self, name: str):
        path = self.manager.action_manager.scripts_dir / f"{name}.json"
        try:
            if path.exists():
                path.unlink()
                return True
            return False
        except Exception:
            return False

    def toggle_start_stop(self):
        def worker():
            if self.manager.clicker.running:
                self.manager._on_stop()
            else:
                self.manager._on_start()

        threading.Thread(target=worker, daemon=True).start()
        return True

    # ---- 录制控制 ----

    def start_recording(self, name: str = "recording"):
        """开始录制输入事件。"""
        self.manager.start_recording(name)
        return True

    def stop_recording_and_save(self, name: str | None = None):
        """停止录制并保存到 data/action_scripts/。"""
        path = self.manager.stop_recording_and_save(name)
        if path:
            return Path(path).name
        return None

    def cancel_recording(self):
        """取消录制（不保存）。"""
        self.manager.cancel_recording()
        return True

    def play_recording(self, script_name: str, speed: float = 1.0):
        """回放指定脚本（stem 名）。可播放录制脚本和动作脚本。

        所有异常都会通过 toast 弹窗提示，避免静默失败。
        """
        def toast(msg: str, duration: float = 4.0):
            """在主线程弹 toast（线程安全）。"""
            try:
                from webview import push_toast
                push_toast(msg, duration)
            except Exception:
                pass

        def worker():
            try:
                scripts_dir = self.manager.action_manager.scripts_dir
                filepath = scripts_dir / f"{script_name}.json"

                toast(f"▶ 开始执行: {script_name}", duration=2.0)

                if not filepath.exists():
                    toast(f"❌ 脚本文件不存在: {filepath}", duration=5.0)
                    return

                # 读取脚本头判断类型
                try:
                    with open(filepath, "r", encoding="utf-8") as f:
                        data = json.load(f)
                except Exception as e:
                    toast(f"❌ 读取脚本失败: {e}", duration=5.0)
                    return

                script_type = data.get("meta", {}).get("type", "action")
                toast(f"脚本类型: {script_type}", duration=2.0)

                if script_type == "recorded":
                    # 录制脚本 → PlaybackEngine
                    ok = self.manager.play_recording(filepath, speed)
                    if not ok:
                        toast(f"❌ 回放启动失败（详见日志）", duration=5.0)
                else:
                    # 动作脚本 → ActionScript
                    try:
                        actions = self.manager.action_manager.load_script(script_name)
                    except Exception as e:
                        toast(f"❌ 加载动作脚本失败: {e}", duration=5.0)
                        return

                    if not actions:
                        toast(f"❌ 脚本无动作: {script_name}", duration=5.0)
                        return

                    toast(f"✓ 已加载 {len(actions)} 个动作，即将启动", duration=2.0)
                    self.manager._run_script_session(script_name, actions)

            except Exception as e:
                # 兜底：任何未捕获的异常都弹出来
                import traceback
                tb = traceback.format_exc()
                toast(f"❌ 执行异常: {e}\n{tb[-200:]}", duration=8.0)

        threading.Thread(target=worker, daemon=True).start()
        return True

    def stop_playback(self):
        """停止回放。"""
        self.manager.stop_playback()
        return True

    # ---- 暂停/继续/停止 API（脚本和回放通用） ----

    def pause_current(self):
        """暂停当前正在运行的脚本或回放。"""
        def worker():
            try:
                m = self.manager
                if m.script_running and not m.script_paused:
                    m.pause_script()
                elif m._playback and m._playback.is_playing() and not m._playback.is_paused():
                    m.pause_playback()
            except Exception as e:
                try:
                    from webview import push_toast
                    push_toast(f"❌ 暂停失败: {e}", duration=3.0)
                except Exception:
                    pass
        threading.Thread(target=worker, daemon=True).start()
        return True

    def resume_current(self):
        """继续当前暂停的脚本或回放。"""
        def worker():
            try:
                m = self.manager
                if m.script_running and m.script_paused:
                    m.resume_script()
                elif m._playback and m._playback.is_paused():
                    m.resume_playback()
            except Exception as e:
                try:
                    from webview import push_toast
                    push_toast(f"❌ 继续失败: {e}", duration=3.0)
                except Exception:
                    pass
        threading.Thread(target=worker, daemon=True).start()
        return True

    def stop_current(self):
        """停止当前正在运行的脚本或回放。"""
        def worker():
            try:
                m = self.manager
                if m.script_running:
                    m.stop_script()
                elif m._playback and m._playback.is_playing():
                    m.stop_playback()
            except Exception as e:
                try:
                    from webview import push_toast
                    push_toast(f"❌ 停止失败: {e}", duration=3.0)
                except Exception:
                    pass
        threading.Thread(target=worker, daemon=True).start()
        return True

    # ---- 热键配置 API ----

    def get_hotkeys(self):
        """返回当前热键配置。"""
        try:
            return self.manager.get_hotkeys()
        except Exception:
            return {}

    def set_hotkeys(self, hotkeys):
        """更新热键配置。hotkeys 是 {key_name: fkey_str} 字典。"""
        try:
            return self.manager.set_hotkeys(hotkeys)
        except Exception as e:
            return False

    def list_recorded_scripts(self):
        """返回 data/action_scripts/ 下所有 meta.type="recorded" 的脚本名（stem，不含扩展名）。"""
        try:
            scripts_dir = self.manager.action_manager.scripts_dir
            result = []
            for p in scripts_dir.glob("*.json"):
                try:
                    with open(p, "r", encoding="utf-8") as f:
                        data = json.load(f)
                        if data.get("meta", {}).get("type") == "recorded":
                            result.append(p.stem)  # 统一返回 stem
                except Exception:
                    pass
            return result
        except Exception:
            return []

    def toggle_move_mouse(self):
        cur = getattr(self.manager.clicker.config, 'move_mouse', True)
        self.manager.clicker.config.move_mouse = not cur
        ConfigManager.save_config(self.manager.clicker.config, "default")
        return self.manager.clicker.config.move_mouse

    def get_status(self):
        cfg = self.manager.clicker.config
        # 仅返回对用户有用的精简字段；在 move_mouse 为 True 时才包含位置信息
        status = {
            "interception_ready": bool(self.manager.clicker.is_ready()),
            "interception_error": getattr(self.manager.clicker, "init_error", None),
            "running": bool(self.manager.clicker.running),
            "script_running": bool(getattr(self.manager, 'script_running', False)),
            "script_paused": bool(getattr(self.manager, 'script_paused', False)),
            "script_name": getattr(self.manager, 'current_script_name', None),
            # 录制状态
            "recording": bool(getattr(self.manager, '_recording_session_active', False)),
            "recording_name": getattr(self.manager, '_recording_name', None),
            "playback_active": bool(
                getattr(self.manager, '_playback', None)
                and getattr(self.manager._playback, '_playing', False)
            ),
            "click_interval": getattr(cfg, 'click_interval', None),
            "hold_duration": getattr(cfg, 'hold_duration', None),
            "move_mouse": getattr(cfg, 'move_mouse', True),
        }

        # 回放进度（录制脚本专有）
        pb = getattr(self.manager, "_playback", None)
        if pb and pb.is_playing():
            idx, total = pb.get_progress()
            status["playback_active"] = True
            status["playback_progress"] = f"事件 {idx} / {total}" if total > 0 else ""
            status["playback_paused"] = bool(pb.is_paused())
        else:
            status["playback_active"] = False
            status["playback_progress"] = ""
            status["playback_paused"] = False

        # 录制事件计数
        try:
            rec = getattr(self.manager, "_recorder", None)
            if rec and status["recording"]:
                status["recording_event_count"] = int(rec.get_event_count())
        except Exception:
            pass

        if status.get("move_mouse"):
            status.update({
                "center_x": getattr(cfg, 'center_x', None),
                "center_y": getattr(cfg, 'center_y', None),
                "radius": getattr(cfg, 'radius', None),
            })

        # 如果执行器存在，返回被忽略的移动计数（用于调试）
        try:
            ae = getattr(self.manager, 'action_executor', None)
            if ae is not None and hasattr(ae, 'ignored_moves'):
                status["ignored_moves"] = int(getattr(ae, 'ignored_moves', 0))
        except Exception:
            pass

        # 倒计时信息（如果存在）
        try:
            cd_end = getattr(self.manager, 'countdown_end', None)
            cd_label = getattr(self.manager, 'countdown_label', None)
            if cd_end and cd_end > 0:
                import time as _time
                remaining = max(0, int(cd_end - _time.time()))
                status['countdown'] = remaining
                status['countdown_label'] = cd_label
        except Exception:
            pass

        # 热键配置
        try:
            status['hotkeys'] = self.manager.get_hotkeys()
        except Exception:
            pass

        return status


def start_gui():
    manager = ClickerManager(push_toast=push_toast)

    api = Api(manager)

    # 如果 Interception 未就绪，在启动窗口时立即提示一次（避免用户进了界面还不知情）
    if not manager.clicker.is_ready():
        try:
            from Clicker import show_message_box
            msg = (
                "RocoKingdom Clicker 需要 Interception 驱动才能工作。\n\n"
                f"{manager.clicker.init_error or '无法加载 interception.dll。'}\n\n"
                "安装步骤：\n"
                "  1) 以【管理员身份】运行程序目录下 driver_installer\\install-interception.exe /install\n"
                "  2) 重启电脑后再运行本程序。\n\n"
                "（如果只浏览界面，不需要驱动；但点击操作会被拒绝。）"
            )
            show_message_box(msg, "驱动未就绪 - RocoKingdom Clicker", error=False)
        except Exception:
            pass

    index_path = (WEB_DIR / 'index.html').as_uri()

    # run webview in main thread
    webview.create_window('RocoKingdom Clicker', index_path, js_api=api, width=1680, height=920)
    webview.start()


if __name__ == '__main__':
    start_gui()
