"""MusicPlayer 的单元测试。

运行方式（项目根目录）：

    python -m unittest discover -s tests -v

用 FakeExecutor 顶替 ActionExecutor，只记录「什么时刻按下/抬起了哪个键」，
这样既能验证时序，又不会真的往系统里注入按键（在开发机上跑测试时乱按一通
会打到用户当前的窗口里）。

时间相关的断言都留了较宽的容差：CI 机器负载不确定，卡个几毫秒很正常，
测试要验的是「顺序对不对、有没有卡键、倍速有没有生效」，不是毫秒级精度。
"""

from __future__ import annotations

import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from MidiScore import KeyEvent, Performance  # noqa: E402
from MusicPlayer import MusicPlayer  # noqa: E402

# 等待演奏线程结束的上限；正常用例都远小于这个值
_JOIN_TIMEOUT = 5.0


class FakeExecutor:
    """记录按键时刻的假执行器。"""

    def __init__(self):
        self.events: list[tuple[float, int, bool]] = []
        self.keyboard_ready = True
        self.fail_keys: set[int] = set()
        self._t0 = time.perf_counter()

    def is_keyboard_ready(self) -> bool:
        return self.keyboard_ready

    def key_down(self, vk: int) -> bool:
        self.events.append((time.perf_counter() - self._t0, vk, True))
        return vk not in self.fail_keys

    def key_up(self, vk: int) -> bool:
        self.events.append((time.perf_counter() - self._t0, vk, False))
        return vk not in self.fail_keys

    # ---- 断言辅助 ----

    def presses(self, vk: int) -> list[float]:
        return [t for t, key, down in self.events if key == vk and down]

    def releases(self, vk: int) -> list[float]:
        return [t for t, key, down in self.events if key == vk and not down]

    def is_balanced(self) -> bool:
        """每个键的按下/抬起必须一一配对，且结束时没有键还按着。"""
        held: set[int] = set()
        for _t, vk, down in self.events:
            if down:
                if vk in held:
                    return False
                held.add(vk)
            else:
                held.discard(vk)
        return not held


def _performance(spans: list[tuple[float, float, int]],
                 key_prefix: str = "K") -> Performance:
    """由 (按下时刻, 抬起时刻, vk) 列表构造一份演奏序列。"""
    events: list[KeyEvent] = []
    for press, release, vk in spans:
        events.append(KeyEvent(press, True, f"{key_prefix}{vk}", vk, vk))
        events.append(KeyEvent(release, False, f"{key_prefix}{vk}", vk, vk))
    events.sort(key=lambda e: (e.time, e.down))
    return Performance(
        events=events,
        note_count=len(spans),
        duration=max((e.time for e in events), default=0.0),
    )


def _wait_until(player: MusicPlayer, predicate, timeout: float = _JOIN_TIMEOUT) -> bool:
    deadline = time.perf_counter() + timeout
    while time.perf_counter() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return predicate()


class MusicPlayerBasicTest(unittest.TestCase):
    def setUp(self):
        self.executor = FakeExecutor()
        self.player = MusicPlayer(self.executor)
        self.completed: list[tuple[bool, bool]] = []
        self.player.on_complete = lambda ok, stopped: self.completed.append((ok, stopped))
        self.addCleanup(self.player.stop)

    def test_start_without_load_fails(self):
        self.assertFalse(self.player.start())
        self.assertFalse(self.player.is_playing())

    def test_load_rejects_empty_performance(self):
        self.assertFalse(self.player.load(Performance()))
        self.assertFalse(self.player.load(None))

    def test_plays_every_event_in_order(self):
        performance = _performance([(0.0, 0.04, 0x41), (0.06, 0.10, 0x42),
                                    (0.12, 0.16, 0x43)])
        self.assertTrue(self.player.load(performance, "测试曲"))
        self.assertTrue(self.player.start())
        self.assertTrue(_wait_until(self.player, lambda: not self.player.is_playing()))

        self.assertEqual(len(self.executor.events), 6)
        self.assertEqual([(vk, down) for _t, vk, down in self.executor.events],
                         [(0x41, True), (0x41, False),
                          (0x42, True), (0x42, False),
                          (0x43, True), (0x43, False)])
        self.assertTrue(self.executor.is_balanced())
        self.assertEqual(self.completed, [(True, False)])

    def test_timing_follows_the_score(self):
        performance = _performance([(0.0, 0.05, 0x41), (0.20, 0.25, 0x42)])
        self.player.load(performance)
        self.player.start()
        self.assertTrue(_wait_until(self.player, lambda: not self.player.is_playing()))

        first = self.executor.presses(0x41)[0]
        second = self.executor.presses(0x42)[0]
        self.assertLess(first, 0.06)
        # 第二个音在 0.2s 处，允许调度抖动
        self.assertAlmostEqual(second, 0.20, delta=0.06)
        # 音符时值被如实保持（0.05s）
        self.assertAlmostEqual(self.executor.releases(0x41)[0] - first, 0.05, delta=0.04)

    def test_chord_presses_multiple_keys_before_releasing(self):
        performance = _performance([(0.0, 0.10, 0x41), (0.0, 0.10, 0x42)])
        self.player.load(performance)
        self.player.start()
        self.assertTrue(_wait_until(self.player, lambda: not self.player.is_playing()))
        kinds = [down for _t, _vk, down in self.executor.events]
        self.assertEqual(kinds, [True, True, False, False])
        self.assertTrue(self.executor.is_balanced())

    def test_progress_and_position_advance(self):
        performance = _performance([(0.0, 0.05, 0x41), (0.15, 0.20, 0x42)])
        self.player.load(performance)
        self.assertEqual(self.player.get_progress(), (0, 4))
        self.assertEqual(self.player.get_meta()["note_count"], 2)

        self.player.start()
        self.assertTrue(_wait_until(
            self.player, lambda: self.player.get_progress()[0] >= 2))
        index, total = self.player.get_progress()
        self.assertEqual(total, 4)
        self.assertGreaterEqual(index, 2)
        self.assertTrue(_wait_until(self.player, lambda: not self.player.is_playing()))
        self.assertEqual(self.player.get_progress(), (4, 4))
        position, duration = self.player.get_position()
        self.assertAlmostEqual(duration, 0.20, places=3)
        self.assertAlmostEqual(position, duration, places=3)

    def test_start_twice_is_rejected(self):
        self.player.load(_performance([(0.0, 0.30, 0x41)]))
        self.assertTrue(self.player.start())
        self.assertFalse(self.player.start())
        self.player.stop()


class MusicPlayerControlTest(unittest.TestCase):
    def setUp(self):
        self.executor = FakeExecutor()
        self.player = MusicPlayer(self.executor)
        self.completed: list[tuple[bool, bool]] = []
        self.player.on_complete = lambda ok, stopped: self.completed.append((ok, stopped))
        self.addCleanup(self.player.stop)

    def test_stop_releases_held_keys(self):
        """中途停止必须把按住的键抬起来，否则游戏里那个音会一直响。"""
        self.player.load(_performance([(0.0, 2.0, 0x41)]))
        self.player.start()
        self.assertTrue(_wait_until(self.player, lambda: len(self.executor.presses(0x41)) == 1))
        self.player.stop()

        self.assertFalse(self.player.is_playing())
        self.assertEqual(len(self.executor.releases(0x41)), 1)
        self.assertTrue(self.executor.is_balanced())
        self.assertEqual(self.completed, [(False, True)])

    def test_stop_during_a_gap_still_finishes_cleanly(self):
        self.player.load(_performance([(0.0, 0.02, 0x41), (1.5, 1.52, 0x42)]))
        self.player.start()
        time.sleep(0.15)
        self.player.stop()
        self.assertFalse(self.player.is_playing())
        self.assertTrue(self.executor.is_balanced())
        self.assertEqual(self.executor.presses(0x42), [])

    def test_pause_releases_keys_and_shifts_timeline(self):
        """暂停时抬起按键；继续后时间轴整体后移，音符间隔保持不变。"""
        self.player.load(_performance([(0.0, 0.05, 0x41), (0.60, 0.65, 0x42)]))
        self.player.start()
        self.assertTrue(_wait_until(self.player, lambda: len(self.executor.releases(0x41)) == 1))

        paused_at = time.perf_counter() - self.executor._t0
        self.assertTrue(self.player.pause())
        self.assertTrue(self.player.is_paused())
        # 暂停不会让第二个音提前或延后触发
        time.sleep(0.35)
        self.assertEqual(self.executor.presses(0x42), [])

        resume_at = time.perf_counter() - self.executor._t0
        self.assertTrue(self.player.resume())
        self.assertFalse(self.player.is_paused())
        self.assertTrue(_wait_until(self.player, lambda: not self.player.is_playing()))

        second = self.executor.presses(0x42)[0]
        # 第二个音原本在 0.6s；暂停了多久，时间轴就该整体后移多久
        self.assertAlmostEqual(second, 0.60 + (resume_at - paused_at), delta=0.12)
        self.assertEqual(self.completed, [(True, False)])

    def test_pause_before_start_is_noop(self):
        self.assertFalse(self.player.pause())
        self.assertFalse(self.player.resume())
        self.assertEqual(self.player.toggle_pause(), "")

    def test_toggle_pause_round_trip(self):
        self.player.load(_performance([(0.0, 1.0, 0x41)]))
        self.player.start()
        self.assertTrue(_wait_until(self.player, lambda: len(self.executor.presses(0x41)) == 1))
        self.assertEqual(self.player.toggle_pause(), "paused")
        self.assertEqual(self.player.toggle_pause(), "resumed")
        self.player.stop()

    def test_stop_during_pause_wakes_thread(self):
        """暂停中的演奏线程卡在等待里，stop 必须能把它叫醒。"""
        self.player.load(_performance([(0.0, 1.0, 0x41), (1.2, 1.3, 0x42)]))
        self.player.start()
        self.assertTrue(_wait_until(self.player, lambda: len(self.executor.presses(0x41)) == 1))
        self.player.pause()
        time.sleep(0.05)
        started = time.perf_counter()
        self.player.stop()
        self.assertLess(time.perf_counter() - started, 2.0)
        self.assertFalse(self.player.is_playing())
        self.assertTrue(self.executor.is_balanced())

    def test_failed_key_injection_is_tolerated(self):
        """某个键注入失败不应该中断整首曲子。"""
        self.executor.fail_keys.add(0x41)
        self.player.load(_performance([(0.0, 0.05, 0x41), (0.10, 0.15, 0x42)]))
        self.player.start()
        self.assertTrue(_wait_until(self.player, lambda: not self.player.is_playing()))
        self.assertEqual(len(self.executor.presses(0x42)), 1)
        self.assertEqual(self.completed, [(True, False)])


class MusicPlayerOptionsTest(unittest.TestCase):
    def setUp(self):
        self.executor = FakeExecutor()
        self.player = MusicPlayer(self.executor)
        self.addCleanup(self.player.stop)

    def test_speed_multiplier_compresses_timeline(self):
        self.player.load(_performance([(0.0, 0.02, 0x41), (0.40, 0.42, 0x42)]))
        self.player.speed = 2.0
        started = time.perf_counter()
        self.player.start()
        self.assertTrue(_wait_until(self.player, lambda: not self.player.is_playing()))
        elapsed = time.perf_counter() - started
        # 2 倍速下 0.42s 的曲子应该 0.21s 左右播完
        self.assertLess(elapsed, 0.38)
        self.assertAlmostEqual(self.executor.presses(0x42)[0], 0.20, delta=0.08)

    def test_speed_is_clamped_to_a_sane_range(self):
        self.player.load(_performance([(0.0, 0.02, 0x41)]))
        self.player.speed = 0.0
        self.player.start()
        self.assertTrue(_wait_until(self.player, lambda: not self.player.is_playing()))
        self.assertGreaterEqual(self.player.speed, 0.1)

    def test_loop_repeats_the_whole_score(self):
        self.player.load(_performance([(0.0, 0.02, 0x41)]))
        self.player.set_loop_config(count=3, delay=0.0)
        self.player.start()
        self.assertTrue(_wait_until(self.player, lambda: not self.player.is_playing()))
        self.assertEqual(len(self.executor.presses(0x41)), 3)
        self.assertEqual(self.player.get_loop_progress(), (3, 3))

    def test_infinite_loop_only_ends_on_stop(self):
        self.player.load(_performance([(0.0, 0.02, 0x41)]))
        self.player.set_loop_config(count=0, delay=0.0)
        self.player.start()
        self.assertTrue(_wait_until(self.player, lambda: len(self.executor.presses(0x41)) >= 3))
        self.assertTrue(self.player.is_playing())
        self.player.stop()
        self.assertFalse(self.player.is_playing())

    def test_loop_delay_inserts_a_gap(self):
        self.player.load(_performance([(0.0, 0.02, 0x41)]))
        self.player.set_loop_config(count=2, delay=0.25)
        self.player.start()
        self.assertTrue(_wait_until(self.player, lambda: not self.player.is_playing()))
        presses = self.executor.presses(0x41)
        self.assertEqual(len(presses), 2)
        self.assertGreaterEqual(presses[1] - presses[0], 0.24)

    def test_set_loop_config_rejects_negative_values(self):
        self.player.set_loop_config(count=-5, delay=-1.0)
        self.assertEqual(self.player.loop_count, 0)
        self.assertEqual(self.player.loop_delay, 0.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
