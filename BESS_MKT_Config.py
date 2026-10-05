from __future__ import annotations
from dataclasses import dataclass


@dataclass(frozen=True)
class StrategyProfile:
    """逐日滚动优化中单种策略的差异化参数。"""
    name: str
    enable_fcr: bool
    enable_afrr: bool
    energy_market: str      # "da" / "idm" / "none"
    soc_end_min: float
    soc_end_max: float


STRATEGIES: dict[str, StrategyProfile] = {
    "joint": StrategyProfile(
        name="joint",
        enable_fcr=True,
        enable_afrr=True,
        energy_market="idm",
        soc_end_min=0.40,
        soc_end_max=0.60,
    ),
    "da_only": StrategyProfile(
        name="da_only",
        enable_fcr=False,
        enable_afrr=False,
        energy_market="da",
        soc_end_min=0.10,
        soc_end_max=0.90,
    ),
    "fcr_only": StrategyProfile(
        name="fcr_only",
        enable_fcr=True,
        enable_afrr=False,
        energy_market="none",
        soc_end_min=0.35,
        soc_end_max=0.65,
    ),
}


@dataclass(frozen=True)
class BESS_MKT_Config:
    # 电池与市场参数
    P_MAX: float = 10.0  # MW
    E_MAX: float = 10.0  # MWh
    SOC_MIN: float = 0.05
    SOC_MAX: float = 0.95
    ETA_CH: float = 0.93
    ETA_DIS: float = 0.93
    C_DEG: float = 1.0  # EUR/MWh
    # 初始及日末 SoC 目标（§2.7.1：双向 aFRR 申报以 50% 为基准工作点）
    SOC_INIT: float = 0.50
    SOC_END_MIN: float = 0.40   # 保证次日首个 4h 块仍有足够 aFRR UP 能量空间
    SOC_END_MAX: float = 0.60

    # 时间离散：15min * 96
    DT: float = 0.25  # h
    N_T: int = 96
    N_B: int = 6  # 4h blocks

    # FCR / aFRR 持续可用性保障参数 (h)
    # - TAU_FCR：FCR 有限储能 SoC 缓冲深度，覆盖 PQ2024 §3.1.6 四分量合计（对称）：
    #     危险态 0.50h + 前次激活残差 0.25h + SM延迟 0.25h + Reservebetrieb 0.08h ≈ 0.83h
    #   （若仅覆盖危险态可用 0.50；当前取完整 0.83 以满足全部 PQ 要求）
    # - TAU_AFRR_UP / TAU_AFRR_DN：aFRR 可交易功率对应的最小能量持续时间（§2.7.1：60 min）
    TAU_FCR: float = 0.83
    TAU_AFRR_UP: float = 1.0
    TAU_AFRR_DN: float = 1.0

    # PQ2024 §3.1.6：P_max ≥ 1.25 × P_VL（FCR 储能管理需预留 25% 功率裕量）
    # 可交易 FCR 功率上限 = P_MAX × FCR_MARKETABLE_RATIO = 20 × 0.80 = 16 MW
    FCR_MARKETABLE_RATIO: float = 0.80

    # FCR / aFRR 容量中标率
    PROB_FCR: float = 1.00
    PROB_AFRR_UP: float = 1.00
    PROB_AFRR_DN: float = 1.00

    # aFRR 激活率（期望净激活比例，基于历史统计：德国日均激活约 2h/24h）
    ALPHA_UP: float = 2 / 24   # ≈ 0.0833
    ALPHA_DN: float = 2 / 24   # ≈ 0.0833

    @property
    def BLOCK_SIZE(self) -> int:
        """4h block 内包含的离散时段数（如 DT=0.25h → 16）。"""
        return int(round(4.0 / self.DT))

    def block_of(self, t: int) -> int:
        """将时段索引 t (0..N_T-1) 映射为 4h block 索引 (0..N_B-1)。"""
        t = int(t)
        if t < 0 or t >= self.N_T:
            raise ValueError(f"t 越界: {t}, 允许范围 [0, {self.N_T - 1}]")
        return t // self.BLOCK_SIZE
