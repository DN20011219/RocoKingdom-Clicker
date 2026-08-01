"""PathPlanner — 基于锚点的路径扰动引擎。

提供四种可切换的策略：
  - 'straight':   纯直线（均匀插值，无扰动，用于调试）
  - 'sine':       正弦垂直扰动（平滑弧线，适合简单场景）
  - 'fitts':      Fitts 拟人化（加减速 + 过冲 + 微抖动，更自然）
  - 'neuromotor': 间歇预测控制模型（Intermittent Predictive Control，
                   基于 2021-2025 顶会前沿研究）

四种策略都保证：总耗时不变，终点位移精确一致。
全程使用相对量，不依赖绝对坐标。
"""

from __future__ import annotations

import math
import random
import time


class PathPlanner:
    """路径扰动规划器（纯相对量模式，四策略可切换）。

    strategy='straight':
      纯直线模式：忽略输入路径，输出均匀插值直线，无任何扰动。
      用于调试桥接路径逻辑、验证位移计算与事件拼接是否正确。

    strategy='sine':
      在累积路径的垂直方向叠加正弦扰动，包络 sin(πu) 保证首尾扰动=0。

    strategy='fitts':
      模拟真实人类鼠标行为：
        ① 每步叠加高斯微抖动（σ 随距离自适应缩放）
        ② 垂直方向随机弧线偏移（模拟手臂自然弧度）
        ③ 一定概率产生过冲（冲过头再修正回来）
        ④ 最后一步精确校正，保证终点 100% 一致

    strategy='neuromotor':
      间歇预测控制模型 (Intermittent Predictive Control)，融合 2021-2025 顶会研究：
        ① 间歇子运动链（Do & Chang, CHI 2021，离散弹道子运动序列）
        ② 感知延迟 + 噪声重规划（Klar et al., 2025，主动推理）
        ③ Lognormal 速度脉冲（Rudakov et al., 2025，非对称钟形）
        ④ 熵控相位随机（Liu et al., 2024，快=高熵，慢=低熵）

    保证：
      - 总位移精确等于 (total_dx, total_dy)
      - 事件数量和时间戳完全不变
    """

    VALID_STRATEGIES = ("sine", "fitts", "neuromotor", "straight")

    # 类级调用计数器：确保即使同一纳秒内多次调用，种子也不重复
    _call_counter: int = 0

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
        # neuromotor 策略参数（间歇预测控制模型）
        nm_lognormal_sigma: float = 0.65,
        nm_perception_noise: float = 0.03,
        nm_entropy_alpha: float = 0.6,
        nm_lateral_drift: float = 0.06,
        nm_max_corrections: int = 2,
    ):
        """
        Args:
            strategy: 扰动策略，'straight'、'sine'、'fitts' 或 'neuromotor'。

            sine_amplitude_px: [sine] 最大垂直偏移像素。
            sine_frequency:    [sine] 扰动频率（完整周期数）。

            fitts_jitter_px:      [fitts] 每步微抖动标准差 (px)。
            fitts_arc_px:         [fitts] 垂直弧线最大偏移 (px)。
            fitts_overshoot_chance: [fitts] 过冲概率 (0~1)。
            fitts_overshoot_px:   [fitts] 过冲最大像素。

            nm_lognormal_sigma:   [neuromotor] Lognormal 速度剖面宽度 (0.4~0.9)。
            nm_perception_noise:  [neuromotor] 感知噪声系数 (占距离比例)。
            nm_entropy_alpha:     [neuromotor] 熵控强度 (0=无, 1=最强)。
            nm_lateral_drift:     [neuromotor] 横向漂移幅度 (占距离比例)。
            nm_max_corrections:   [neuromotor] 最大修正子运动次数。
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

        # neuromotor（间歇预测控制）
        self.nm_lognormal_sigma = nm_lognormal_sigma
        self.nm_perception_noise = nm_perception_noise
        self.nm_entropy_alpha = nm_entropy_alpha
        self.nm_lateral_drift = nm_lateral_drift
        self.nm_max_corrections = nm_max_corrections

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

        if seed is not None:
            rng = random.Random(seed)
        else:
            # 三重唯一性保证：纳秒时间戳 ^ 64位随机 ^ 调用计数器
            # 确保同一循环内多段调用绝不产生相同种子
            PathPlanner._call_counter += 1
            auto_seed = (
                time.time_ns()
                ^ random.getrandbits(64)
                ^ (PathPlanner._call_counter * 0x9E3779B97F4A7C15)
            )
            rng = random.Random(auto_seed)

        if self.strategy == "straight":
            return self._straight_strategy(total_dx, total_dy, original_moves)
        elif self.strategy == "sine":
            return self._sine_strategy(total_dx, total_dy, original_moves, rng)
        elif self.strategy == "neuromotor":
            return self._neuromotor_strategy(total_dx, total_dy, original_moves, rng)
        else:
            return self._fitts_strategy(total_dx, total_dy, original_moves, rng)

    # ---- straight 策略 ----------------------------------------------------------

    def _straight_strategy(
        self, total_dx: int, total_dy: int,
        original_moves: list[dict],
    ) -> list[dict]:
        """纯直线：忽略输入路径形状，输出均匀插值直线，无任何扰动。

        用于调试桥接路径逻辑是否正确（隔离扰动影响）。
        时间戳保持与原始事件一致，位移均匀分配到每一步。
        """
        n = len(original_moves)
        result: list[dict] = []
        prev_cum_x, prev_cum_y = 0, 0
        for i, m in enumerate(original_moves):
            tcx = round(total_dx * (i + 1) / n)
            tcy = round(total_dy * (i + 1) / n)
            result.append({
                "t": m.get("t", 0.0),
                "type": "move",
                "x": tcx - prev_cum_x,
                "y": tcy - prev_cum_y,
            })
            prev_cum_x, prev_cum_y = tcx, tcy
        return result

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
        """Fitts 拟人化：平滑低频扰动 + 弧线 + 过冲 + 精确校正。

        核心：用多个随机频率正弦波叠加产生平滑连续扰动，
        避免独立噪声导致的锯齿状跳动，模拟手臂惯性运动。
        """
        n = len(original_moves)
        dist = math.sqrt(total_dx * total_dx + total_dy * total_dy)

        cum_x, cum_y = self._cumulative(original_moves)
        norm_times = self._norm_times(original_moves)
        if norm_times is None:
            return list(original_moves)

        perp_x, perp_y = -total_dy / dist, total_dx / dist

        # 自适应缩放：距离越远扰动越大（但设上限）
        scale = min(dist / 200.0, 1.5)
        arc_amp = self.fitts_arc_px * scale
        jitter_amp = self.fitts_jitter_px * scale * 3  # 平滑波振幅可更大

        # 生成平滑低频噪声：叠加 2~4 个随机频率的正弦波
        # 每个分量：random_amp * sin(random_freq * π * u + random_phase)
        # 低频范围 0.5~3.0，确保步间变化平滑
        n_harmonics = rng.randint(2, 4)
        harm_x = []
        harm_y = []
        for _ in range(n_harmonics):
            freq = rng.uniform(0.5, 3.0)
            phase_x = rng.uniform(0, 2 * math.pi)
            phase_y = rng.uniform(0, 2 * math.pi)
            amp_x = jitter_amp * rng.uniform(0.3, 1.0)
            amp_y = jitter_amp * rng.uniform(0.3, 1.0)
            harm_x.append((amp_x, freq, phase_x))
            harm_y.append((amp_y, freq, phase_y))

        # 弧线参数
        arc_freq = rng.uniform(0.8, 1.5)
        arc_phase = rng.uniform(0, math.pi)

        # 过冲参数
        do_overshoot = rng.random() < self.fitts_overshoot_chance
        overshoot_pos = rng.uniform(0.70, 0.85) if do_overshoot else None
        overshoot_px = rng.uniform(
            self.fitts_overshoot_px * 0.3, self.fitts_overshoot_px,
        ) if do_overshoot else 0

        perturbed_x = [0.0] * (n + 1)
        perturbed_y = [0.0] * (n + 1)

        for i, u in enumerate(norm_times):
            # ① 平滑低频噪声（多正弦波叠加，步间自然相关）
            jx, jy = 0.0, 0.0
            for amp, freq, phase in harm_x:
                jx += amp * math.sin(freq * math.pi * u + phase)
            for amp, freq, phase in harm_y:
                jy += amp * math.sin(freq * math.pi * u + phase)

            # 包络 sin(πu)：保证首尾扰动 → 0
            envelope = math.sin(math.pi * u)
            jx *= envelope
            jy *= envelope

            # ② 垂直弧线偏移
            arc_offset = arc_amp * math.sin(
                arc_freq * math.pi * u + arc_phase,
            ) * envelope
            jx += perp_x * arc_offset
            jy += perp_y * arc_offset

            # ③ 过冲：沿运动方向冲过头再平滑修正
            if do_overshoot and overshoot_pos is not None:
                if u < overshoot_pos:
                    t_ratio = u / overshoot_pos
                    overshoot_strength = t_ratio * t_ratio * (3 - 2 * t_ratio)
                else:
                    t_ratio = (u - overshoot_pos) / (1.0 - overshoot_pos)
                    overshoot_strength = 1.0 - t_ratio * t_ratio * (3 - 2 * t_ratio)
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

        return self._to_moves(
            perturbed_x, perturbed_y, original_moves, total_dx, total_dy,
        )

    # ---- neuromotor 策略（间歇预测控制） -----------------------------------------
    #
    # 参考文献 (References):
    #
    # [1] Do, S., Chang, M., & Lee, B. (2021). A Simulation Model of
    #     Intermittently Controlled Point-and-Click Behaviour.
    #     In Proceedings of the 2021 CHI Conference on Human Factors
    #     in Computing Systems (CHI '21). ACM.
    #     DOI: 10.1145/3411764.3445514
    #     — BUMP 模型：间歇控制 + 弹道子运动 + 感知更新
    #     Code: https://github.com/dodoseung (CHI '21)
    #
    # [2] Klar, M., Stein, S., Paterson, F., Williamson, J.H.,
    #     & Murray-Smith, R. (2025). An Active Inference Model of
    #     Mouse Point-and-Click Behaviour.
    #     arXiv:2510.14611. University of Glasgow.
    #     — 主动推理 + 感知延迟 + 不确定性驱动修正
    #     Code: https://github.com/mkl4r/AIF-Pointing
    #
    # [3] Rudakov, E., Shock, J., Lappi, O., & Cowley, B.U. (2025).
    #     SSSUMO: Real-Time Semi-Supervised Submovement Decomposition.
    #     arXiv:2507.08028. University of Helsinki.
    #     — Lognormal 速度脉冲 + 2-3 Hz 子运动节律
    #     Code: https://github.com/evgenii-rudakov/sssumo
    #
    # [4] Liu, J., Cui, Z., Ge, W., & Zhan, P. (2024).
    #     DMTG: A Human-Like Mouse Trajectory Generation Bot Based
    #     on Entropy-Controlled Diffusion Networks.
    #     arXiv:2410.18233. University of Michigan / Sichuan University.
    #     — 熵控扩散模型，相位自适应随机性
    #     Code: https://github.com/SaluRamos/mouse-ai
    #
    # 经典理论基础:
    #   - Flash & Hogan (1985): Minimum Jerk, J. Neuroscience 5(7)
    #   - Harris & Wolpert (1998): Signal-Dependent Noise, Nature 394
    #   - Plamondon (1995): Sigma-Lognormal, Biol. Cybernetics 72(4)
    #

    def _neuromotor_strategy(
        self, total_dx: int, total_dy: int,
        original_moves: list[dict], rng: random.Random,
    ) -> list[dict]:
        """间歇预测控制策略 (Intermittent Predictive Control)。

        融合 2021-2025 顶会前沿研究：
          ① 间歇子运动链 [1] — 离散弹道子运动序列
          ② 感知延迟 + 噪声重规划 [2] — 主动推理
          ③ Lognormal 速度脉冲 [3] — 非对称钟形速度剖面
          ④ 熵控相位随机 [4] — 快=高熵，慢=低熵

        参考文献编号见上方类级别注释。
        """
        n = len(original_moves)
        dist = math.sqrt(total_dx * total_dx + total_dy * total_dy)

        norm_times = self._norm_times(original_moves)
        if norm_times is None:
            return list(original_moves)

        # 时间跨度（秒）
        times = [m.get("t", 0.0) for m in original_moves]
        t_span = times[-1] - times[0]
        if t_span <= 0:
            return list(original_moves)

        # 方向单位向量 & 垂直方向
        dir_x, dir_y = total_dx / dist, total_dy / dist
        perp_x, perp_y = -dir_y, dir_x

        # 自适应缩放
        scale = min(dist / 200.0, 2.5)

        # ================================================================
        # ① 间歇子运动分解 [1] (Do & Chang, CHI 2021)
        # 人类指向运动由 1-3 个离散弹道子运动组成，
        # 每个子运动独立规划，之间通过感知更新连接。
        # ================================================================
        if dist > 400:
            n_sub = rng.randint(2, 1 + self.nm_max_corrections)
        elif dist > 150:
            n_sub = rng.randint(1, min(2, self.nm_max_corrections))
        else:
            n_sub = 1

        # 子运动时间边界（归一化 0~1）
        # 第一个子运动通常最短（弹道快速阶段）
        boundaries = [0.0]
        remaining = 1.0
        for k in range(n_sub - 1):
            if k == 0:
                # 主弹道阶段占 55-75% 时间
                frac = rng.uniform(0.55, 0.75)
            else:
                frac = rng.uniform(0.4, 0.7)
            boundary = remaining * frac
            boundaries.append(boundaries[-1] + boundary)
            remaining -= boundary
        boundaries.append(1.0)

        # 每个子运动的 lognormal sigma（控制速度剖面形状）[3]
        sigmas = []
        for k in range(n_sub):
            # 后续子运动 sigma 稍小（更对称 = 更精确的修正）
            s = self.nm_lognormal_sigma * rng.uniform(0.85, 1.15)
            if k > 0:
                s *= rng.uniform(0.8, 1.0)  # 修正子运动更对称
            sigmas.append(s)

        # ================================================================
        # ② 感知模型 [2] (Klar et al., 2025, Active Inference)
        # 每次子运动结束后，感知当前位置（含噪声），
        # 然后基于感知位置重新规划下一段子运动。
        # 感知不确定性 → 修正子运动自然涌现。
        # ================================================================
        perception_sigma = self.nm_perception_noise * dist

        # 子运动目标点（沿运动方向的累积位移）
        # 第一个子运动瞄准目标 + 感知误差（可能过冲）
        sub_targets_along = []  # 每个子运动的终点（沿运动方向）
        sub_targets_lateral = []  # 每个子运动的终点（垂直方向）

        # 主弧线参数（整体横向弧度）
        arc_sign = rng.choice([-1.0, 1.0])
        arc_amp = dist * self.nm_lateral_drift * rng.uniform(0.7, 1.3)

        for k in range(n_sub):
            if k == n_sub - 1:
                # 最后一个子运动必须精确到达终点
                target_along = float(dist)
                target_lateral = 0.0
            else:
                # 中间子运动：瞄准目标 + 感知噪声 [2]
                # 弹道阶段通常覆盖大部分距离
                if k == 0:
                    coverage = rng.uniform(0.85, 1.05)  # 可能过冲
                else:
                    coverage = rng.uniform(0.90, 1.0)
                target_along = dist * coverage
                # 感知噪声导致横向偏差
                target_lateral = rng.gauss(0, perception_sigma * 0.5)

            sub_targets_along.append(target_along)
            sub_targets_lateral.append(target_lateral)

        # ================================================================
        # ③ Lognormal 速度脉冲参数 [3] (Rudakov et al., 2025)
        # 每个子运动的速度剖面为 lognormal PDF：
        #   v(t) = (1 / (t·σ·√(2π))) · exp(-(ln(t)-μ)² / (2σ²))
        # 位置为 lognormal CDF：
        #   s(t) = ½ · (1 + erf((ln(t)-μ) / (σ√2)))
        # 峰值速度出现在 t_peak = exp(μ - σ²)
        # ================================================================
        # 对每个子运动，设定峰值在 30-40% 处
        # t_peak/T = exp(μ - σ²) → μ = ln(t_peak/T) + σ²
        mus = []
        for k in range(n_sub):
            sigma_k = sigmas[k]
            peak_ratio = rng.uniform(0.28, 0.40)
            mu_k = math.log(peak_ratio) + sigma_k * sigma_k
            mus.append(mu_k)

        # ================================================================
        # ④ 熵控参数 [4] (Liu et al., 2024, DMTG)
        # 快速弹道阶段 = 高熵（大随机性）
        # 慢速修正阶段 = 低熵（精确控制）
        # ================================================================
        entropy_base = self.nm_entropy_alpha * scale

        # 平滑熵控噪声：用 2~3 个低频正弦波叠加替代逐步独立高斯，
        # 消除帧间锯齿抖动，保持速度连续性（类似 fitts 策略优化思路）
        n_entropy_harm = rng.randint(2, 3)
        entropy_harm_x = []
        entropy_harm_y = []
        for _ in range(n_entropy_harm):
            freq = rng.uniform(1.5, 4.0)  # 中低频，步间平滑
            phase_x = rng.uniform(0, 2 * math.pi)
            phase_y = rng.uniform(0, 2 * math.pi)
            amp = entropy_base * dist * 0.008 * rng.uniform(0.5, 1.0)
            entropy_harm_x.append((amp, freq, phase_x))
            entropy_harm_y.append((amp * 0.7, freq, phase_y))

        # OU 漂移状态（横向随机游走）
        ou_state = 0.0
        ou_theta = rng.uniform(3.0, 6.0)  # 回复速率
        ou_sigma = arc_amp * 0.25  # 扩散强度（稍降，减少高频抖动）

        # 生成路径
        perturbed_x = [0.0] * (n + 1)
        perturbed_y = [0.0] * (n + 1)

        # 子运动边界平滑过渡宽度（归一化时间）
        blend_width = 0.06

        prev_u = 0.0
        for i, u in enumerate(norm_times):
            if i == 0:
                perturbed_x[1] = 0.0
                perturbed_y[1] = 0.0
                prev_u = u
                continue

            dt = max(u - prev_u, 1e-6)

            # 确定当前属于哪个子运动（含边界平滑混合）
            k = 0
            for kk in range(n_sub):
                if u <= boundaries[kk + 1]:
                    k = kk
                    break
            else:
                k = n_sub - 1

            # 子运动内局部进度 (0~1)
            seg_start = boundaries[k]
            seg_end = boundaries[k + 1]
            seg_duration = max(seg_end - seg_start, 1e-6)
            local_t = min(max((u - seg_start) / seg_duration, 1e-6), 1.0)

            # ③ Lognormal CDF 位置 [3]
            sigma_k = sigmas[k]
            mu_k = mus[k]
            s = self._lognormal_cdf(local_t, mu_k, sigma_k)

            # 子运动起点/终点
            if k == 0:
                start_along = 0.0
                start_lateral = 0.0
            else:
                start_along = sub_targets_along[k - 1]
                start_lateral = sub_targets_lateral[k - 1]
            end_along = sub_targets_along[k]
            end_lateral = sub_targets_lateral[k]

            # 沿运动方向 & 横向位置
            along = start_along + (end_along - start_along) * s
            lateral_base = start_lateral + (end_lateral - start_lateral) * s

            # 子运动边界平滑过渡：在边界附近混合前后两段的位置，
            # 避免速度突变产生的不连续感
            if k > 0 and (u - seg_start) < blend_width:
                # 混合因子：0(完全用前段终点) → 1(完全用当前段)
                blend_t = (u - seg_start) / blend_width
                blend = blend_t * blend_t * (3.0 - 2.0 * blend_t)  # smoothstep
                # 前一段终点位置
                prev_end_along = sub_targets_along[k - 1]
                prev_end_lateral = sub_targets_lateral[k - 1]
                along = prev_end_along + (along - prev_end_along) * blend
                lateral_base = prev_end_lateral + (lateral_base - prev_end_lateral) * blend

            # 主弧线偏移（全局平滑弧度，首尾为零）
            arc_offset = arc_amp * math.sin(math.pi * u) * arc_sign

            # ④ 熵控平滑噪声 [4] (DMTG 2024)
            # 用预生成的正弦波叠加，按速度剖面调制幅度：
            # 速度越大 → 熵越高 → 振幅越大；首尾包络归零
            log_t = math.log(max(local_t, 1e-6))
            vel_proxy = math.exp(
                -(log_t - mu_k) ** 2 / (2.0 * sigma_k * sigma_k)
            ) / max(local_t, 0.05)
            vel_norm = min(vel_proxy / 3.0, 1.0)

            # 速度调制 + sin(πu) 首尾包络
            vel_mod = (0.25 + 0.75 * vel_norm) * math.sin(math.pi * u)
            noise_x = 0.0
            noise_y = 0.0
            for amp, freq, phase in entropy_harm_x:
                noise_x += amp * math.sin(freq * 2.0 * math.pi * u + phase)
            for amp, freq, phase in entropy_harm_y:
                noise_y += amp * math.sin(freq * 2.0 * math.pi * u + phase)
            noise_x *= vel_mod
            noise_y *= vel_mod

            # OU 横向漂移（均值回复随机游走，本身已平滑）
            dW = rng.gauss(0, 1) * math.sqrt(dt)
            ou_state += -ou_theta * ou_state * dt + ou_sigma * dW
            drift_envelope = math.sin(math.pi * u)

            # 合成最终位置
            total_along = along + noise_x
            total_lateral = (
                lateral_base + arc_offset + ou_state * drift_envelope + noise_y
            )

            pos_x = dir_x * total_along + perp_x * total_lateral
            pos_y = dir_y * total_along + perp_y * total_lateral

            # 首尾点不扰动
            if i == n - 1:
                pos_x, pos_y = float(total_dx), float(total_dy)

            perturbed_x[i + 1] = pos_x
            perturbed_y[i + 1] = pos_y
            prev_u = u

        # 强制首尾精确匹配
        perturbed_x[0], perturbed_y[0] = 0.0, 0.0
        perturbed_x[n], perturbed_y[n] = float(total_dx), float(total_dy)

        return self._to_moves(
            perturbed_x, perturbed_y, original_moves, total_dx, total_dy,
        )

    # ---- neuromotor 辅助函数 ---------------------------------------------------

    @staticmethod
    def _lognormal_cdf(t: float, mu: float, sigma: float) -> float:
        """Lognormal CDF: P(T ≤ t) = ½(1 + erf((ln(t)-μ)/(σ√2)))。

        [3] Rudakov et al. (2025): 子运动速度剖面为 lognormal 脉冲，
        其累积分布给出 S 形位置曲线（非对称，峰值速度提前）。
        """
        if t <= 0.0:
            return 0.0
        if t >= 1.0:
            return 1.0
        z = (math.log(t) - mu) / (sigma * math.sqrt(2.0))
        return 0.5 * (1.0 + math.erf(z))

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
