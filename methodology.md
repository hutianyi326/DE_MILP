# Methodology — 代码关键流程与设计思路

## 1. In_And_Out.read_file：多日数据读取与缺失补齐

### 1.1 背景

原始 `read_file` 仅支持单日优化：读取 Excel 后只取排序后的第一个日期，输出长度为
`N_T=96`（15min × 96 = 24h）的价格向量。为支持**逐日滚动多策略优化**，需将其改造为
多日读取函数，同时升级缺失数据处理逻辑。

### 1.2 修改后的处理流程

```text
① 读取 Excel "Input" sheet，列重命名，解析 date / slot
② 数值清洗（to_numeric），dropna(date, slot)，groupby 聚合
③ _expand_hourly_slots_if_needed（24 行整点 → 96 行 15min）
④ _ensure_96_slots_per_day（每日补齐 0..95 共 96 个 slot，缺失处为 NaN）
⑤ 丢弃「完全缺失日」（某日所有价格列在 96 slot 全为 NaN → 移除 + warning）
⑥ 智能补齐 value_cols（DA、FCR、aFRR 容量 / 能量价）
⑦ 用补齐后的 DA 生成缺失的 ID 价格（正态抽样 N(DA, σ)）
⑧ 残余 NaN 兜底 fillna(0.0)
⑨ 记录补齐质量报告
⑩ 写回 "Input_filled" sheet + "Imputation_Report" sheet
⑪ 遍历所有可用日期，逐日构建 day_dict → 组装多日返回值
```

> **与原流程的关键差异**：
> - ID 价格生成（原步骤②）推迟到价格列补齐之后（新步骤⑦），
>   确保用于锚定正态分布的 DA 值已被插值填充。
> - 不再 `sel_date = sorted(...)[0]` 只取首日，改为遍历全部日期。
> - `fillna(0.0)` 从主逻辑降级为最终兜底。

### 1.3 完全缺失日处理

**判定标准**：某日所有 `value_cols` 在 96 个 slot 上全部为 NaN。

**处理**：从数据中移除该日期，不做补齐（整日补齐过于失真），打印 warning。

> 注：如果某日期在原始 Excel 中根本没有出现（无任何行），
> `_ensure_96_slots_per_day` 不会为它创建行——天然被忽略。

### 1.4 缺失数据补齐策略（逐日独立，不跨日插值）

| 列 | 方法 | 代码标识 | 理由 |
|---|---|---|---|
| `price_da` | 线性插值 + bfill/ffill 延伸首尾 | `linear_interp` | DA 价格时间连续，15min 间变化平缓 |
| `price_afrr_eng_up_hourly` | 同 DA | `linear_interp` | aFRR 能量价同样时间连续 |
| `price_afrr_eng_dn_hourly` | 同 DA | `linear_interp` | 同上 |
| `price_fcr_cap_hourly` | 4h block 内 ffill/bfill → 跨 block ffill/bfill | `block_ffill` | FCR 按 4h 块出清，块内应一致 |
| `price_afrr_cap_up_hourly` | 同 FCR | `block_ffill` | aFRR 容量价也按 4h 出清 |
| `price_afrr_cap_dn_hourly` | 同 FCR | `block_ffill` | 同上 |
| `price_intra_day` | N(DA_imputed, σ) 正态抽样 | `normal_from_da` | 复用现有逻辑，但基于补齐后的 DA |
| 残余 NaN（极端情况） | fillna(0.0) | `fallback_zero` | 兜底，极少触发 |

**逐日独立**的原因：电力市场价格在午夜有结构性跳变（日前市场按天出清），
跨日插值会混淆 23:45 和次日 00:00 的价格。

### 1.5 补齐质量报告（Imputation_Report sheet）

写入同一 Excel 文件，sheet 名 `Imputation_Report`，分两个区域：

**区域一：Summary（从第 1 行开始）**

| Date | Column | Total Slots | Missing Count | Missing Rate | Method |
|------|--------|-------------|---------------|--------------|--------|
| 2026/4/1 | price_da | 96 | 3 | 3.1% | linear_interp |
| 2026/4/1 | price_fcr_cap_hourly | 96 | 0 | 0.0% | — |

**区域二：Detail（Summary 下方空两行）**

| Date | Slot | Time | Column | Imputed Value | Method |
|------|------|------|--------|---------------|--------|
| 2026/4/1 | 5 | 01:15 | price_da | 45.32 | linear_interp |

实现方式：补齐前记录 `missing_before = qh[cols].isna().copy()`，
补齐后逐单元格对比，收集从 NaN → 非 NaN 的记录。

### 1.6 返回值格式

```python
{
    "days": {                    # dict[date, day_dict]，按日期排序
        date(2026,4,1): {
            "price_da":        np.array(96),   # 与 Optimizer 接口一致
            "price_intra_day": np.array(96),
            "price_fcr_cap":   np.array(6),    # 4h block 均价
            "prob_fcr":        np.array(6),
            "price_afrr_cap_up": np.array(6),
            "price_afrr_cap_dn": np.array(6),
            "prob_afrr_up":    np.array(6),
            "prob_afrr_dn":    np.array(6),
            "price_afrr_eng_up": np.array(96),
            "price_afrr_eng_dn": np.array(96),
            "alpha_up":        np.array(96),
            "alpha_dn":        np.array(96),
            "date":            np.array(96),   # 全部填当日日期
            "hour":            np.array(96),   # 0..23 各重复 4 次
            "selected_date":   date(2026,4,1),
        },
        date(2026,4,2): { ... },
    },
    "available_dates": [date(2026,4,1), date(2026,4,2), ...],
    "hourly_input_filled": qh,   # 完整多日 DataFrame（含补齐后数据）
}
```

每个 `day_dict` 的内部结构与 `Optimizer.build_and_solve` 的 `_validate_inputs`
所要求的格式完全一致（N_T=96 的向量 + N_B=6 的向量），因此优化器无需改动。

### 1.7 对下游模块的影响

| 模块 | 影响 | 本次是否改动 |
|------|------|-------------|
| `Optimizer.py` | 需支持多策略与 SOC 滚动注入 | 已改动（见 §3） |
| `main.py` | 需从 `data["days"]` 按日迭代调用优化器 | 后续步骤 |
| `print_result` | 需支持多日 × 多策略结果汇总导出 | 后续步骤 |

---

## 2. BESS_MKT_Config.py：策略配置

### 2.1 新增 `StrategyProfile` dataclass

为三种策略提供差异化参数，与 `BESS_MKT_Config` 平级定义在同一文件中。

```python
@dataclass(frozen=True)
class StrategyProfile:
    name: str               # "joint" / "da_only" / "fcr_only"
    enable_fcr: bool        # 是否启用 FCR 容量申报
    enable_afrr: bool       # 是否启用 aFRR 容量/能量
    energy_market: str      # "da" / "idm" / "none"
    soc_end_min: float      # 日末 SOC 下限（比例值 0–1）
    soc_end_max: float      # 日末 SOC 上限（比例值 0–1）
```

### 2.2 `STRATEGIES` 字典

| 策略 | enable_fcr | enable_afrr | energy_market | soc_end 范围 | 说明 |
|------|:---:|:---:|:---:|:---:|------|
| `joint` | True | True | `"idm"` | 0.40–0.60 | 全开：FCR+aFRR+IDM 联合优化 |
| `da_only` | False | False | `"da"` | 0.10–0.90 | 纯日前现货套利 |
| `fcr_only` | True | False | `"none"` | 0.35–0.65 | 纯 FCR 容量，无主动能量交易 |

### 2.3 `energy_market` 字段设计理由

不使用简单的 `enable_idm: bool`，因为 DA-only 策略在日前市场交易应使用
`price_da` 而非 `price_intra_day`。三种取值语义明确：

- `"idm"`：使用 `price_intra_day`（reserve 持仓后在日内连续市场调仓）
- `"da"`：使用 `price_da`（纯日前套利，按 DA 出清价结算）
- `"none"`：不含能量套利项（`p_ch/p_dis` 仅服务于 SOC 管理）

### 2.4 `soc_end_min/max` 差异化理由

方案 B 的核心思想是 **SOC 连续传递**（每天末尾 SOC → 下一天初始值）。
差异化的日末 SOC 约束是一种软保护：

- **Joint（0.40–0.60）**：最严格，为次日多市场 reserve 预留 SOC 空间
- **DA-only（0.10–0.90）**：最宽松，允许更大幅度套利
- **FCR-only（0.35–0.65）**：中等，需保持 FCR 能量缓冲余量

### 2.5 不改已有 `BESS_MKT_Config` 字段

原有 `SOC_INIT`、`SOC_END_MIN`、`SOC_END_MAX` 保留为默认值/后备——
当不传入 `StrategyProfile` 参数时，`Optimizer` 回退到 config 默认值。

### 2.6 `BLOCK_SIZE` 属性与 `block_of` 方法

将 4h block 时段计算逻辑集中到 `BESS_MKT_Config` 中，避免在 `Optimizer` 和
`In_And_Out` 中重复定义：

```python
@property
def BLOCK_SIZE(self) -> int:
    return int(round(4.0 / self.DT))  # DT=0.25h → 16

def block_of(self, t: int) -> int:
    return int(t) // self.BLOCK_SIZE   # t ∈ [0, N_T)
```

`Optimizer.block_of` 和 `In_And_Out._build_day_rows` 均委托给此方法，
`In_And_Out._block_of_t` 已移除。

### 2.7 `ALPHA_UP/DN` 和 `PROB_*` 不需要按策略设零

`alpha_up/dn` 在模型中始终乘以 `r_aup[b]` 或 `r_adn[b]`。
当 `enable_afrr=False` 时，`r_aup/r_adn` 通过 `fix_aup/fix_adn=0` 锁零，
alpha × 0 ≡ 0，无需额外处理。`prob_fcr/prob_afrr_*` 同理。

---

## 3. Optimizer.py：多策略求解与 SOC 滚动注入

### 3.1 改动概览

| 改动点 | 位置 | 说明 |
|--------|------|------|
| import 更新 | 第 6 行 | 新增 `STRATEGIES`, `StrategyProfile` |
| `_add_base_vars_and_constraints` 签名 | 新增 `soc_init_mwh`, `soc_end_min`, `soc_end_max` | SOC 初值/末端外部注入 |
| `_full_obj_terms` → `_obj_terms_for_strategy` | 替换 | 按 `StrategyProfile` 分支构建目标函数 |
| `_solve_joint` 删除 | — | 逻辑合并入 `build_and_solve` |
| `build_and_solve` 重写 | 新增 `strategy`, `soc_init_mwh` 参数 | 统一入口，按策略配置约束和目标 |

### 3.2 `_add_base_vars_and_constraints` 改动

**SOC 初始值注入**：

```python
# 旧：硬编码 config 值
mdl += soc[0] == c.E_MAX * c.SOC_INIT

# 新：支持外部传入（MWh 绝对值）
actual_soc_init = soc_init_mwh if soc_init_mwh is not None else c.E_MAX * c.SOC_INIT
mdl += soc[0] == actual_soc_init
```

**SOC 末端约束注入**：

```python
# 旧：硬编码 config 值
mdl += soc[N_T] >= c.E_MAX * c.SOC_END_MIN

# 新：支持 StrategyProfile 覆盖（比例值）
actual_end_min = soc_end_min if soc_end_min is not None else c.SOC_END_MIN
mdl += soc[N_T] >= c.E_MAX * actual_end_min
```

### 3.3 `_obj_terms_for_strategy` 目标函数

根据 `StrategyProfile` 的三个开关控制目标函数组成：

| 目标项 | Joint | DA-only | FCR-only |
|--------|:---:|:---:|:---:|
| 能量套利（`price_energy × (p_dis - p_ch)`） | IDM 价 | DA 价 | 无 |
| aFRR 能量收入 | 有 | 无 | 无 |
| FCR 容量收入 | 有 | 无 | 有 |
| aFRR 容量收入 | 有 | 无 | 无 |
| 退化成本 | 有 | 有 | 有 |

退化成本始终计入（包括 `alpha_up × r_aup` 项），但由于 DA-only 和 FCR-only
中 `r_aup/r_adn` 被锁零，这些项自动为零。

### 3.4 `build_and_solve` 新接口

```python
def build_and_solve(
    self, inputs, *, strategy="joint", soc_init_mwh=None, verbose=True
) -> dict:
```

内部流程：
1. `_validate_inputs(inputs)` — 校验输入形状（不变）
2. 从 `STRATEGIES[strategy]` 获取 `StrategyProfile`
3. 根据 `enable_fcr/enable_afrr` 生成 `fix_fcr/fix_aup/fix_adn`
4. 调用 `_add_base_vars_and_constraints`，传入 SOC 初值和末端参数
5. 调用 `_obj_terms_for_strategy` 构建目标函数
6. 求解并返回结果

### 3.5 向后兼容

- `build_and_solve(inputs)` 无额外参数时，默认 `strategy="joint"`、
  `soc_init_mwh=None`，行为与改动前完全一致
- `_validate_inputs` 不变：所有策略仍接收完整 `day_dict`（含所有价格字段）
- `_extract_results` 不变：返回格式统一，被锁零的变量自然为零

### 3.6 对下游的影响

| 模块 | 影响 | 是否需改动 |
|------|------|-----------|
| `main.py` | 调用需改为双层循环（策略×日期），传入 `strategy` 和 `soc_init_mwh` | 已完成（见 §4） |
| `In_And_Out.print_result` | 需根据策略选择正确的能量价格基准（DA vs IDM） | 已完成（见 §5） |

---

## 4. main.py：逐策略 × 逐日滚动调用

### 4.1 设计原则

- 不使用函数或 class 封装，保持脚本式平铺调用
- `strategies_to_run` 列表控制本次运行哪些策略（可 1 种或多种）
- 每种策略独立维护 `soc_carry`（日间 SOC 传递）

### 4.2 调用流程

```text
data = read_file(...)                         # 一次性读取多日
for strategy in strategies_to_run:            # 外层循环：策略
    soc_carry = E_MAX × SOC_INIT              #   首日初始 SOC
    for dt in dates:                          #   内层循环：日期
        res = optimizer.build_and_solve(      #     单日求解
            day_data, strategy=strategy,
            soc_init_mwh=soc_carry,
        )
        results[strategy][dt] = {res, inputs} #     收集结果
        soc_carry = res["soc"][-1]            #     SOC 传递
print_result({file_path, results}, config)    # 一次性导出
```

### 4.3 SOC 滚动传递

每种策略的 SOC 链独立：`soc_carry` 在策略外层循环开头重置为
`config.E_MAX * config.SOC_INIT`，日内由 `Optimizer` 根据 `soc_end_min/max`
约束控制末端范围，末端值 `res["soc"][-1]` 传递给下一天的 `soc_init_mwh`。

---

## 5. In_And_Out.print_result：多日 × 多策略结果导出

### 5.1 设计原则

- `bundle["results"]` 为 `dict[strategy_name, dict[date, entry]]`，
  有几种策略就导出几种，不强制要求全部三种
- 单策略时行为与旧版类似（一个详情 sheet + summary）
- 能量价格基准按策略自动切换

### 5.2 `bundle` 输入格式

```python
bundle = {
    "file_path": str,
    "results": {
        "joint": {
            date(2026,4,1): {"opt_result": dict, "inputs": dict},
            date(2026,4,2): {"opt_result": dict, "inputs": dict},
        },
        # 可选：更多策略...
    },
}
```

### 5.3 内部结构

拆为两层：

- `_build_day_rows(opt_result, inputs, config, strategy_name)` → `list[dict]`
  - 构建单日 96 行明细
  - 根据 `STRATEGIES[strategy_name].energy_market` 选择价格基准
  - `"idm"` → `price_intra_day`；`"da"` → `price_da`；`"none"` → 零向量
    （与 Optimizer 目标函数一致：FCR-only 不含能量套利项）
- `print_result(bundle, config)` → str
  - 外层双循环组装 DataFrame
  - 同时收集 summary 行

### 5.4 能量价格列重命名

原列名 `ID Charge Cost` / `ID Discharge Income` 暗示只用日内市场价格，
改为 `Energy Charge Cost` / `Energy Discharge Income`，语义更准确
（DA-only 策略中这些列使用 DA 价格计算）。

### 5.5 输出 Excel 结构

| Sheet 名 | 内容 | 行数 |
|-----------|------|------|
| `result_<策略名>` | 该策略全部日期明细（96 × D） | 每策略一个 |
| `summary` | 每日 × 每策略汇总（长表） | D × N_策略 |

**Summary 列**：Date, Strategy, FCR Revenue, AFRR UP Revenue,
AFRR DN Revenue, Energy Charge Cost, Energy Discharge Income,
AFRR DN Energy Cost, AFRR UP Energy Income, 退化成本, Total Revenue

### 5.6 输出文件命名

- 单策略：`Result_<策略名>_YYYYMMDD_HHMMSS.xlsx`
- 多策略：`Result_compare_YYYYMMDD_HHMMSS.xlsx`

---

## 6. MILP Mathematical Formulation and Current Model Assumptions (English)

This section documents the current mathematical formulation used for the German BESS market model. The model is a deterministic mixed-integer linear program (MILP). It co-optimizes energy arbitrage and ancillary-service capacity within the same dispatch problem, subject to battery operating limits, power competition and reserve energy requirements.

### 6.1 Time indices and notation

- `t = 0, ..., N_T - 1`: 15-minute operating intervals within one day;
- `b = 0, ..., N_B - 1`: 4-hour ancillary-service product blocks;
- `b(t) = floor(t / BLOCK_SIZE)`, where `BLOCK_SIZE = 16` for `DT = 0.25 h`;
- `DT = 0.25 h`, `N_T = 96`, and `N_B = 6`;
- for the 7 MW / 16.7 MWh case, `P_MAX = 7 MW` and `E_MAX = 16.7 MWh`.

### 6.2 Decision variables

| Symbol | Meaning | Type / unit |
|---|---|---|
| `p_ch(t)` | Battery charging power | continuous, MW |
| `p_dis(t)` | Battery discharging power | continuous, MW |
| `SOC(t)` | Stored energy at the beginning of interval `t` | continuous, MWh |
| `y(t)` | Charging/discharging status | binary |
| `r_fcr(b)` | FCR capacity offered in block `b` | integer, MW |
| `r_up(b)` | aFRR upward capacity offered in block `b` | integer, MW |
| `r_dn(b)` | aFRR downward capacity offered in block `b` | integer, MW |

### 6.3 Objective function

For the joint strategy, the model maximizes expected operating revenue as follows. The energy price `pi_E(t)` is the IDM price for `joint`, the DA price for `da_only`, and zero for `fcr_only`.

```text
Maximize R =

SUM_t [
    pi_E(t) * (p_dis(t) - p_ch(t)) * DT
  + q_up(b) * pi_up_E(t) * alpha_up(t) * r_up(b) * DT
  - q_dn(b) * pi_dn_E(t) * alpha_dn(t) * r_dn(b) * DT
  - C_deg * (
        p_ch(t) + p_dis(t)
      + alpha_up(t) * r_up(b)
      + alpha_dn(t) * r_dn(b)
    ) * DT
]

+ SUM_b [
      q_fcr(b) * pi_fcr_C(b) * r_fcr(b)
    + 4 * q_up(b) * pi_up_C(b) * r_up(b)
    + 4 * q_dn(b) * pi_dn_C(b) * r_dn(b)
]
```

The revenue terms are interpreted as follows:

- energy arbitrage revenue is the energy price multiplied by net discharge energy;
- aFRR UP energy is an expected revenue from upward activation;
- aFRR DN energy is an expected energy cost from downward activation;
- FCR capacity price is treated as EUR/MW per 4-hour product block in the current implementation;
- aFRR capacity prices are treated as EUR/(MW·h) and are therefore multiplied by 4 hours;
- `C_deg` is the degradation cost per MWh of modeled throughput.

The strategy switches are implemented as follows:

| Strategy | FCR enabled | aFRR enabled | Energy-market price | End-of-day SOC range |
|---|:---:|:---:|---|---:|
| `joint` | Yes | Yes | IDM | 40%–60% |
| `da_only` | No | No | DA | 10%–90% |
| `fcr_only` | Yes | No | None | 35%–65% |

When a market is disabled, its capacity variables are fixed to zero and its corresponding revenue terms become zero.

### 6.4 State-of-charge dynamics

```text
SOC(t+1) =
    SOC(t)
  + eta_ch * p_ch(t) * DT
  - p_dis(t) * DT / eta_dis
  - alpha_up(t) * r_up(b) * DT / eta_dis
  + eta_ch * alpha_dn(t) * r_dn(b) * DT
```

The model treats aFRR activation through expected activation rates. aFRR UP consumes battery energy, while aFRR DN stores energy in the battery.

### 6.5 Core operating constraints

#### Power competition

```text
p_dis(t) + r_fcr(b) + r_up(b) <= P_MAX

p_ch(t) + r_fcr(b) + r_dn(b) <= P_MAX
```

These constraints ensure that the same inverter power is not simultaneously committed to energy trading and reserve delivery beyond the rated power.

#### Charging/discharging exclusivity

```text
p_ch(t)  <= P_MAX * (1 - y(t))
p_dis(t) <= P_MAX * y(t)
y(t) in {0, 1}
```

#### SOC operating limits

```text
E_MAX * SOC_MIN <= SOC(t) <= E_MAX * SOC_MAX
```

The initial SOC and end-of-day SOC are constrained by:

```text
SOC(0) = SOC_INIT_MWh

E_MAX * SOC_END_MIN <= SOC(N_T) <= E_MAX * SOC_END_MAX
```

For the multi-day run, `SOC_INIT_MWh` for the next day is the previous day's optimized terminal SOC.

#### Reserve energy headroom

```text
SOC(t) >=
    E_MAX * SOC_MIN
  + TAU_FCR * r_fcr(b) / eta_dis
  + TAU_AFRR_UP * r_up(b) / eta_dis
```

```text
SOC(t) <=
    E_MAX * SOC_MAX
  - TAU_FCR * eta_ch * r_fcr(b)
  - TAU_AFRR_DN * eta_ch * r_dn(b)
```

These constraints reserve sufficient energy space for the required upward and downward response capability. The current configuration uses `TAU_FCR = 0.83 h` and `TAU_AFRR_UP = TAU_AFRR_DN = 1.0 h`.

#### Capacity variable domains

```text
0 <= r_fcr(b) <= P_MAX * FCR_MARKETABLE_RATIO
0 <= r_up(b)  <= P_MAX
0 <= r_dn(b)  <= P_MAX
```

The reserve-capacity variables are integer MW variables in the current implementation. `FCR_MARKETABLE_RATIO = 0.80` limits the marketable FCR capacity to 80% of the inverter rating.

### 6.6 Current model assumptions

#### Market and price assumptions

1. The optimization uses a full-day price profile with 96 15-minute energy intervals and six 4-hour ancillary-service blocks.
2. DA, IDM and aFRR energy prices are treated as EUR/MWh. aFRR capacity prices are treated as EUR/(MW·h). FCR prices are treated as EUR/MW per 4-hour product block.
3. Missing DA and aFRR energy prices are filled by within-day interpolation. Missing capacity prices are filled within the relevant 4-hour block and then carried across the day if necessary.
4. When IDM prices are missing, the current input preparation generates them from the imputed DA price using a normal distribution with:

```text
mean  = DA price
sigma = max(1.0, 5% * abs(DA price))
```

The current German input workbook has an empty IDM column, so the joint-strategy result is based on simulated IDM prices rather than observed IDM transaction prices.

#### Optimization and bidding assumptions

1. The model has deterministic perfect foresight of the input prices over each optimized day; forecast error and price uncertainty are not modeled.
2. FCR and aFRR capacity acceptance probabilities are set to 100% in the current configuration.
3. The model optimizes the accepted reserve capacity directly and does not simulate bid curves, marginal clearing, partial acceptance or strategic price bidding.
4. FCR, aFRR UP and aFRR DN can be co-optimized when the power and SOC constraints are satisfied. The model does not add additional commercial restrictions beyond the coded constraints.

#### Activation and technical assumptions

1. aFRR activation is represented by a fixed expected activation rate:

```text
alpha_up = alpha_dn = 2 / 24
```

This represents an expected average of approximately two activation hours per day in each direction. It is not a stochastic activation simulation.
2. The 7 MW / 16.7 MWh case uses `eta_ch = eta_dis = 0.93`, `SOC_MIN = 5%` and `SOC_MAX = 95%`.
3. The battery is assumed to satisfy the relevant prequalification and response requirements; detailed AGC tracking, four-second response behavior, performance scoring and non-delivery penalties are not explicitly simulated.
4. The model is solved separately for each strategy and each day. Each strategy maintains its own SOC carry-over chain from one day to the next.

#### Financial interpretation and limitations

1. The output is expected market operating revenue net of modeled degradation cost. It is not a full project cash-flow or equity-return model.
2. CAPEX, fixed OPEX, BSP or trader revenue sharing, network charges, taxes, financing costs, insurance and other transaction fees are excluded.
3. The degradation cost is a linear throughput charge and does not model cell-level aging, calendar aging, replacement timing or nonlinear degradation.
4. The annual result is a deterministic back-test using known historical input prices. It should be interpreted as a modeled operating benchmark or gross revenue estimate, not as a guaranteed investment return.
