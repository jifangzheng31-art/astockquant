"""技术指标计算。纯函数，无网络依赖，便于单元测试和回测复用。

输入统一为含 date/open/high/low/close/volume/amount 的日线 DataFrame。
所有指标只使用当日及之前的数据；相对强度需要显式传入基准指数日线，
避免在函数内部偷偷读取“未来”行情。
"""
from __future__ import annotations

import math
from typing import Any

import numpy as np
import pandas as pd

REQUIRED_COLUMNS = ("date", "open", "high", "low", "close", "volume")


def _require(df: pd.DataFrame) -> pd.DataFrame:
    missing = [c for c in REQUIRED_COLUMNS if c not in df.columns]
    if missing:
        raise ValueError(f"日线缺少字段: {missing}")
    out = df.copy()
    out["date"] = pd.to_datetime(out["date"], errors="coerce")
    out = out.dropna(subset=["date", "open", "high", "low", "close"]).sort_values("date")
    out = out.drop_duplicates("date", keep="last").reset_index(drop=True)
    if out.empty:
        raise ValueError("日线为空")
    return out


def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    values = 100 - 100 / (1 + rs)
    values = values.where(avg_loss != 0, 100.0)
    values = values.where((avg_gain != 0) | (avg_loss != 0), 50.0)
    return values


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    prev_close = df["close"].shift(1)
    tr = pd.concat(
        [
            df["high"] - df["low"],
            (df["high"] - prev_close).abs(),
            (df["low"] - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return tr.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()


def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    out = _require(df)
    close = out["close"]
    high = out["high"]
    low = out["low"]
    volume = out["volume"].astype(float).fillna(0)
    amount = (
        pd.to_numeric(out["amount"], errors="coerce").fillna(0)
        if "amount" in out.columns
        else pd.Series(np.nan, index=out.index)
    )

    for period in (5, 10, 20, 60):
        out[f"ma{period}"] = close.rolling(period, min_periods=period).mean()

    out["ema12"] = close.ewm(span=12, adjust=False, min_periods=12).mean()
    out["ema26"] = close.ewm(span=26, adjust=False, min_periods=26).mean()
    out["macd_dif"] = out["ema12"] - out["ema26"]
    out["macd_dea"] = out["macd_dif"].ewm(span=9, adjust=False, min_periods=9).mean()
    # A 股软件常用柱体 = 2 × (DIF - DEA)，此处保持同一口径。
    out["macd_hist"] = 2 * (out["macd_dif"] - out["macd_dea"])
    out["rsi14"] = rsi(close, 14)
    out["atr14"] = atr(out, 14)
    out["atr_pct"] = out["atr14"] / close * 100

    out["vol_ma5"] = volume.rolling(5, min_periods=3).mean()
    out["vol_ma20"] = volume.rolling(20, min_periods=10).mean()
    out["vol_ratio"] = volume / out["vol_ma5"].replace(0, np.nan)
    out["vol_ratio20"] = volume / out["vol_ma20"].replace(0, np.nan)
    out["amount_ma20"] = amount.rolling(20, min_periods=10).mean()

    for period in (5, 10, 20, 60):
        out[f"ret_{period}"] = close.pct_change(period) * 100
    out["ret_1"] = close.pct_change(1) * 100
    out["ma20_slope_5"] = (out["ma20"] / out["ma20"].shift(5) - 1) * 100

    prior_low20 = low.shift(1).rolling(20, min_periods=10).min()
    prior_high20 = high.shift(1).rolling(20, min_periods=10).max()
    prior_low60 = low.shift(1).rolling(60, min_periods=30).min()
    prior_high60 = high.shift(1).rolling(60, min_periods=30).max()
    out["support20"] = prior_low20
    out["resistance20"] = prior_high20
    out["support60"] = prior_low60
    out["resistance60"] = prior_high60
    out["breakout20"] = close > prior_high20
    out["breakout60"] = close > prior_high60
    out["distance_ma20_pct"] = (close / out["ma20"] - 1) * 100
    out["distance_ma5_pct"] = (close / out["ma5"] - 1) * 100

    rolling_high60 = close.rolling(60, min_periods=30).max()
    out["drawdown60_pct"] = (close / rolling_high60 - 1) * 100

    out["up_day"] = out["ret_1"] > 0
    out["volume_up"] = volume > out["vol_ma5"]
    out["limit_like"] = out["ret_1"] >= 9.7
    return out


def attach_relative_strength(stock: pd.DataFrame, benchmark: pd.DataFrame) -> pd.DataFrame:
    """把基准收益与超额收益并入个股表；按日期内连接，不做未来填充。"""
    if stock.empty:
        return stock.copy()
    out = stock.copy()
    if benchmark is None or benchmark.empty:
        for col in ("bench_ret_5", "bench_ret_10", "bench_ret_20", "excess_5", "excess_10", "excess_20", "rs_ratio"):
            out[col] = np.nan
        return out
    bench = benchmark[["date", "close"]].copy()
    bench["date"] = pd.to_datetime(bench["date"], errors="coerce").dt.normalize()
    bench = bench.dropna().sort_values("date").drop_duplicates("date")
    bench["bench_close"] = pd.to_numeric(bench["close"], errors="coerce")
    for period in (5, 10, 20):
        bench[f"bench_ret_{period}"] = bench["bench_close"].pct_change(period) * 100
    bench["rs_base"] = bench["bench_close"] / bench["bench_close"].shift(20)
    merged = out.merge(
        bench[["date", "bench_close", "bench_ret_5", "bench_ret_10", "bench_ret_20", "rs_base"]],
        on="date",
        how="left",
    )
    for period in (5, 10, 20):
        merged[f"excess_{period}"] = merged[f"ret_{period}"] - merged[f"bench_ret_{period}"]
    stock_base = merged["close"] / merged["close"].shift(20)
    merged["rs_ratio"] = stock_base / merged["rs_base"]
    return merged


def latest_value(df: pd.DataFrame, column: str, default: float = float("nan")) -> float:
    if column not in df.columns or df.empty:
        return default
    value = df[column].iloc[-1]
    try:
        f = float(value)
    except (TypeError, ValueError):
        return default
    return f if math.isfinite(f) else default


def technical_summary(df: pd.DataFrame) -> dict[str, Any]:
    """面向个股页的结构化摘要；缺失指标返回 None，不臆造。"""
    if len(df) < 20:
        raise ValueError("有效日线不足 20 条，无法生成可靠技术分析")
    last = df.iloc[-1]
    close = float(last["close"])
    ma5 = latest_value(df, "ma5")
    ma10 = latest_value(df, "ma10")
    ma20 = latest_value(df, "ma20")
    ma60 = latest_value(df, "ma60")
    dif = latest_value(df, "macd_dif")
    dea = latest_value(df, "macd_dea")
    hist = latest_value(df, "macd_hist")
    rsi14 = latest_value(df, "rsi14")
    atr_pct = latest_value(df, "atr_pct")
    support = latest_value(df, "support20")
    resistance = latest_value(df, "resistance20")
    drawdown = latest_value(df, "drawdown60_pct")

    trend_score = sum(
        [
            close > ma5 if math.isfinite(ma5) else False,
            ma5 > ma10 if math.isfinite(ma5) and math.isfinite(ma10) else False,
            ma10 > ma20 if math.isfinite(ma10) and math.isfinite(ma20) else False,
            close > ma20 if math.isfinite(ma20) else False,
            ma20 > ma60 if math.isfinite(ma20) and math.isfinite(ma60) else False,
        ]
    )
    trend_labels = {
        5: "多头排列",
        4: "偏强趋势",
        3: "震荡偏强",
        2: "震荡",
        1: "偏弱",
        0: "空头/弱势",
    }
    if math.isfinite(rsi14):
        if rsi14 >= 80:
            rsi_state = "严重超买"
        elif rsi14 >= 70:
            rsi_state = "超买"
        elif rsi14 <= 20:
            rsi_state = "严重超卖"
        elif rsi14 <= 30:
            rsi_state = "超卖"
        else:
            rsi_state = "中性"
    else:
        rsi_state = "数据不足"

    if math.isfinite(dif) and math.isfinite(dea):
        macd_state = "DIF 在 DEA 上方" if dif >= dea else "DIF 在 DEA 下方"
    else:
        macd_state = "数据不足"

    risk_notes: list[str] = []
    if math.isfinite(atr_pct):
        if atr_pct >= 6:
            risk_notes.append(f"ATR 波动率 {atr_pct:.2f}%，波动显著偏高")
        elif atr_pct >= 3.5:
            risk_notes.append(f"ATR 波动率 {atr_pct:.2f}%，波动偏高")
    if math.isfinite(rsi14) and rsi14 >= 75:
        risk_notes.append("RSI 处于超买区，追高回撤风险上升")
    if math.isfinite(drawdown) and drawdown <= -25:
        risk_notes.append(f"距 60 日高点回撤 {abs(drawdown):.1f}%，弱势尚未修复")
    if math.isfinite(ma20) and close < ma20:
        risk_notes.append("收盘价位于 MA20 下方，中期趋势偏弱")
    if not risk_notes:
        risk_notes.append("未触发主要技术风险阈值，但仍需结合大盘与个股基本面")

    return {
        "date": pd.Timestamp(last["date"]).date().isoformat(),
        "close": close,
        "change_pct": latest_value(df, "pct_chg"),
        "ma5": ma5,
        "ma10": ma10,
        "ma20": ma20,
        "ma60": ma60,
        "trend": trend_labels[trend_score],
        "trend_score": trend_score,
        "macd_dif": dif,
        "macd_dea": dea,
        "macd_hist": hist,
        "macd_state": macd_state,
        "rsi14": rsi14,
        "rsi_state": rsi_state,
        "atr_pct": atr_pct,
        "support20": support,
        "resistance20": resistance,
        "distance_ma20_pct": latest_value(df, "distance_ma20_pct"),
        "drawdown60_pct": drawdown,
        "vol_ratio": latest_value(df, "vol_ratio"),
        "risk_notes": risk_notes,
        "disclaimer": "以上为基于历史行情的概率性技术参考，不构成收益承诺或买卖建议。",
    }
