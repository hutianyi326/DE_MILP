from __future__ import annotations
import numpy as np
import pulp
from BESS_MKT_Config import BESS_MKT_Config, STRATEGIES, StrategyProfile


class Optimizer:
    def __init__(self, config: BESS_MKT_Config):
        self.config = config
        self.block_size = config.BLOCK_SIZE
        if config.N_T % self.block_size != 0:
            raise ValueError("N_T 不能被 (4h / DT) 整除，block_of 无法定义。")
        if config.N_T != config.N_B * self.block_size:
            raise ValueError(
                f"须满足 N_T == N_B×(4h/DT)：当前 N_T={config.N_T}, "
                f"N_B={config.N_B}, block_size={self.block_size}。"
            )

    def block_of(self, t: int) -> int:
        return self.config.block_of(t)

    def _add_base_vars_and_constraints(
        self, mdl, d: dict, prefix: str, *,
        fix_fcr=None, fix_aup=None, fix_adn=None,
        soc_init_mwh: float | None = None,
        soc_end_min: float | None = None,
        soc_end_max: float | None = None,
    ):
        """
        创建基础变量和约束，返回变量字典。
        fix_fcr/fix_aup/fix_adn: 若传入 np.array(N_B)，则固定对应容量变量。
        soc_init_mwh: 覆盖 config.SOC_INIT（MWh 绝对值），用于逐日滚动传递。
        soc_end_min/max: 覆盖 config.SOC_END_MIN/MAX（比例值 0–1）。
        """
        c = self.config
        pfx = prefix

        p_ch = [pulp.LpVariable(f"{pfx}_pch_{t}", 0, c.P_MAX) for t in range(c.N_T)]
        p_dis = [pulp.LpVariable(f"{pfx}_pdis_{t}", 0, c.P_MAX) for t in range(c.N_T)]
        y = [pulp.LpVariable(f"{pfx}_y_{t}", cat="Binary") for t in range(c.N_T)]

        soc = [
            pulp.LpVariable(f"{pfx}_soc_{t}", c.E_MAX * c.SOC_MIN, c.E_MAX * c.SOC_MAX)
            for t in range(c.N_T + 1)
        ]

        if fix_fcr is not None:
            r_fcr = [pulp.LpVariable(f"{pfx}_rfcr_{b}", fix_fcr[b], fix_fcr[b]) for b in range(c.N_B)]
        else:
            # PQ2024 §3.1.6：P_max ≥ 1.25 × P_VL，可交易 FCR 上限 = P_MAX × FCR_MARKETABLE_RATIO
            r_fcr = [
                pulp.LpVariable(
                    f"{pfx}_rfcr_{b}", 0,
                    c.P_MAX * c.FCR_MARKETABLE_RATIO,
                    cat="Integer",
                )
                for b in range(c.N_B)
            ]

        if fix_aup is not None:
            r_aup = [pulp.LpVariable(f"{pfx}_raup_{b}", fix_aup[b], fix_aup[b]) for b in range(c.N_B)]
        else:
            r_aup = [pulp.LpVariable(f"{pfx}_raup_{b}", 0, c.P_MAX, cat="Integer") for b in range(c.N_B)]

        if fix_adn is not None:
            r_adn = [pulp.LpVariable(f"{pfx}_radn_{b}", fix_adn[b], fix_adn[b]) for b in range(c.N_B)]
        else:
            r_adn = [pulp.LpVariable(f"{pfx}_radn_{b}", 0, c.P_MAX, cat="Integer") for b in range(c.N_B)]

        actual_soc_init = soc_init_mwh if soc_init_mwh is not None else c.E_MAX * c.SOC_INIT
        actual_end_min = soc_end_min if soc_end_min is not None else c.SOC_END_MIN
        actual_end_max = soc_end_max if soc_end_max is not None else c.SOC_END_MAX

        mdl += soc[0] == actual_soc_init, f"{pfx}_soc_init"
        mdl += soc[c.N_T] >= c.E_MAX * actual_end_min, f"{pfx}_soc_end_lo"
        mdl += soc[c.N_T] <= c.E_MAX * actual_end_max, f"{pfx}_soc_end_hi"

        for t in range(c.N_T):
            b = self.block_of(t)

            # SOC 动态（15min 视角）
            mdl += (
                soc[t + 1]
                == soc[t]
                + c.ETA_CH * p_ch[t] * c.DT
                - (1.0 / c.ETA_DIS) * p_dis[t] * c.DT
                - d["alpha_up"][t] * r_aup[b] * c.DT / c.ETA_DIS
                + d["alpha_dn"][t] * r_adn[b] * c.DT * c.ETA_CH
            ), f"{pfx}_soc_dyn_{t}"

            # 充放电互斥
            mdl += p_ch[t] <= c.P_MAX * (1 - y[t]), f"{pfx}_ch_off_{t}"
            mdl += p_dis[t] <= c.P_MAX * y[t], f"{pfx}_dis_off_{t}"

            # 功率容量（功率 + 备用容量 <= 额定功率）
            mdl += p_dis[t] + r_fcr[b] + r_aup[b] <= c.P_MAX, f"{pfx}_cap_up_{t}"
            mdl += p_ch[t] + r_fcr[b] + r_adn[b] <= c.P_MAX, f"{pfx}_cap_dn_{t}"

            # 备用容量的能量可持续性约束（电网侧功率换算至电芯侧 SOC 变化量）：
            # 放电方向：电芯消耗 = 电网侧功率 / ETA_DIS（FCR 取最差放电侧）
            # 充电方向：电芯存入 = 电网侧功率 * ETA_CH（FCR 取最差充电侧）
            mdl += soc[t] >= (
                c.E_MAX * c.SOC_MIN
                + c.TAU_FCR * r_fcr[b] / c.ETA_DIS
                + c.TAU_AFRR_UP * r_aup[b] / c.ETA_DIS
            ), f"{pfx}_rsv_lo_{t}"
            mdl += soc[t] <= (
                c.E_MAX * c.SOC_MAX
                - c.TAU_FCR * r_fcr[b] * c.ETA_CH
                - c.TAU_AFRR_DN * r_adn[b] * c.ETA_CH
            ), f"{pfx}_rsv_hi_{t}"

        return {"p_ch": p_ch, "p_dis": p_dis, "y": y, "soc": soc, "r_fcr": r_fcr, "r_aup": r_aup, "r_adn": r_adn}

    def _validate_inputs(self, d: dict) -> None:
        """
        校验 ``read_file`` 等与 ``BESS_MKT_Config`` 对齐后的输入形状。
        能量价（DA / ID / aFRR 能量）须为 EUR/MWh，与 ``C_DEG`` 一致；长度须为 ``N_T``。
        容量价长度为 ``N_B``（4h 块均价，与 ``read_file`` 中对 15min 序列分块平均一致）。
        """
        c = self.config
        required = (
            "price_da",
            "price_intra_day",
            "price_afrr_eng_up",
            "price_afrr_eng_dn",
            "price_fcr_cap",
            "price_afrr_cap_up",
            "price_afrr_cap_dn",
            "prob_fcr",
            "prob_afrr_up",
            "prob_afrr_dn",
            "alpha_up",
            "alpha_dn",
        )
        for k in required:
            if k not in d:
                raise KeyError(f"inputs 缺少键 '{k}'（应与 In_And_Out.read_file 返回值一致）。")

        n_t, n_b = c.N_T, c.N_B
        for k in (
            "price_da",
            "price_intra_day",
            "price_afrr_eng_up",
            "price_afrr_eng_dn",
            "alpha_up",
            "alpha_dn",
        ):
            a = np.asarray(d[k], dtype=float).ravel()
            if a.size != n_t:
                raise ValueError(
                    f"'{k}' 长度须为 N_T={n_t}（当前 {a.size}），请确认 Input 为 15min×{n_t} 且与 config 一致。"
                )

        for k in (
            "price_fcr_cap",
            "price_afrr_cap_up",
            "price_afrr_cap_dn",
            "prob_fcr",
            "prob_afrr_up",
            "prob_afrr_dn",
        ):
            a = np.asarray(d[k], dtype=float).ravel()
            if a.size != n_b:
                raise ValueError(
                    f"'{k}' 长度须为 N_B={n_b}（当前 {a.size}），须与 4h 块数一致。"
                )

    def _obj_terms_for_strategy(
        self, d: dict, v: dict, profile: StrategyProfile,
    ) -> list:
        """根据 ``StrategyProfile`` 构建目标函数项列表。

        - ``energy_market="idm"``：使用 ``price_intra_day`` 作为能量套利价格。
        - ``energy_market="da"``：使用 ``price_da``。
        - ``energy_market="none"``：不含能量套利项（p_ch/p_dis 仅做 SOC 管理）。
        - 退化成本始终计入（含 aFRR 激活带来的期望充放电量）。
        - FCR / aFRR 容量与能量收入根据 ``enable_fcr``、``enable_afrr`` 控制
          （变量本身已通过 fix_* 锁零，此处对称跳过以避免多余线性项）。
        """
        c = self.config
        terms: list = []

        if profile.energy_market == "idm":
            price_energy = d["price_intra_day"]
        elif profile.energy_market == "da":
            price_energy = d["price_da"]
        else:
            price_energy = None

        for t in range(c.N_T):
            b = self.block_of(t)

            if price_energy is not None:
                terms.append(
                    price_energy[t] * (v["p_dis"][t] - v["p_ch"][t]) * c.DT
                )

            if profile.enable_afrr:
                terms.append(
                    d["price_afrr_eng_up"][t]
                    * d["prob_afrr_up"][b]
                    * d["alpha_up"][t]
                    * v["r_aup"][b]
                    * c.DT
                )
                terms.append(
                    -d["price_afrr_eng_dn"][t]
                    * d["prob_afrr_dn"][b]
                    * d["alpha_dn"][t]
                    * v["r_adn"][b]
                    * c.DT
                )

            terms.append(
                -c.C_DEG
                * (
                    v["p_ch"][t]
                    + v["p_dis"][t]
                    + d["alpha_up"][t] * v["r_aup"][b]
                    + d["alpha_dn"][t] * v["r_adn"][b]
                )
                * c.DT
            )

        for b in range(c.N_B):
            if profile.enable_fcr:
                terms.append(
                    d["price_fcr_cap"][b] * d["prob_fcr"][b] * v["r_fcr"][b]
                )
            if profile.enable_afrr:
                terms.append(
                    d["price_afrr_cap_up"][b]
                    * d["prob_afrr_up"][b]
                    * v["r_aup"][b]
                    * 4.0
                )
                terms.append(
                    d["price_afrr_cap_dn"][b]
                    * d["prob_afrr_dn"][b]
                    * v["r_adn"][b]
                    * 4.0
                )
        return terms

    def _extract_results(self, mdl, v: dict) -> dict:
        c = self.config
        status = pulp.LpStatus[mdl.status]
        total_rev = pulp.value(mdl.objective) if mdl.status == 1 else None
        return {
            "status": status,
            "total_revenue_eur": total_rev,
            "p_ch": np.array([x.varValue or 0 for x in v["p_ch"]]),
            "p_dis": np.array([x.varValue or 0 for x in v["p_dis"]]),
            "soc": np.array([x.varValue or 0 for x in v["soc"]]),
            "r_fcr": np.array([x.varValue or 0 for x in v["r_fcr"]]),
            "r_aup": np.array([x.varValue or 0 for x in v["r_aup"]]),
            "r_adn": np.array([x.varValue or 0 for x in v["r_adn"]]),
        }

    def build_and_solve(
        self,
        inputs: dict,
        *,
        strategy: str = "joint",
        soc_init_mwh: float | None = None,
        verbose: bool = True,
    ) -> dict:
        """
        根据策略求解单日 MILP 优化。

        Parameters
        ----------
        inputs : dict
            ``In_And_Out.read_file`` 返回的 ``days[date]`` 单日字典，
            含 ``N_T`` 个 15min 能量价与 ``N_B`` 个 4h 容量价等。
        strategy : str
            ``"joint"`` / ``"da_only"`` / ``"fcr_only"``，
            从 ``STRATEGIES`` 字典查找对应 ``StrategyProfile``。
        soc_init_mwh : float | None
            若非 None，以 MWh 绝对值覆盖 ``config.SOC_INIT``（用于逐日滚动传递）。
        verbose : bool
            是否打印求解器日志。
        """
        self._validate_inputs(inputs)

        if strategy not in STRATEGIES:
            raise ValueError(
                f"未知策略 '{strategy}'，可选：{list(STRATEGIES.keys())}"
            )
        profile = STRATEGIES[strategy]
        c = self.config

        fix_fcr = np.zeros(c.N_B) if not profile.enable_fcr else None
        fix_aup = np.zeros(c.N_B) if not profile.enable_afrr else None
        fix_adn = np.zeros(c.N_B) if not profile.enable_afrr else None

        mdl = pulp.LpProblem(strategy, pulp.LpMaximize)
        v = self._add_base_vars_and_constraints(
            mdl, inputs, strategy[0].upper(),
            fix_fcr=fix_fcr,
            fix_aup=fix_aup,
            fix_adn=fix_adn,
            soc_init_mwh=soc_init_mwh,
            soc_end_min=profile.soc_end_min,
            soc_end_max=profile.soc_end_max,
        )
        mdl += pulp.lpSum(
            self._obj_terms_for_strategy(inputs, v, profile)
        ), "obj"

        pulp.PULP_CBC_CMD(msg=verbose, timeLimit=120).solve(mdl)
        return self._extract_results(mdl, v)
