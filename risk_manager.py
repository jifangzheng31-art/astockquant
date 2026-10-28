"""风险管理：观察区间、止损参考、目标参考与风险收益比。

这是“规则计算器”，不是收益预测。每个价格都能追溯到信号日收盘价、
ATR14、20 日支撑/压力或均线，并随结果一起输出计算依据和数据日期。
"""
from __future__ import annotations

import math
from datetime import date
from typing import Any

import pandas as pd

from config import Settings, get_settings


def _finite(value: Any) -> float | None:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _round_price(value: float | None) -> float | None:
    return round(value + 1e-9, 2) if value is not None and math.isfinite(value) else None


def build_risk_plan(
    row: pd.Series | dict[str, Any],
    *,
    data_date: str | date,
    mode: str = "postmarket",
    score: float | None = None,
    settings: Settings | None = None,
) -> dict[str, Any]:
    settings = settings or get_settings()
    close = _finite(row.get("close"))
    atr = _finite(row.get("atr14"))
    atr_pct = _finite(row.get("atr_pct"))
    support = _finite(row.get("support20"))
    resistance = _finite(row.get("resistance20"))
    ma5 = _finite(row.get("ma5"))
    ma10 = _finite(row.get("ma10"))
    ma20 = _finite(row.get("ma20"))
    rsi14 = _finite(row.get("rsi14"))

    if close is None or close <= 0:
        raise ValueError("缺少有效收盘价，无法生成风险参考")
    if atr is None or atr <= 0:
        raise ValueError("缺少有效 ATR14，无法生成风险参考")
    if support is None or support <= 0:
        support = min(close - 1.5 * atr, close * 0.95)
    if resistance is None or resistance <= 0:
        resistance = close + 1.5 * atr

    # 观察区间：以“回踩 1 ATR 至现价上方 0.5 ATR”为基础，并用 20 日
    # 支撑/压力约束。区间只用于观察，不代表一定会成交。
    zone_low = max(support * 1.002, close - 1.0 * atr)
    zone_high = min(close + 0.5 * atr, max(resistance * 0.998, close + 0.15 * atr))
    zone_low = min(zone_low, close - 0.01)
    zone_high = max(zone_high, zone_low + 0.02)
    zone_low = max(0.01, zone_low)

    # 止损：支撑下方 1% 与“收盘价 - 1.5 ATR”取更低者，再用最大 8%
    # 距离封顶，避免极端个股出现不可承受的理论止损。
    raw_stop = min(support * 0.99, close - 1.5 * atr)
    max_loss_stop = close * (1 - 0.08)
    stop = max(raw_stop, max_loss_stop)
    stop = min(stop, close - 0.01)
    stop = max(0.01, stop)

    # 目标参考：优先使用 20 日压力；压力太近/已突破时用 2 ATR，
    # 压力过远（>3 ATR）时封顶，避免短线风险收益比被远端压力位夸大。
    if resistance > close * 1.01:
        capped = min(resistance, close + 3.0 * atr)
        target = max(capped, close + 1.0 * atr)
        target_rule = "20 日压力位（不含当日；超过 3×ATR 时按 3×ATR 封顶）"
    else:
        target = close + 2.0 * atr
        target_rule = "收盘价 + 2×ATR14（20 日压力已被突破或距离过近）"

    risk_per_share = max(0.0, close - stop)
    reward_per_share = max(0.0, target - close)
    rr = reward_per_share / risk_per_share if risk_per_share > 0 else None

    risk_score = 0
    risk_notes: list[str] = []
    if atr_pct is not None:
        if atr_pct >= 6:
            risk_score += 3
            risk_notes.append("波动率很高")
        elif atr_pct >= 4:
            risk_score += 2
            risk_notes.append("波动率偏高")
        elif atr_pct >= 2.5:
            risk_score += 1
    if rsi14 is not None and rsi14 >= 75:
        risk_score += 1
        risk_notes.append("RSI 超买")
    if ma20 is not None and close < ma20:
        risk_score += 1
        risk_notes.append("股价位于 MA20 下方")
    if rr is not None and rr < 1.2:
        risk_score += 1
        risk_notes.append("风险收益比偏低")
    level = "高" if risk_score >= 4 else "中" if risk_score >= 2 else "低"

    basis = [
        f"信号日收盘价 {close:.2f}",
        f"ATR14 {atr:.2f}（占收盘价 {atr_pct:.2f}%）" if atr_pct is not None else f"ATR14 {atr:.2f}",
        f"20 日支撑 {support:.2f} / 压力 {resistance:.2f}（均不含信号日）",
        f"MA5 {ma5:.2f} / MA10 {ma10:.2f} / MA20 {ma20:.2f}" if all(
            v is not None for v in (ma5, ma10, ma20)
        ) else "均线数据部分缺失",
        "观察区间 = max(支撑×1.002, 收盘−1ATR) 至 min(收盘+0.5ATR, 压力)",
        "止损参考 = max(min(支撑×0.99, 收盘−1.5ATR), 收盘×0.92)",
        f"目标参考 = {target_rule}",
    ]

    return {
        "data_date": data_date.isoformat() if isinstance(data_date, date) else str(data_date),
        "mode": mode,
        "close": _round_price(close),
        "entry_zone_low": _round_price(zone_low),
        "entry_zone_high": _round_price(zone_high),
        "observation_zone": [_round_price(zone_low), _round_price(zone_high)],
        "stop_loss": _round_price(stop),
        "stop_distance_pct": round((close - stop) / close * 100, 2),
        "target": _round_price(target),
        "target_rule": target_rule,
        "support20": _round_price(support),
        "resistance20": _round_price(resistance),
        "risk_reward_ratio": round(rr, 2) if rr is not None else None,
        "atr14": _round_price(atr),
        "atr_pct": round(atr_pct, 2) if atr_pct is not None else None,
        "risk_level": level,
        "risk_notes": risk_notes or ["未触发主要风险阈值"],
        "score": score,
        "basis": basis,
        "disclaimer": (
            "价格区间由信号日行情按上述固定规则计算，仅作风险观察参考；"
            "不构成买卖指令，不保证成交或收益。A 股 T+1，买入后当日不能卖出。"
        ),
    }


def position_size(
    *,
    account_value: float,
    risk_pct: float,
    entry_price: float,
    stop_price: float,
    lot_size: int = 100,
) -> dict[str, Any]:
    """按“单笔最大亏损占总资产比例”计算参考仓位。

    A 股买入以 100 股为一手，结果向下取整；不包含无法成交、涨跌停等限制。
    """
    if account_value <= 0:
        raise ValueError("账户资产必须大于 0")
    if not 0 < risk_pct <= 10:
        raise ValueError("单笔风险比例应在 0~10% 之间")
    if entry_price <= 0 or stop_price <= 0 or stop_price >= entry_price:
        raise ValueError("止损价必须低于买入参考价且大于 0")
    per_share_risk = entry_price - stop_price
    max_loss = account_value * risk_pct / 100
    raw_shares = max_loss / per_share_risk
    lots = int(raw_shares // lot_size)
    shares = lots * lot_size
    position_value = shares * entry_price
    actual_risk = shares * per_share_risk
    return {
        "suggested_shares": shares,
        "suggested_lots": lots,
        "position_value": round(position_value, 2),
        "position_pct": round(position_value / account_value * 100, 2),
        "max_loss_amount": round(actual_risk, 2),
        "max_loss_pct": round(actual_risk / account_value * 100, 2),
        "per_share_risk": round(per_share_risk, 2),
        "rule": (
            "股数 = floor(账户资产×单笔风险比例÷每股风险÷100)×100；"
            "未计手续费、滑点与涨跌停无法成交的情况"
        ),
    }
