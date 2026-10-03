"""区域连点的平滑性回归测试。

针对两条实际使用反馈：

1. **点击后鼠标"弹回原位"再跑向下一个点** —— 两个来源：落点抖动的反向补偿
   （按下前 +jitter、抬起后 −jitter，clicks_per_spot=3 时一个点来回 6 趟），
   以及漂移校正用单个 stroke 把偏差一次性甩回去。
2. **默认策略在短程换点上抖动过大** —— `path_strategy` 默认 `global` 会跟随
   第三栏的 `fitts`（2px 高斯微抖 + 30% 概率 4.5~15px 过冲再拉回）；而
   `PathPlanner` 的 sine 振幅是固定像素、不随距离缩放，全局默认 10px 叠在
   `min_spot_distance_px=40` 的短程移动上就是 4 个 ±10px 的波。

修复的核心是**每段移动后读回真实光标位置**当作下一段的起点：位移量从实际位置
算，误差不跨段累积，于是既不需要反向补偿、也不需要事后瞬移校正。
`RegionClickTrackingTest` 专门锁这一点。

所有注入路径（`_send_mouse_stroke`）与等待（`_sleep_with_controls` /
`_wait_until_ready`）都换成记录桩：测试不会真的动鼠标，不需要驱动，也不需要
管理员权限。
"""

from __future__ import annotations

import math
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import ActionScript  # noqa: E402
from ActionScript import (ActionExecutor, ActionScriptManager,  # noqa: E402
                          RegionClickAction)
from ConfigManager import DEFAULT_PATH_PLANNER, DEFAULT_REGION_CLICK  # noqa: E402
from PathPlanner import PathPlanner  # noqa: E402

LEFT_DOWN, LEFT_UP = ActionExecutor._BUTTON_STATES["left"]


class RecordingExecutor(ActionExecutor):
    """把注入、等待、光标读取全部换成记录，绝不动真鼠标。

    只在最底层拦 `_send_mouse_stroke`，让 `_send_relative_move` / `_send_button`
    的真实实现照跑——这样 stroke 的组装方式也在被测范围内。
    """

    def __init__(self):
        super().__init__()
        self.moves: list[tuple[int, int]] = []
        self.buttons: list[int] = []
        self.cursor: tuple[int, int] | None = (500, 500)
        self.cursor_script: list = []

    def _send_mouse_stroke(self, stroke) -> bool:
        if int(stroke.state) == 0:
            self.moves.append((int(stroke.x), int(stroke.y)))
        else:
            self.buttons.append(int(stroke.state))
        return True

    def _sleep_with_controls(self, seconds, stop_event=None, pause_event=None) -> bool:
        return not (stop_event is not None and stop_event.is_set())

    def _wait_until_ready(self, stop_event=None, pause_event=None) -> bool:
        return not (stop_event is not None and stop_event.is_set())

    def _read_cursor_or_none(self):
        if self.cursor_script:
            return self.cursor_script.pop(0)
        return self.cursor

    def reset(self):
        self.moves.clear()
        self.buttons.clear()


class ClickJitterTest(unittest.TestCase):
    """落点抖动只出去、不回来。"""

    def setUp(self):
        self.ex = RecordingExecutor()

    def test_click_does_not_compensate_back(self):
        """旧实现会在抬起后补发 (-jx, -jy)，那就是用户看到的"弹回原位"。"""
        action = RegionClickAction(x_jitter_px=5, y_jitter_px=5,
                                   hold_ms=1, hold_jitter_ms=0)
        with mock.patch.object(ActionScript.random, "randint", return_value=4):
            ok = self.ex._click_at_spot((100, 100), action, LEFT_DOWN, LEFT_UP,
                                        None, None)
        self.assertTrue(ok)
        self.assertEqual(self.ex.moves, [(4, 4)],
                         "应该只有一次出去的微移，不能有反向补偿")
        self.assertEqual(self.ex.buttons, [LEFT_DOWN, LEFT_UP])

    def test_zero_jitter_sends_no_move(self):
        action = RegionClickAction(x_jitter_px=0, y_jitter_px=0,
                                   hold_ms=1, hold_jitter_ms=0)
        self.ex._click_at_spot((100, 100), action, LEFT_DOWN, LEFT_UP, None, None)
        self.assertEqual(self.ex.moves, [])
        self.assertEqual(self.ex.buttons, [LEFT_DOWN, LEFT_UP])

    def test_interrupted_click_still_releases_the_button(self):
        """被打断时也必须抬起按键，否则留下按住的鼠标。"""
        import threading
        action = RegionClickAction(x_jitter_px=0, y_jitter_px=0,
                                   hold_ms=1, hold_jitter_ms=0)
        stop = threading.Event()
        self.ex._sleep_with_controls = lambda *a, **k: False
        ok = self.ex._click_at_spot((100, 100), action, LEFT_DOWN, LEFT_UP, stop, None)
        self.assertFalse(ok)
        self.assertEqual(self.ex.buttons, [LEFT_DOWN, LEFT_UP])


class DriftCorrectionTest(unittest.TestCase):
    """漂移校正：默认关闭；开启时分多步走，不是单 stroke 瞬移。"""

    def setUp(self):
        self.ex = RecordingExecutor()

    def test_disabled_by_default(self):
        self.assertEqual(DEFAULT_REGION_CLICK["correct_drift_px"], 0)
        self.ex.cursor = (100, 100)
        self.ex._correct_drift(500, 500, DEFAULT_REGION_CLICK["correct_drift_px"])
        self.assertEqual(self.ex.moves, [])

    def test_corrects_in_multiple_steps_not_one_snap(self):
        self.ex.cursor = (100, 100)
        self.ex._correct_drift(200, 100, 4)
        self.assertGreater(len(self.ex.moves), 1,
                           "单 stroke 甩过去就是用户看到的瞬移")
        self.assertEqual(sum(dx for dx, _ in self.ex.moves), 100)
        self.assertEqual(sum(dy for _, dy in self.ex.moves), 0)

    def test_steps_are_bounded(self):
        """再大的偏差也不能拆成几百步。"""
        self.ex.cursor = (0, 0)
        self.ex._correct_drift(5000, 5000, 4)
        self.assertLessEqual(len(self.ex.moves), 12)
        self.assertEqual(sum(dx for dx, _ in self.ex.moves), 5000)

    def test_within_threshold_does_nothing(self):
        self.ex.cursor = (198, 100)
        self.ex._correct_drift(200, 100, 4)
        self.assertEqual(self.ex.moves, [])

    def test_read_failure_does_nothing(self):
        self.ex.cursor = None
        self.ex._correct_drift(200, 100, 4)
        self.assertEqual(self.ex.moves, [])


class ReadCursorTest(unittest.TestCase):
    """读回光标位置的失败语义。"""

    def setUp(self):
        self.ex = RecordingExecutor()

    def test_returns_actual_position(self):
        self.ex.cursor = (1, 2)
        self.assertEqual(self.ex._read_cursor((7, 9)), (1, 2))

    def test_falls_back_when_read_fails(self):
        self.ex.cursor = None
        self.assertEqual(self.ex._read_cursor((7, 9)), (7, 9))

    def test_origin_is_a_real_position_not_a_failure(self):
        """(0,0) 是虚拟屏原点的合法坐标。

        _get_cursor_pos 拿 (0,0) 当失败哨兵，两者无法区分；区域连点用它算位移，
        判错一次就会甩出一个从屏幕原点出发的巨大位移。这是 _read_cursor_or_none
        存在的理由。
        """
        self.ex.cursor = (0, 0)
        self.assertEqual(self.ex._read_cursor((7, 9)), (0, 0))


class RegionClickTrackingTest(unittest.TestCase):
    """换点位移必须从**实际**光标位置算，不是从上一个目标点算。

    这是本次修复的核心：只要起点取自读回的真实位置，落点抖动、丢 stroke、
    用户手动挪鼠标都不会让误差一段段累积，反向补偿与瞬移校正也就都不需要了。
    """

    SPOT1 = (100, 100)
    SPOT2 = (300, 300)

    def setUp(self):
        self.ex = RecordingExecutor()
        self.planned: list[tuple[int, int]] = []
        self.ex._plan_relative_move = self._record_plan

    def _record_plan(self, dx, dy, duration_ms, action):
        self.planned.append((int(dx), int(dy)))
        return []

    def _action(self, clicks: int) -> RegionClickAction:
        return RegionClickAction(
            region_x=0, region_y=0, region_width=1000, region_height=1000,
            clicks=clicks, forever=False,
            clicks_per_spot=1, clicks_per_spot_jitter=0,
            interval_ms=0, interval_jitter_ms=0,
            hold_ms=1, hold_jitter_ms=0,
            move_duration_ms=10, move_duration_jitter_ms=0,
            margin_px=0, min_spot_distance_px=0,
            x_jitter_px=0, y_jitter_px=0,
            spot_pause_ms=0, spot_pause_jitter_ms=0,
            correct_drift_px=0, path_strategy="straight",
        )

    def _run(self, spots, cursor_script, clicks):
        spots = list(spots)
        self.ex._pick_spot = lambda bounds, prev, mind: spots.pop(0)
        self.ex.cursor_script = list(cursor_script)
        return self.ex._execute_region_click(self._action(clicks), None, None)

    def test_first_move_starts_from_the_actual_cursor(self):
        self._run([self.SPOT1], [(500, 500), self.SPOT1], clicks=1)
        self.assertEqual(self.planned[0], (-400, -400))

    def test_second_move_starts_from_the_readback_not_the_previous_target(self):
        """读回 (107, 95) 说明上一段没正好落在 SPOT1 上（丢 stroke / 抖动）。

        旧实现按 SPOT2 - SPOT1 = (200, 200) 算，误差就这么带到下一段；
        现在必须按 SPOT2 - 读回位置 = (193, 205) 算。
        """
        ok = self._run([self.SPOT1, self.SPOT2],
                       [(500, 500), (107, 95), self.SPOT2], clicks=2)
        self.assertTrue(ok)
        self.assertEqual(self.planned, [(-400, -400), (193, 205)])

    def test_read_failure_yields_no_movement_instead_of_a_huge_jump(self):
        """读不到光标时回落成"已到位"，本段位移 0。

        旧实现拿 _get_cursor_pos 的 (0,0) 哨兵当真实位置，会算出一个从屏幕原点
        出发的巨大位移量。
        """
        self._run([self.SPOT1], [None, None, None], clicks=1)
        self.assertEqual(self.planned[0], (0, 0))

    def test_clicks_are_counted_and_session_ends(self):
        ok = self._run([self.SPOT1, self.SPOT2],
                       [(500, 500), self.SPOT1, self.SPOT2], clicks=2)
        self.assertTrue(ok)
        self.assertEqual(self.ex.region_click_total, 2)
        self.assertEqual(self.ex.region_click_spots, 2)
        self.assertFalse(self.ex.region_click_active, "结束后必须复位，否则边框不消失")
        self.assertEqual(self.ex.buttons, [LEFT_DOWN, LEFT_UP] * 2)

    def test_active_flag_is_cleared_even_on_stop(self):
        import threading
        stop = threading.Event()
        stop.set()
        spots = [self.SPOT1, self.SPOT2]
        self.ex._pick_spot = lambda bounds, prev, mind: spots.pop(0)
        self.ex.cursor_script = [(500, 500)]
        ok = self.ex._execute_region_click(self._action(0), stop, None)
        self.assertFalse(ok)
        self.assertFalse(self.ex.region_click_active)


class DefaultsTest(unittest.TestCase):
    """两份默认值必须同步，且默认就是平滑的那套。"""

    _SYNCED = ("path_strategy", "path_steps", "button", "correct_drift_px",
               "path_params", "x_jitter_px", "y_jitter_px", "clicks_per_spot",
               "interval_ms", "hold_ms", "move_duration_ms", "margin_px",
               "min_spot_distance_px")

    def _parse(self, item: dict):
        """_parse_region_click 挂在 ActionScriptManager 上，不在 ActionExecutor。

        它的构造函数会 mkdir data_dir/action_scripts，所以用临时目录，
        免得测试在真实 data/ 里留下东西。
        """
        with tempfile.TemporaryDirectory() as tmp:
            return ActionScriptManager(Path(tmp))._parse_region_click(item)

    def test_dataclass_and_config_defaults_agree(self):
        """面板走 DEFAULT_REGION_CLICK，手写脚本走 RegionClickAction()。

        两边不同步的话，脚本里省略字段就会悄悄退回旧的抖动行为。
        """
        d = RegionClickAction()
        for key in self._SYNCED:
            with self.subTest(field=key):
                self.assertEqual(getattr(d, key), DEFAULT_REGION_CLICK[key])

    def test_region_defaults_are_smooth(self):
        self.assertEqual(DEFAULT_REGION_CLICK["path_strategy"], "sine")
        self.assertEqual(DEFAULT_REGION_CLICK["correct_drift_px"], 0)
        params = DEFAULT_REGION_CLICK["path_params"]
        self.assertLessEqual(params["sine_amplitude_px"], 3.0)
        self.assertLessEqual(params["sine_frequency"], 1)

    def test_parse_falls_back_to_the_gentle_path_params(self):
        """省略 path_params 时要拿到温和默认，而不是空 dict（空 dict = 全局 10px 振幅）。"""
        action = self._parse({"type": "region_click",
                              "region": {"x": 0, "y": 0, "width": 100, "height": 100}})
        self.assertIsNotNone(action)
        self.assertEqual(action.path_strategy, "sine")
        self.assertEqual(action.path_params.get("sine_amplitude_px"), 3.0)
        self.assertEqual(action.correct_drift_px, 0)

    def test_parse_keeps_explicit_path_params(self):
        action = self._parse({"type": "region_click",
                              "region": {"x": 0, "y": 0, "width": 100, "height": 100},
                              "path_strategy": "fitts",
                              "path_params": {"fitts_arc_px": 20.0}})
        self.assertEqual(action.path_strategy, "fitts")
        self.assertEqual(action.path_params, {"fitts_arc_px": 20.0})

    def test_planner_receives_the_gentle_params(self):
        ex = RecordingExecutor()
        action = RegionClickAction()
        kwargs = ex._planner_kwargs(action.path_params)
        self.assertEqual(kwargs.get("sine_amplitude_px"), 3.0)
        self.assertEqual(kwargs.get("sine_frequency"), 1)


class ShortMoveSmoothnessTest(unittest.TestCase):
    """直接量"鼠标乱不乱跑"：40px 短程换点偏离直线多少。

    `min_spot_distance_px` 默认 40，这就是区域连点最典型的换点距离。
    """

    DX, DY = 40, 0
    DURATION_MS = 220
    SAMPLES = 200

    def _worst_perpendicular(self, strategy: str, params: dict) -> float:
        ex = RecordingExecutor()
        planner = PathPlanner(strategy=strategy, **params)
        dist = math.hypot(self.DX, self.DY)
        perp_x, perp_y = -self.DY / dist, self.DX / dist
        worst = 0.0
        for _ in range(self.SAMPLES):
            base = ex._build_base_moves(self.DX, self.DY, self.DURATION_MS, 27)
            cx = cy = 0.0
            for ev in planner.generate_path(self.DX, self.DY, base):
                cx += ev.get("x", 0)
                cy += ev.get("y", 0)
                worst = max(worst, abs(cx * perp_x + cy * perp_y))
        return worst

    def test_region_default_stays_close_to_the_line(self):
        worst = self._worst_perpendicular(
            DEFAULT_REGION_CLICK["path_strategy"],
            DEFAULT_REGION_CLICK["path_params"])
        self.assertLessEqual(
            worst, 4.5,
            f"40px 换点垂直偏移达 {worst:.1f}px —— 这就是用户说的\"随机跑动\"")

    def test_region_default_is_gentler_than_the_global_default(self):
        """改动前区域连点走 global，即第三栏那套全局默认（10px 振幅）。"""
        region = self._worst_perpendicular(
            DEFAULT_REGION_CLICK["path_strategy"],
            DEFAULT_REGION_CLICK["path_params"])
        global_ = self._worst_perpendicular(
            DEFAULT_PATH_PLANNER["strategy"],
            {k: v for k, v in DEFAULT_PATH_PLANNER.items() if k != "strategy"})
        self.assertLess(region, global_,
                        f"区域默认({region:.1f}px) 应比全局默认({global_:.1f}px) 更平")

    def test_endpoint_is_still_exact(self):
        """再平滑也不能丢终点精度：总位移必须正好等于目标。"""
        ex = RecordingExecutor()
        params = dict(DEFAULT_REGION_CLICK["path_params"])
        planner = PathPlanner(strategy=DEFAULT_REGION_CLICK["path_strategy"], **params)
        for dx, dy in ((40, 0), (0, 40), (-37, 29), (120, -80)):
            base = ex._build_base_moves(dx, dy, self.DURATION_MS, 27)
            path = planner.generate_path(dx, dy, base)
            self.assertEqual((sum(e.get("x", 0) for e in path),
                              sum(e.get("y", 0) for e in path)), (dx, dy),
                             f"({dx},{dy}) 终点漂移")


if __name__ == "__main__":
    unittest.main(verbosity=2)
