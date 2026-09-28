"""
MIDI 自动演奏引擎。

把 MidiScore.build_performance 编出来的按键事件序列按时间轴注入键盘，
对外暴露与 PlaybackEngine 一致的控制接口（start / pause / resume / stop /
is_playing / get_progress），因此 ClickerManager 和 GUI 可以用同一套状态机
来管理「回放」「脚本」「演奏」三种会话。

实现上有三点必须注意：

1. **绝对时间调度**：每个事件的目标时刻 = 起始时刻 + 事件时间 / 速度，
   不用「sleep(间隔) 累加」的写法，否则误差会随音符数一路累积，一首两分钟的
   曲子播到后面就明显跟不上拍子。
2. **定时器精度**：Windows 默认时钟中断是 15.6ms，time.sleep(0.005) 实际会睡
   到 15ms 以上。演奏前调用 winmm.timeBeginPeriod(1) 把精度提到 1ms，结束后
   timeEndPeriod(1) 还原（不还原会一直拖累整机功耗）。
3. **绝不留卡键**：暂停、停止、异常、播完，任何退出路径都会抬起所有还按住的键。
   漏掉一次就会让游戏里某个音一直响，用户只能自己再去按那个键。
"""

from __future__ import annotations

import ctypes
import logging
import threading
import time
from typing import Callable, Optional

from MidiScore import Performance

# 单次 sleep 的上限：够小才能保证暂停/停止在 10ms 内被响应
_SLEEP_SLICE = 0.005
# 暂停轮询间隔：这里不需要毫秒级精度，20ms 足够且省 CPU
_PAUSE_POLL = 0.02


def _time_begin_period(milliseconds: int = 1) -> bool:
    """把系统时钟中断精度提到 milliseconds（失败不影响演奏，只是节奏会松一点）。"""
    try:
        return ctypes.windll.winmm.timeBeginPeriod(milliseconds) == 0
    except Exception:
        return False


def _time_end_period(milliseconds: int = 1) -> None:
    try:
        ctypes.windll.winmm.timeEndPeriod(milliseconds)
    except Exception:
        pass


class MusicPlayer:
    """按时间轴演奏一份 Performance。"""

    def __init__(self, executor, logger: Optional[logging.Logger] = None):
        """
        Args:
            executor: ActionScript.ActionExecutor，用它的 key_down/key_up 注入按键。
                      复用执行器是为了不再多开一个 Interception 上下文。
        """
        self.logger = logger or logging.getLogger("music_player")
        self._executor = executor

        self._performance: Optional[Performance] = None
        self._name: str = ""

        self.speed: float = 1.0
        self.loop_count: int = 1        # 0 = 无限循环
        self.loop_delay: float = 0.0    # 每轮之间的间隔秒数

        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._pause_event = threading.Event()
        self._pause_event.set()

        self._playing = False
        self._paused = False
        self._pause_offset = 0.0
        self._start_perf = 0.0
        self._event_index = 0
        self._loop_current = 0
        self._position = 0.0
        self._timer_resolution_raised = False

        # 当前按住的虚拟键码；任何退出路径都要靠它把键全部抬起来
        self._held_vks: set[int] = set()
        self._held_lock = threading.Lock()

        # 播完回调：(success, stopped)，在演奏线程里调用
        self.on_complete: Optional[Callable[[bool, bool], None]] = None

    # ---- 载入 ----

    def load(self, performance: Performance, name: str = "") -> bool:
        """载入一份编译好的演奏序列。"""
        if performance is None or not performance.events:
            self.logger.warning("演奏序列为空，未载入")
            return False
        self._performance = performance
        self._name = name or "MIDI 演奏"
        self._event_index = 0
        self._position = 0.0
        self.logger.info("已载入演奏序列 %s：%d 个事件 / %d 个音符 / %.1f 秒",
                         self._name, len(performance.events),
                         performance.note_count, performance.duration)
        return True

    def set_loop_config(self, count: int = 1, delay: float = 0.0) -> None:
        """设置循环次数（0 = 无限）与每轮间隔。"""
        self.loop_count = max(0, int(count))
        self.loop_delay = max(0.0, float(delay))

    def get_meta(self) -> dict:
        performance = self._performance
        return {
            "name": self._name,
            "note_count": performance.note_count if performance else 0,
            "event_count": len(performance.events) if performance else 0,
            "duration": performance.duration if performance else 0.0,
            "key_count": performance.key_count if performance else 0,
        }

    # ---- 控制 ----

    def start(self) -> bool:
        """启动演奏。返回是否成功启动线程。"""
        if self._playing:
            self.logger.warning("演奏已在进行中")
            return False
        if not self._performance or not self._performance.events:
            self.logger.error("没有可演奏的序列，请先载入曲谱")
            return False

        self._stop_event.clear()
        self._pause_event.set()
        self._paused = False
        self._pause_offset = 0.0
        self._event_index = 0
        self._loop_current = 0
        self._position = 0.0
        self.speed = max(0.1, min(4.0, float(self.speed or 1.0)))
        self._playing = True

        self._thread = threading.Thread(target=self._run, name="MusicPlayer", daemon=True)
        self._thread.start()
        self.logger.info("演奏已启动: %s（速度 %.2fx，循环 %s）", self._name, self.speed,
                         "无限" if self.loop_count == 0 else f"{self.loop_count} 次")
        return True

    def stop(self) -> None:
        """停止演奏（不保留进度），并抬起所有按住的键。"""
        if not self._playing:
            return
        self._stop_event.set()
        self._pause_event.set()
        thread = self._thread
        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=2.0)
        self._release_all()
        self.logger.info("演奏已停止: %s（事件 %d/%d）", self._name,
                         self._event_index, self._total_events())

    def pause(self) -> bool:
        """暂停演奏。暂停期间抬起所有按键，避免某个音一直响着。"""
        if not self._playing or self._paused:
            return False
        self._paused = True
        self._pause_event.clear()
        self._release_all()
        self.logger.info("演奏已暂停: %s", self._name)
        return True

    def resume(self) -> bool:
        """继续演奏。"""
        if not self._playing or not self._paused:
            return False
        self._paused = False
        self._pause_event.set()
        self.logger.info("演奏已继续: %s", self._name)
        return True

    def toggle_pause(self) -> str:
        """切换暂停状态，返回 'paused' / 'resumed' / ''。"""
        if self.pause():
            return "paused"
        if self.resume():
            return "resumed"
        return ""

    def is_playing(self) -> bool:
        return self._playing

    def is_paused(self) -> bool:
        return self._playing and self._paused

    def _total_events(self) -> int:
        return len(self._performance.events) if self._performance else 0

    def get_progress(self) -> tuple[int, int]:
        """返回 (已完成事件数, 总事件数)。"""
        return self._event_index, self._total_events()

    def get_loop_progress(self) -> tuple[int, int]:
        """返回 (当前轮次, 总轮次)；总轮次为 0 表示无限循环。"""
        return max(1, self._loop_current), self.loop_count

    def get_position(self) -> tuple[float, float]:
        """返回 (当前播放位置秒, 单轮总时长秒)。"""
        duration = self._performance.duration if self._performance else 0.0
        return min(self._position, duration), duration

    # ---- 演奏线程 ----

    def _run(self) -> None:
        stopped = False
        success = False
        self._timer_resolution_raised = _time_begin_period(1)
        try:
            loops_done = 0
            while not self._stop_event.is_set():
                loops_done += 1
                self._loop_current = loops_done
                if not self._play_once():
                    stopped = True
                    break
                if self.loop_count and loops_done >= self.loop_count:
                    success = True
                    break
                if self.loop_delay > 0 and not self._sleep_interruptible(self.loop_delay):
                    stopped = True
                    break
            else:
                stopped = True
            success = success and not stopped
        except Exception as exc:
            self.logger.error("演奏线程异常: %s", exc, exc_info=True)
            success = False
        finally:
            self._release_all()
            if self._timer_resolution_raised:
                _time_end_period(1)
                self._timer_resolution_raised = False
            self._playing = False
            self._paused = False
            self._pause_event.set()
            self.logger.info("演奏结束: %s（%s）", self._name,
                             "已停止" if stopped else "已播完")
            if self.on_complete:
                try:
                    self.on_complete(success, stopped)
                except Exception as exc:
                    self.logger.warning("on_complete 回调异常: %s", exc)

    def _play_once(self) -> bool:
        """播放一轮。返回 False 表示被停止（含异常），True 表示这一轮正常播完。"""
        performance = self._performance
        if performance is None:
            return False
        events = performance.events
        total = len(events)
        speed = max(0.1, self.speed)
        self._pause_offset = 0.0
        self._start_perf = time.perf_counter()
        index = 0

        while index < total:
            if self._stop_event.is_set():
                return False
            event = events[index]
            if not self._wait_until(event.time, speed):
                return False

            if event.down:
                self._press(event.vk)
            else:
                self._lift(event.vk)
            index += 1
            self._event_index = index
            self._position = min(event.time, performance.duration)

        # 收尾：谱子最后一个事件之后可能还有键按着（异常谱面），一律抬起
        self._release_all()
        self._position = performance.duration
        self._event_index = total
        return True

    def _wait_until(self, event_time: float, speed: float) -> bool:
        """睡到 event_time 对应的时刻。返回 False 表示被停止。

        暂停就地处理，把暂停时长累加进 _pause_offset。目标时刻必须在循环里
        每次重算：如果沿用暂停前算好的 target，暂停期间流逝的时间就会把当前
        音符到下一个音符的间隔吃掉，继续之后曲子会凭空变快。
        """
        while True:
            if self._stop_event.is_set():
                return False
            if not self._pause_event.is_set():
                paused_at = time.perf_counter()
                while not self._pause_event.is_set():
                    if self._stop_event.is_set():
                        return False
                    time.sleep(_PAUSE_POLL)
                self._pause_offset += time.perf_counter() - paused_at
                continue
            remaining = (self._start_perf + event_time / speed
                         + self._pause_offset) - time.perf_counter()
            if remaining <= 0:
                return True
            time.sleep(min(_SLEEP_SLICE, remaining))

    def _sleep_interruptible(self, seconds: float) -> bool:
        """可被停止打断的睡眠，返回 False 表示被停止。"""
        deadline = time.perf_counter() + seconds
        while not self._stop_event.is_set():
            remaining = deadline - time.perf_counter()
            if remaining <= 0:
                return True
            if not self._pause_event.is_set():
                time.sleep(_PAUSE_POLL)
                deadline += _PAUSE_POLL
                continue
            time.sleep(min(_SLEEP_SLICE, remaining))
        return False

    # ---- 按键注入 ----

    def _press(self, vk: int) -> None:
        if self._executor.key_down(vk):
            with self._held_lock:
                self._held_vks.add(vk)
        else:
            self.logger.warning("按键按下失败 VK=0x%02x", vk)

    def _lift(self, vk: int) -> None:
        with self._held_lock:
            held = vk in self._held_vks
            self._held_vks.discard(vk)
        # 即使没记录到按下也补一次抬起：按下和抬起分别注入，
        # 中间任何一次失败都可能让游戏侧一直认为这个键按着
        self._executor.key_up(vk)
        if not held:
            self.logger.debug("抬起未记录的按键 VK=0x%02x", vk)

    def _release_all(self) -> None:
        """抬起所有还按住的键。"""
        with self._held_lock:
            pending = list(self._held_vks)
            self._held_vks.clear()
        for vk in pending:
            try:
                self._executor.key_up(vk)
            except Exception as exc:
                self.logger.error("抬起按键失败 VK=0x%02x: %s", vk, exc)
        if pending:
            self.logger.debug("已抬起 %d 个按键", len(pending))
