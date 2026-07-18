"""
PathPlanner — 基于锚点的路径扰动引擎。

提供两种可切换的策略：
  - 'sine':   正弦垂直扰动（平滑弧线，适合简单场景）
  - 'fitts':  Fitts 拟人化（加减速 + 过冲 + 微抖动，更自然）

两种策略都保证：总耗时不变，终点位移精确一致。
全程使用相对量，不依赖绝对坐标。
"""

from __future__ import annotations

import math
import random
import time


class PathPlanner:
    """路径扰动规划器（纯相对量模式，双策略可切换）。

    strategy='sine':
      在累积路径的垂直方向叠加正弦扰动，包络 sin(πu) 保证首尾扰动=0。

    strategy='fitts':
      模拟真实人类鼠标行为：
        ① 每步叠加高斯微抖动（σ 随距离自适应缩放）
        ② 垂直方向随机弧线偏移（模拟手臂自然弧度）
        ③ 一定概率产生过冲（冲过头再修正回来）
        ④ 最后一步精确校正，保证终点 100% 一致

    保证：
      - 总位移精确等于 (total_dx, total_dy)
      - 事件数量和时间戳完全不变
    """

    VALID_STRATEGIES = ("sine", "fitts")

    def __init__(
        self,
        strategy: str = "fitts",
        # sine 策略参数
        sine_amplitude_px: float = 10.0,
        sine_frequency: int = 2,
        # fitts 策略参数
        fitts_jitter_px: float = 2.0,
        fitts_arc_px: float = 8.0,
        fitts_overshoot_chance: float = 0.3,
        fitts_overshoot_px: float = 15.0,
    ):
        """
        Args:
            strategy: 扰动策略，'sine' 或 'fitts'。

            sine_amplitude_px: [sine] 最大垂直偏移像素。
            sine_frequency:    [sine] 扰动频率（完整周期数）。

            fitts_jitter_px:      [fitts] 每步微抖动标准差 (px)。
            fitts_arc_px:         [fitts] 垂直弧线最大偏移 (px)。
            fitts_overshoot_chance: [fitts] 过冲概率 (0~1)。
            fitts_overshoot_px:   [fitts] 过冲最大像素。
        """
        if strategy not in self.VALID_STRATEGIES:
            raise ValueError(f"未知策略 '{strategy}'，可选: {self.VALID_STRATEGIES}")
        self.strategy = strategy

        # sine
        self.sine_amplitude_px = sine_amplitude_px
        self.sine_frequency = sine_frequency

        # fitts
        self.fitts_jitter_px = fitts_jitter_px
        self.fitts_arc_px = fitts_arc_px
        self.fitts_overshoot_chance = fitts_overshoot_chance
        self.fitts_overshoot_px = fitts_overshoot_px

    # ---- 公开 API ---------------------------------------------------------------

    def generate_path(
        self,
        total_dx: int,
        total_dy: int,
        original_moves: list[dict],
        seed: int | None = None,
    ) -> list[dict]:
        """生成扰动后的 move 事件列表（相对移动量），总位移不变。"""
        if not original_moves:
            return []

        dist = math.sqrt(total_dx * total_dx + total_dy * total_dy)
        if dist < 5.0:
            return list(original_moves)

        rng = random.Random(
            seed if seed is not None
            else int(time.time() * 1000) ^ random.randint(0, 0xFFFF)
        )

        if self.strategy == "sine":
            return self._sine_strategy(total_dx, total_dy, original_moves, rng)
        else:
            return self._fitts_strategy(total_dx, total_dy, original_moves, rng)

    # ---- sine 策略 --------------------------------------------------------------

    def _sine_strategy(
        self, total_dx: int, total_dy: int,
        original_moves: list[dict], rng: random.Random,
    ) -> list[dict]:
        """正弦垂直扰动：在累积路径上叠加 sin 波垂直偏移。"""
        n = len(original_moves)
        dist = math.sqrt(total_dx * total_dx + total_dy * total_dy)

        # 累积位移
        cum_x, cum_y = self._cumulative(original_moves)
        # 归一化时间
        norm_times = self._norm_times(original_moves)
        if norm_times is None:
            return list(original_moves)

        perp_x, perp_y = -total_dy / dist, total_dx / dist
        freq = rng.randint(self.sine_frequency, self.sine_frequency + 2)
        phase = rng.uniform(0, 2 * math.pi)
        amplitude = rng.uniform(self.sine_amplitude_px * 0.5, self.sine_amplitude_px)

        # 叠加扰动
        perturbed_x, perturbed_y = [0.0] * (n + 1), [0.0] * (n + 1)
        for i, u in enumerate(norm_times):
            offset = amplitude * math.sin(freq * 2 * math.pi * u + phase) * math.sin(math.pi * u)
            perturbed_x[i + 1] = cum_x[i + 1] + perp_x * offset
            perturbed_y[i + 1] = cum_y[i + 1] + perp_y * offset

        # 强制首尾
        perturbed_x[0], perturbed_y[0] = 0.0, 0.0
        perturbed_x[n], perturbed_y[n] = float(total_dx), float(total_dy)

        return self._to_moves(perturbed_x, perturbed_y, original_moves, total_dx, total_dy)

    # ---- fitts 策略 -------------------------------------------------------------

    def _fitts_strategy(
        self, total_dx: int, total_dy: int,
        original_moves: list[dict], rng: random.Random,
    ) -> list[dict]:
        """Fitts 拟人化：微抖动 + 弧线 + 过冲 + 精确校正。"""
        n = len(original_moves)
        dist = math.sqrt(total_dx * total_dx + total_dy * total_dy)

        cum_x, cum_y = self._cumulative(original_moves)
        norm_times = self._norm_times(original_moves)
        if norm_times is None:
            return list(original_moves)

        perp_x, perp_y = -total_dy / dist, total_dx / dist

        # 自适应缩放：距离越远扰动越大（但设上限）
        scale = min(dist / 200.0, 1.5)
        jitter_sigma = self.fitts_jitter_px * scale
        arc_amp = self.fitts_arc_px * scale

        # 随机弧线参数
        arc_freq = rng.uniform(0.8, 1.5)
        arc_phase = rng.uniform(0, math.pi)

        # 过冲参数
        do_overshoot = rng.random() < self.fitts_overshoot_chance
        overshoot_pos = rng.uniform(0.70, 0.85) if do_overshoot else None
        overshoot_px = rng.uniform(self.fitts_overshoot_px * 0.3, self.fitts_overshoot_px) if do_overshoot else 0

        perturbed_x = [0.0] * (n + 1)
        perturbed_y = [0.0] * (n + 1)

        for i, u in enumerate(norm_times):
            # ① 高斯微抖动（各方向独立）
            jx = rng.gauss(0, jitter_sigma)
            jy = rng.gauss(0, jitter_sigma)

            # ② 垂直弧线偏移（sin(πu) 包络保证首尾=0）
            arc_offset = arc_amp * math.sin(arc_freq * math.pi * u + arc_phase) * math.sin(math.pi * u)
            jx += perp_x * arc_offset
            jy += perp_y * arc_offset

            # ③ 过冲：在 overshoot_pos 处沿运动方向冲过头，后续自然修正
            if do_overshoot and overshoot_pos is not None:
                # 过冲强度用 sigmoid 过渡：overshoot_pos 之前逐渐增加到最大，之后衰减回0
                if u < overshoot_pos:
                    # 逐渐增加（平滑曲线）
                    t_ratio = u / overshoot_pos
                    overshoot_strength = t_ratio * t_ratio * (3 - 2 * t_ratio)  # smoothstep
                else:
                    # 逐渐衰减
                    t_ratio = (u - overshoot_pos) / (1.0 - overshoot_pos)
                    overshoot_strength = 1.0 - t_ratio * t_ratio * (3 - 2 * t_ratio)
                # 沿运动方向（平行）偏移
                dir_x = total_dx / dist
                dir_y = total_dy / dist
                jx += dir_x * overshoot_px * overshoot_strength
                jy += dir_y * overshoot_px * overshoot_strength

            # 首尾点不扰动
            if i == 0 or i == n - 1:
                jx, jy = 0.0, 0.0

            perturbed_x[i + 1] = cum_x[i + 1] + jx
            perturbed_y[i + 1] = cum_y[i + 1] + jy

        # 强制首尾精确匹配
        perturbed_x[0], perturbed_y[0] = 0.0, 0.0
        perturbed_x[n], perturbed_y[n] = float(total_dx), float(total_dy)

        return self._to_moves(perturbed_x, perturbed_y, original_moves, total_dx, total_dy)

    # ---- 工具方法 ---------------------------------------------------------------

    @staticmethod
    def _cumulative(moves: list[dict]) -> tuple[list[int], list[int]]:
        """计算累积位移序列，长度 n+1（首元素为 0）。"""
        n = len(moves)
        cx = [0] * (n + 1)
        cy = [0] * (n + 1)
        for i, m in enumerate(moves):
            cx[i + 1] = cx[i] + int(m.get("x", 0))
            cy[i + 1] = cy[i] + int(m.get("y", 0))
        return cx, cy

    @staticmethod
    def _norm_times(moves: list[dict]) -> list[float] | None:
        """计算归一化时间 (0~1)，如果时间跨度=0 返回 None。"""
        times = [m.get("t", 0.0) for m in moves]
        t_start, t_end = times[0], times[-1]
        span = t_end - t_start
        if span <= 0:
            return None
        return [(t - t_start) / span for t in times]

    @staticmethod
    def _to_moves(
        perturbed_x: list[float], perturbed_y: list[float],
        original_moves: list[dict],
        total_dx: int, total_dy: int,
    ) -> list[dict]:
        """累积浮点路径 → 整数相对移动量，最后一步校正总位移。"""
        n = len(original_moves)
        result = []
        for i in range(n):
            dx = int(round(perturbed_x[i + 1])) - int(round(perturbed_x[i]))
            dy = int(round(perturbed_y[i + 1])) - int(round(perturbed_y[i]))
            result.append({
                "t": original_moves[i]["t"],
                "type": "move",
                "x": dx,
                "y": dy,
            })

        # 校正最后一步，确保总位移精确
        actual_dx = sum(e["x"] for e in result)
        actual_dy = sum(e["y"] for e in result)
        if actual_dx != total_dx or actual_dy != total_dy:
            result[-1]["x"] += total_dx - actual_dx
            result[-1]["y"] += total_dy - actual_dy

        return result
