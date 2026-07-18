"""
PathPlanner — 基于锚点的路径扰动引擎。

给定两个锚点之间的位移向量 (dx, dy) 和原始 move 事件序列，
在直线路径上叠加垂直方向的正弦扰动，生成不同的移动轨迹。
保持总耗时和终点位移精确不变。

全程使用相对量，不依赖绝对坐标。
"""

from __future__ import annotations

import math
import random
import time


class PathPlanner:
    """路径扰动规划器（纯相对量模式）。

    扰动原理：
      1. 将原始相对移动量转为累积位移路径 (0,0) → (total_dx, total_dy)
      2. 在垂直于总位移的方向上叠加正弦扰动
      3. 扰动包络 sin(πu) 保证首尾点偏移量 = 0
      4. 首尾点精确强制匹配
      5. 转回相对移动量序列

    保证：
      - 总位移精确等于 (total_dx, total_dy)
      - 每个事件的 x/y 之和 = 原始之和
      - 中间步骤大小合理，不会产生巨大跳变
    """

    def __init__(
        self,
        sine_amplitude_px: float = 10.0,
        sine_frequency: int = 2,
    ):
        """
        Args:
            sine_amplitude_px: 正弦扰动的最大垂直偏移像素数。
            sine_frequency: 扰动频率（完整周期数），越大路径弯曲越多。
        """
        self.sine_amplitude_px = sine_amplitude_px
        self.sine_frequency = sine_frequency

    # ---- 公开 API ---------------------------------------------------------------

    def generate_path(
        self,
        total_dx: int,
        total_dy: int,
        original_moves: list[dict],
        seed: int | None = None,
    ) -> list[dict]:
        """生成扰动后的 move 事件列表（相对移动量），总位移不变。

        Args:
            total_dx, total_dy: 两锚点之间的总位移向量
                （由累加原始相对移动量得到）。
            original_moves: 原始 move 事件列表（含 t, x, y 字段，x/y 为相对量）。
            seed: 随机种子（可选）。

        Returns:
            扰动后的 move 事件列表（dict 格式，与原始 move 事件格式一致）。
            所有事件的 x/y 相对量之和精确等于 (total_dx, total_dy)。
        """
        if not original_moves:
            return []

        # 位移太短无需扰动（< 5px 基本是原地微调）
        dist = math.sqrt(total_dx * total_dx + total_dy * total_dy)
        if dist < 5.0:
            return list(original_moves)

        rng = random.Random(
            seed if seed is not None
            else int(time.time() * 1000) ^ random.randint(0, 0xFFFF)
        )

        # 1. 计算原始路径的累积位移
        n = len(original_moves)
        cum_x = [0] * (n + 1)
        cum_y = [0] * (n + 1)
        for i, m in enumerate(original_moves):
            cum_x[i + 1] = cum_x[i] + int(m.get("x", 0))
            cum_y[i + 1] = cum_y[i] + int(m.get("y", 0))

        # 2. 计算归一化时间参数 (0~1)
        orig_times = [m.get("t", 0.0) for m in original_moves]
        t_start = orig_times[0]
        t_end = orig_times[-1]
        t_span = t_end - t_start
        if t_span <= 0:
            return list(original_moves)
        norm_times = [(t - t_start) / t_span for t in orig_times]

        # 3. 计算垂直方向单位向量
        perp_x = -total_dy / dist
        perp_y = total_dx / dist

        # 4. 生成随机扰动参数
        freq = rng.randint(self.sine_frequency, self.sine_frequency + 2)
        phase = rng.uniform(0, 2 * math.pi)
        amplitude = rng.uniform(
            self.sine_amplitude_px * 0.5,
            self.sine_amplitude_px,
        )

        # 5. 在每个采样点上叠加垂直扰动
        #    perturbation(u) = amplitude * sin(freq * 2π * u + phase) * sin(πu)
        #    sin(πu) 保证 u=0 和 u=1 时扰动 = 0
        perturbed_x = [0] * (n + 1)
        perturbed_y = [0] * (n + 1)
        for i, u in enumerate(norm_times):
            # 正弦扰动 × sin(πu) 包络
            sine_val = math.sin(freq * 2 * math.pi * u + phase)
            envelope = math.sin(math.pi * u)
            offset = amplitude * sine_val * envelope
            perturbed_x[i + 1] = cum_x[i + 1] + perp_x * offset
            perturbed_y[i + 1] = cum_y[i + 1] + perp_y * offset

        # 6. 强制首尾精确匹配
        perturbed_x[0] = 0
        perturbed_y[0] = 0
        perturbed_x[n] = total_dx
        perturbed_y[n] = total_dy

        # 7. 累积路径 → 相对移动量
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

        # 8. 校正最后一步，确保总位移精确
        actual_dx = sum(e["x"] for e in result)
        actual_dy = sum(e["y"] for e in result)
        if actual_dx != total_dx or actual_dy != total_dy:
            result[-1]["x"] += total_dx - actual_dx
            result[-1]["y"] += total_dy - actual_dy

        return result
