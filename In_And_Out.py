from __future__ import annotations

from datetime import date, datetime, time
from pathlib import Path

import numpy as np
import pandas as pd
from openpyxl import load_workbook
from openpyxl.styles import Font, PatternFill

from BESS_MKT_Config import BESS_MKT_Config, STRATEGIES

# Input 表：DA / aFRR 能量 / ID 列为 EUR/MWh；FCR、aFRR 容量价见列标题，与 Optimizer 一致。


def _normalize_colon(x):
    """统一字符串中的全角符号，便于日期/文本清洗。"""
    if pd.isna(x):
        return x
    return str(x).replace("：", ":").replace("／", "/").strip()


def _parse_input_date(raw):
    """解析 Input A 列日期为 date；兼容 Excel 序列、Timestamp、字符串。"""
    if raw is None or (isinstance(raw, float) and np.isnan(raw)):
        return None
    if pd.isna(raw):
        return None
    if isinstance(raw, date) and not isinstance(raw, datetime):
        return raw
    ts = pd.to_datetime(raw, errors="coerce")
    if pd.isna(ts):
        ts = pd.to_datetime(_normalize_colon(str(raw)), errors="coerce")
    if pd.isna(ts):
        return None
    return ts.date()


def _parse_hour_span(x):
    """解析「小时」列（如 Input 的 C 列）为 0..23；兼容整数、Excel 时间(time)、时间字符串 HH:MM。"""
    if x is None:
        return None
    if isinstance(x, float) and np.isnan(x):
        return None
    if pd.isna(x):
        return None
    if isinstance(x, time):
        h = int(x.hour)
        return h if 0 <= h <= 23 else None
    if isinstance(x, (pd.Timestamp, datetime)):
        h = int(x.hour)
        return h if 0 <= h <= 23 else None
    s = str(x).strip()
    if not s or s.lower() == "nan":
        return None
    if ":" in s or "：" in s:
        s2 = _normalize_colon(s)
        parts = s2.split(":")
        try:
            h = int(float(parts[0]))
            return h if 0 <= h <= 23 else None
        except (TypeError, ValueError):
            return None
    h = pd.to_numeric(s, errors="coerce")
    if pd.isna(h):
        return None
    h = int(h)
    return h if 0 <= h <= 23 else None


def _parse_time_to_slot(raw):
    """
    解析 B 列时间为日内 15 分钟段索引 0..95。
    支持 Excel time/datetime、dateutil 可解析字符串、HH:MM、以及 0–1 的 Excel 日内小数。
    """
    if raw is None or (isinstance(raw, float) and np.isnan(raw)):
        return None
    if pd.isna(raw):
        return None
    if isinstance(raw, time):
        total = raw.hour * 60 + raw.minute
    elif isinstance(raw, (pd.Timestamp, datetime)):
        total = raw.hour * 60 + raw.minute
    elif isinstance(raw, (int, float)) and not isinstance(raw, bool) and 0 <= float(raw) < 1.0:
        total = int(round(float(raw) * 24 * 60))
        total = max(0, min(total, 24 * 60 - 1))
    else:
        s = _normalize_colon(str(raw).strip())
        if not s or s.lower() == "nan":
            return None
        if ":" not in s:
            return None
        parts = s.split(":")
        try:
            h = int(float(parts[0]))
            m = int(float(parts[1])) if len(parts) > 1 else 0
        except (TypeError, ValueError):
            return None
        if h < 0 or h > 23 or m < 0 or m > 59:
            return None
        total = h * 60 + m
    slot = int(round(total / 15.0))
    return slot if 0 <= slot <= 95 else None


def _expand_hourly_slots_if_needed(df: pd.DataFrame) -> pd.DataFrame:
    """若某日仅有整点 24 行（slot 为 0,4,…,92），则复制为 96 个 15 分钟点。"""
    if df.empty:
        return df
    pieces: list[pd.DataFrame] = []
    hourly_slots = set(range(0, 96, 4))
    for _, g in df.groupby("date"):
        g = g.sort_values("slot").reset_index(drop=True)
        slots = set(int(x) for x in g["slot"].tolist())
        if len(g) == 24 and slots == hourly_slots:
            rows = []
            for _, row in g.iterrows():
                base = int(row["slot"]) // 4
                for q in range(4):
                    r = row.copy()
                    r["slot"] = base * 4 + q
                    rows.append(r)
            pieces.append(pd.DataFrame(rows))
        else:
            pieces.append(g)
    return pd.concat(pieces, ignore_index=True).sort_values(["date", "slot"]).reset_index(drop=True)


def _ensure_96_slots_per_day(df: pd.DataFrame, value_cols: list[str]) -> pd.DataFrame:
    """每个自然日补齐 0..95 共 96 个 15 分钟点，不做填充值处理。"""
    if df.empty:
        return df
    dates = sorted(df["date"].dropna().unique())
    full_idx = pd.MultiIndex.from_product([dates, range(96)], names=["date", "slot"])
    out = (
        df.set_index(["date", "slot"])
        .reindex(full_idx)
        .reset_index()
        .sort_values(["date", "slot"])
    )
    for col in value_cols:
        if col not in out.columns:
            out[col] = np.nan
    return out


def read_file(
    file_path: str,
    config: BESS_MKT_Config | None = None,
    *,
    write_back_sheet: bool = True,
    random_seed: int = 42,
) -> dict:
    """
    从 Excel 的 Input sheet 读取多日 15 分钟级输入，智能补齐缺失数据，
    并转换为多日模型输入字典。

    Input 列定义:
      A 日期(YYYY/M/D)，B 时间(HH:MM)，C 对应小时(0–23)，
      D DA(EUR/MWh)，E FCR 容量(EUR/MW)，F aFRR UP 容量(EUR/MW/h)，G aFRR DN 容量(EUR/MW/h)，
      H aFRR UP 能量(EUR/MWh)，I aFRR DN 能量(EUR/MWh)，
      J ID(EUR/MWh)；J 为空时用补齐后的 DA 为期望的正态分布抽样生成。

    返回值:
      {
          "days": dict[date, day_dict],        按日期排序的多日输入
          "available_dates": list[date],        可用日期列表（已排序）
          "hourly_input_filled": DataFrame,     完整多日补齐后数据
      }
      其中每个 day_dict 的格式与 Optimizer.build_and_solve 的输入一致。
    """
    if config is None:
        config = BESS_MKT_Config()

    xls = pd.ExcelFile(file_path)
    if "Input" not in xls.sheet_names:
        raise ValueError("Excel 缺少必要 sheet: ['Input']")

    df = pd.read_excel(xls, sheet_name="Input")
    if df.shape[1] < 10:
        raise ValueError("Input sheet 至少需要 A–J 共 10 列数据。")

    rename_map = {
        df.columns[0]: "date_raw",
        df.columns[1]: "time_raw",
        df.columns[2]: "hour_from_sheet",
        df.columns[3]: "price_da",
        df.columns[4]: "price_fcr_cap_hourly",
        df.columns[5]: "price_afrr_cap_up_hourly",
        df.columns[6]: "price_afrr_cap_dn_hourly",
        df.columns[7]: "price_afrr_eng_up_hourly",
        df.columns[8]: "price_afrr_eng_dn_hourly",
        df.columns[9]: "price_intra_day_input",
    }
    df = df.rename(columns=rename_map)

    df["date"] = df["date_raw"].map(_parse_input_date)
    df["slot"] = df["time_raw"].map(_parse_time_to_slot)
    parse_debug = df[["date_raw", "time_raw", "date", "slot"]].head(8).copy()

    value_cols = [
        "price_da",
        "price_fcr_cap_hourly",
        "price_afrr_cap_up_hourly",
        "price_afrr_cap_dn_hourly",
        "price_afrr_eng_up_hourly",
        "price_afrr_eng_dn_hourly",
    ]
    for col in value_cols:
        df[col] = pd.to_numeric(df[col], errors="coerce").round(4)

    df["price_intra_day_input"] = pd.to_numeric(
        df["price_intra_day_input"], errors="coerce"
    ).round(4)
    df["price_intra_day"] = df["price_intra_day_input"].copy()

    df = df.dropna(subset=["date", "slot"]).copy()
    df["slot"] = df["slot"].astype(int)

    hc = df["hour_from_sheet"].map(_parse_hour_span)
    ok_h = hc.notna() & (hc == df["slot"] // 4)
    bad_h = hc.notna() & ~ok_h
    if bad_h.any():
        print(f">>> warning: {int(bad_h.sum())} 行 C 列小时与 B 列时间不一致（以 B 列为准）")

    df = (
        df.groupby(["date", "slot"], as_index=False)[value_cols + ["price_intra_day"]]
        .mean()
        .sort_values(["date", "slot"])
        .reset_index(drop=True)
    )

    df = _expand_hourly_slots_if_needed(df)
    qh = _ensure_96_slots_per_day(df, value_cols + ["price_intra_day"])

    if qh.empty:
        raise ValueError(
            "Input sheet 未读取到可用的 15 分钟数据（解析后 date/slot 全为空或聚合结果为空）。"
            "请确认 A 为日期、B 为时间(HH:MM)。解析预览：\n"
            f"{parse_debug.to_string()}"
        )

    # ── ⑤ 丢弃完全缺失日 ──
    all_dates = sorted(qh["date"].dropna().unique())
    drop_dates = []
    for dt in all_dates:
        if qh.loc[qh["date"] == dt, value_cols].isna().all().all():
            drop_dates.append(dt)
            print(f">>> warning: {dt} 所有价格列全部为 NaN，已跳过该日")
    if drop_dates:
        qh = qh[~qh["date"].isin(drop_dates)].reset_index(drop=True)
        all_dates = [d for d in all_dates if d not in drop_dates]
    if not all_dates:
        raise ValueError("所有日期的价格数据均为空，无可用数据。")

    # ── ⑥ 记录补齐前缺失快照 ──
    all_impute_cols = value_cols + ["price_intra_day"]
    missing_before = qh[all_impute_cols].isna().copy()

    print("\n>>> missing values by date / 15min slot (before imputation)")
    has_missing = False
    for d_ in all_dates:
        day_df = qh[qh["date"] == d_]
        for col in value_cols:
            n_miss = int(day_df[col].isna().sum())
            if n_miss:
                has_missing = True
                print(f"  {d_} | {col}: missing {n_miss} slots")
    if not has_missing:
        print("  none")

    # ── ⑥ 智能补齐（逐日独立，不跨日插值） ──
    energy_price_cols = [
        "price_da", "price_afrr_eng_up_hourly", "price_afrr_eng_dn_hourly",
    ]
    capacity_price_cols = [
        "price_fcr_cap_hourly", "price_afrr_cap_up_hourly", "price_afrr_cap_dn_hourly",
    ]
    for dt in all_dates:
        day_idx = qh.index[qh["date"] == dt]
        day_df = qh.loc[day_idx]
        for col in energy_price_cols:
            qh.loc[day_idx, col] = (
                day_df[col].interpolate(method="linear").bfill().ffill()
            )
        for col in capacity_price_cols:
            block_id = day_df["slot"] // 16
            filled = day_df.groupby(block_id)[col].transform(
                lambda s: s.ffill().bfill()
            )
            qh.loc[day_idx, col] = filled.ffill().bfill()

    # ── ⑦ ID 价格：缺失处用补齐后的 DA 正态抽样 ──
    rng = np.random.default_rng(random_seed)
    id_missing = qh["price_intra_day"].isna()
    if id_missing.any():
        da_arr = qh.loc[id_missing, "price_da"].to_numpy(dtype=float)
        sigma = np.where(
            np.isfinite(da_arr),
            np.maximum(1.0, 0.05 * np.abs(da_arr)),
            1.0,
        )
        loc = np.where(np.isfinite(da_arr), da_arr, 0.0)
        sim = rng.normal(loc=loc, scale=sigma)
        qh.loc[id_missing, "price_intra_day"] = np.round(sim, 4)

    # ── ⑧ 兜底 ──
    still_missing = qh[all_impute_cols].isna().copy()
    for col in all_impute_cols:
        qh[col] = qh[col].fillna(0.0).round(4)

    # ── ⑨ 收集补齐报告 ──
    def _fmt_slot(s: int) -> str:
        s = int(s)
        return f"{s // 4:02d}:{(s % 4) * 15:02d}"

    method_map: dict[str, str] = {}
    for _c in energy_price_cols:
        method_map[_c] = "linear_interp"
    for _c in capacity_price_cols:
        method_map[_c] = "block_ffill"
    method_map["price_intra_day"] = "normal_from_da"

    summary_rows: list[dict] = []
    detail_rows: list[dict] = []
    for dt in all_dates:
        day_mask = qh["date"] == dt
        for col in all_impute_cols:
            n_missing = int(missing_before.loc[day_mask, col].sum())
            rate = n_missing / 96.0 * 100
            method = method_map.get(col, "—") if n_missing > 0 else "—"
            summary_rows.append({
                "Date": dt, "Column": col, "Total Slots": 96,
                "Missing Count": n_missing,
                "Missing Rate": f"{rate:.1f}%", "Method": method,
            })
            if n_missing > 0:
                was_na = missing_before.loc[day_mask, col]
                for idx in qh.index[day_mask][was_na]:
                    slot_val = int(qh.loc[idx, "slot"])
                    if still_missing.loc[idx, col]:
                        m = "fallback_zero"
                    else:
                        m = method_map.get(col, "unknown")
                    detail_rows.append({
                        "Date": dt, "Slot": slot_val,
                        "Time": _fmt_slot(slot_val), "Column": col,
                        "Imputed Value": qh.loc[idx, col], "Method": m,
                    })

    # ── ⑩ 写回 Excel ──
    if write_back_sheet:
        export_base = qh.copy()
        export_base["Time"] = export_base["slot"].map(_fmt_slot)
        export_base["Hour"] = export_base["slot"] // 4
        export_cols = [
            ("Date", "date"),
            ("Time", "Time"),
            ("Hour", "Hour"),
            ("DA (EUR/MWh)", "price_da"),
            ("FCR (EUR/MW)", "price_fcr_cap_hourly"),
            ("AFRR_UP_CAP (EUR/MW/h)", "price_afrr_cap_up_hourly"),
            ("AFRR_DN_CAP (EUR/MW/h)", "price_afrr_cap_dn_hourly"),
            ("AFRR_UP_ENG (EUR/MWh)", "price_afrr_eng_up_hourly"),
            ("AFRR_DN_ENG (EUR/MWh)", "price_afrr_eng_dn_hourly"),
            ("ID (EUR/MWh)", "price_intra_day"),
        ]
        export_df = export_base[[src for _, src in export_cols]].copy()
        export_df.columns = [dst for dst, _ in export_cols]

        wb = load_workbook(file_path)

        # Input_filled sheet
        base_sheet_name = "Input_filled"
        sheet_name = base_sheet_name
        suffix = 1
        while sheet_name in wb.sheetnames:
            sheet_name = f"{base_sheet_name}_{suffix}"
            suffix += 1
        ws = wb.create_sheet(title=sheet_name)

        for c_idx, col_name in enumerate(export_df.columns, start=1):
            ws.cell(row=1, column=c_idx, value=col_name)

        for r_idx, row in enumerate(export_df.itertuples(index=False), start=2):
            for c_idx, val in enumerate(row, start=1):
                ws.cell(row=r_idx, column=c_idx, value=val)

        highlight_font = Font(bold=True)
        highlight_fill = PatternFill(fill_type="solid", fgColor="FFF59D")
        src_col_to_excel_col = {src: i + 1 for i, (_, src) in enumerate(export_cols)}

        for src_col in all_impute_cols:
            excel_col = src_col_to_excel_col.get(src_col)
            if excel_col is None:
                continue
            for r_idx, is_missing in enumerate(
                missing_before[src_col].tolist(), start=2
            ):
                if bool(is_missing):
                    cell = ws.cell(row=r_idx, column=excel_col)
                    cell.font = highlight_font
                    cell.fill = highlight_fill

        print(f">>> filled data written to sheet: {sheet_name}")

        # Imputation_Report sheet
        rpt_base = "Imputation_Report"
        rpt_name = rpt_base
        rpt_suffix = 1
        while rpt_name in wb.sheetnames:
            rpt_name = f"{rpt_base}_{rpt_suffix}"
            rpt_suffix += 1
        ws_rpt = wb.create_sheet(title=rpt_name)

        sum_headers = [
            "Date", "Column", "Total Slots",
            "Missing Count", "Missing Rate", "Method",
        ]
        for c_idx, h in enumerate(sum_headers, start=1):
            ws_rpt.cell(row=1, column=c_idx, value=h).font = Font(bold=True)
        for r_idx, rec in enumerate(summary_rows, start=2):
            for c_idx, h in enumerate(sum_headers, start=1):
                ws_rpt.cell(row=r_idx, column=c_idx, value=rec[h])

        detail_start = len(summary_rows) + 4
        det_headers = [
            "Date", "Slot", "Time", "Column", "Imputed Value", "Method",
        ]
        for c_idx, h in enumerate(det_headers, start=1):
            ws_rpt.cell(
                row=detail_start, column=c_idx, value=h
            ).font = Font(bold=True)
        for r_idx, rec in enumerate(detail_rows, start=detail_start + 1):
            for c_idx, h in enumerate(det_headers, start=1):
                ws_rpt.cell(row=r_idx, column=c_idx, value=rec[h])

        print(f">>> imputation report written to sheet: {rpt_name}")

        wb.save(file_path)

    # ── ⑪ 构建多日返回值 ──
    if config.N_T % config.N_B != 0:
        raise ValueError("config.N_T 须能被 N_B 整除。")
    block_size = config.BLOCK_SIZE

    days: dict = {}
    for sel_date in all_dates:
        day_q = (
            qh[qh["date"] == sel_date]
            .sort_values("slot")
            .reset_index(drop=True)
        )
        if len(day_q) != config.N_T:
            print(
                f">>> warning: {sel_date} 数据行数 {len(day_q)} "
                f"!= N_T={config.N_T}，已跳过"
            )
            continue

        price_da = day_q["price_da"].to_numpy(dtype=float)
        price_intra_day = day_q["price_intra_day"].to_numpy(dtype=float)
        price_afrr_eng_up = day_q["price_afrr_eng_up_hourly"].to_numpy(dtype=float)
        price_afrr_eng_dn = day_q["price_afrr_eng_dn_hourly"].to_numpy(dtype=float)

        price_fcr_cap = (
            day_q["price_fcr_cap_hourly"]
            .to_numpy(dtype=float)
            .reshape(config.N_B, block_size)
            .mean(axis=1)
        )
        price_afrr_cap_up = (
            day_q["price_afrr_cap_up_hourly"]
            .to_numpy(dtype=float)
            .reshape(config.N_B, block_size)
            .mean(axis=1)
        )
        price_afrr_cap_dn = (
            day_q["price_afrr_cap_dn_hourly"]
            .to_numpy(dtype=float)
            .reshape(config.N_B, block_size)
            .mean(axis=1)
        )

        days[sel_date] = {
            "date": np.full(config.N_T, sel_date, dtype=object),
            "hour": np.arange(config.N_T, dtype=int) // 4,
            "price_da": price_da,
            "price_intra_day": price_intra_day,
            "price_fcr_cap": price_fcr_cap,
            "prob_fcr": np.full(config.N_B, config.PROB_FCR, dtype=float),
            "price_afrr_cap_up": price_afrr_cap_up,
            "price_afrr_cap_dn": price_afrr_cap_dn,
            "prob_afrr_up": np.full(config.N_B, config.PROB_AFRR_UP, dtype=float),
            "prob_afrr_dn": np.full(config.N_B, config.PROB_AFRR_DN, dtype=float),
            "price_afrr_eng_up": price_afrr_eng_up,
            "price_afrr_eng_dn": price_afrr_eng_dn,
            "alpha_up": np.full(config.N_T, config.ALPHA_UP, dtype=float),
            "alpha_dn": np.full(config.N_T, config.ALPHA_DN, dtype=float),
            "selected_date": sel_date,
        }

    available_dates = sorted(days.keys())
    if not available_dates:
        raise ValueError("无可用日期数据（所有日期均不满足 N_T 行要求）。")

    return {
        "days": days,
        "available_dates": available_dates,
        "hourly_input_filled": qh,
    }


def _format_date_ymd(d) -> str:
    if d is None:
        return ""
    if hasattr(d, "year"):
        return f"{d.year}/{d.month}/{d.day}"
    s = str(d)
    if len(s) >= 10 and s[4] == "-":
        y, rest = s[:4], s[5:10].replace("-", "/")
        return f"{y}/{rest}" if "/" in rest else s
    return s


def _build_day_rows(
    opt_result: dict,
    inputs: dict,
    config: BESS_MKT_Config,
    strategy_name: str,
) -> list[dict]:
    """为单日单策略构建 96 行明细数据（内部函数）。

    根据策略的 ``energy_market`` 字段选择能量价格基准：
    ``"idm"`` → ``price_intra_day``，``"da"`` → ``price_da``，
    ``"none"`` → 零向量（不含能量套利，与 Optimizer 目标函数一致）。
    """
    c = config
    n_t = int(c.N_T)
    step = c.BLOCK_SIZE
    dt = float(c.DT)

    profile = STRATEGIES[strategy_name]
    if profile.energy_market == "idm":
        price_energy = np.asarray(inputs["price_intra_day"], dtype=float).ravel()
    elif profile.energy_market == "da":
        price_energy = np.asarray(inputs["price_da"], dtype=float).ravel()
    else:
        price_energy = np.zeros(n_t, dtype=float)

    p_ch = np.asarray(opt_result["p_ch"], dtype=float).ravel()
    p_dis = np.asarray(opt_result["p_dis"], dtype=float).ravel()
    soc = np.asarray(opt_result["soc"], dtype=float).ravel()
    r_fcr = np.asarray(opt_result["r_fcr"], dtype=float).ravel()
    r_aup = np.asarray(opt_result["r_aup"], dtype=float).ravel()
    r_adn = np.asarray(opt_result["r_adn"], dtype=float).ravel()

    pr_fcr = np.asarray(inputs["prob_fcr"], dtype=float).ravel()
    pr_aup = np.asarray(inputs["prob_afrr_up"], dtype=float).ravel()
    pr_adn = np.asarray(inputs["prob_afrr_dn"], dtype=float).ravel()
    p_up = np.asarray(inputs["price_afrr_eng_up"], dtype=float).ravel()
    p_dn = np.asarray(inputs["price_afrr_eng_dn"], dtype=float).ravel()
    p_fcr_cap = np.asarray(inputs["price_fcr_cap"], dtype=float).ravel()
    p_aup_cap = np.asarray(inputs["price_afrr_cap_up"], dtype=float).ravel()
    p_adn_cap = np.asarray(inputs["price_afrr_cap_dn"], dtype=float).ravel()
    alpha_up = np.asarray(inputs["alpha_up"], dtype=float).ravel()
    alpha_dn = np.asarray(inputs["alpha_dn"], dtype=float).ravel()

    date_str = _format_date_ymd(inputs.get("selected_date"))

    rows: list[dict] = []
    for t in range(n_t):
        b = c.block_of(t)
        h = t // 4
        minute = (t % 4) * 15

        rf = float(r_fcr[b])
        ru = float(r_aup[b])
        rd = float(r_adn[b])
        pf = float(pr_fcr[b])
        pu = float(pr_aup[b])
        pd_ = float(pr_adn[b])

        id_ch_mwh = float(p_ch[t] * dt)
        id_dis_mwh = float(p_dis[t] * dt)
        e_dn = float(alpha_dn[t] * rd * dt)
        e_up = float(alpha_up[t] * ru * dt)

        rev_fcr = float(p_fcr_cap[b] * pf * rf) / float(step)
        rev_aup_cap = float(p_aup_cap[b] * pu * ru * 4.0) / step
        rev_adn_cap = float(p_adn_cap[b] * pd_ * rd * 4.0) / step

        energy_ch_cost = float(price_energy[t] * p_ch[t] * dt)
        energy_dis_inc = float(price_energy[t] * p_dis[t] * dt)
        rev_up_eng = float(p_up[t] * pu * alpha_up[t] * ru * dt)
        rev_dn_eng_raw = float(-p_dn[t] * pd_ * alpha_dn[t] * rd * dt)
        afrr_dn_cost = float(-rev_dn_eng_raw) if abs(rev_dn_eng_raw) > 1e-12 else 0.0

        deg = float(
            -c.C_DEG
            * (p_ch[t] + p_dis[t] + alpha_up[t] * ru + alpha_dn[t] * rd)
            * dt
        )
        deg_cost = float(-deg)
        total_row = (
            rev_fcr + rev_aup_cap + rev_adn_cap
            + (energy_dis_inc - energy_ch_cost)
            + rev_up_eng + rev_dn_eng_raw + deg
        )

        rows.append({
            "日期": date_str,
            "小时": h,
            "分钟": minute,
            "FCR申报容量": rf,
            "AFRR UP申报容量": ru,
            "AFRR DN申报容量": rd,
            "FCR实际容量": rf * pf,
            "AFRR UP实际容量": ru * pu,
            "AFRR DN容量": rd * pd_,
            "放电容量": float(p_dis[t]),
            "充电容量": float(p_ch[t]),
            "SOC_init": float(soc[t]),
            "Energy Charge (MWh)": id_ch_mwh,
            "Energy Discharge (MWh)": id_dis_mwh,
            "AFRR DN Energy": e_dn,
            "AFRR UP Energy": e_up,
            "SOC_End": float(soc[t + 1]),
            "FCR Revenue": rev_fcr,
            "AFRR UP Revenue": rev_aup_cap,
            "AFRR DN Revenue": rev_adn_cap,
            "Energy Charge Cost": energy_ch_cost,
            "Energy Discharge Income": energy_dis_inc,
            "AFRR DN Energy Cost": afrr_dn_cost,
            "AFRR UP Energy Income": rev_up_eng,
            "退化成本": deg_cost,
            "Total Revenue": total_row,
        })
    return rows


_DETAIL_COLUMNS = [
    "日期", "小时", "分钟",
    "FCR申报容量", "AFRR UP申报容量", "AFRR DN申报容量",
    "FCR实际容量", "AFRR UP实际容量", "AFRR DN容量",
    "放电容量", "充电容量", "SOC_init",
    "Energy Charge (MWh)", "Energy Discharge (MWh)",
    "AFRR DN Energy", "AFRR UP Energy", "SOC_End",
    "FCR Revenue", "AFRR UP Revenue", "AFRR DN Revenue",
    "Energy Charge Cost", "Energy Discharge Income",
    "AFRR DN Energy Cost", "AFRR UP Energy Income",
    "退化成本", "Total Revenue",
]


def print_result(
    bundle: dict,
    config: BESS_MKT_Config | None = None,
) -> str:
    """
    将多日 × 多策略优化结果导出为新建 Excel。

    ``bundle`` 格式::

        {
            "file_path": str,
            "results": {
                strategy_name: {
                    date: {"opt_result": dict, "inputs": dict},
                    ...
                },
                ...
            },
        }

    ``results`` 中有几种策略就导出几个详情 sheet（``result_<策略名>``），
    另附一个 ``summary`` sheet 汇总每日每策略的收入分项。

    输出文件名：单策略时 ``Result_<策略名>_时间戳.xlsx``，
    多策略时 ``Result_compare_时间戳.xlsx``。

    :return: 新建 Excel 的绝对路径字符串。
    """
    if config is None:
        config = BESS_MKT_Config()

    file_path = bundle["file_path"]
    results = bundle["results"]

    strategy_names = list(results.keys())
    all_strategy_dfs: dict[str, pd.DataFrame] = {}
    summary_rows: list[dict] = []

    for sname in strategy_names:
        day_frames: list[pd.DataFrame] = []
        for dt in sorted(results[sname]):
            entry = results[sname][dt]
            rows = _build_day_rows(
                entry["opt_result"], entry["inputs"], config, sname,
            )
            df_day = pd.DataFrame(rows, columns=_DETAIL_COLUMNS)
            day_frames.append(df_day)

            summary_rows.append({
                "Date": _format_date_ymd(dt),
                "Strategy": sname,
                "FCR Revenue": df_day["FCR Revenue"].sum(),
                "AFRR UP Revenue": df_day["AFRR UP Revenue"].sum(),
                "AFRR DN Revenue": df_day["AFRR DN Revenue"].sum(),
                "Energy Charge Cost": df_day["Energy Charge Cost"].sum(),
                "Energy Discharge Income": df_day["Energy Discharge Income"].sum(),
                "AFRR DN Energy Cost": df_day["AFRR DN Energy Cost"].sum(),
                "AFRR UP Energy Income": df_day["AFRR UP Energy Income"].sum(),
                "退化成本": df_day["退化成本"].sum(),
                "Total Revenue": df_day["Total Revenue"].sum(),
            })

        all_strategy_dfs[sname] = pd.concat(day_frames, ignore_index=True)

    summary_df = pd.DataFrame(summary_rows)

    out_dir = Path(file_path).resolve().parent
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    if len(strategy_names) == 1:
        out_name = f"Result_{strategy_names[0]}_{stamp}.xlsx"
    else:
        out_name = f"Result_compare_{stamp}.xlsx"
    out_path = out_dir / out_name

    with pd.ExcelWriter(out_path, engine="openpyxl") as writer:
        for sname in strategy_names:
            sheet = f"result_{sname}"
            all_strategy_dfs[sname].to_excel(writer, sheet_name=sheet, index=False)
        summary_df.to_excel(writer, sheet_name="summary", index=False)

    print(f">>> result exported: {out_path}")
    return str(out_path.resolve())
