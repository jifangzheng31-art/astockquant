"""100 分制综合评分，独立于数据获取、界面和回测。

固定权重：趋势 25 + 量价 20 + 动量 20 + 相对强度 20 + 风险 15。
每一个子项都只读取“信号日及之前”的指标行；修改规则不会影响数据层。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

import pandas as pd

from config import SCORE_LABELS, SCORE_WEIGHTS, Settings, get_settings

if abs(sum(SCORE_WEIGHTS.values()) - 100.0) > 1e-9:
    raise ValueError(f"评分权重之和必须为 100，当前为 {sum(SCORE_WEIGHTS.values())}")


@dataclass
class ScoringResult:
    total: float
    breakdown: dict[str, float]
    reasons: list[str] = field(default_factory=list)
    risk_flags: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "score": round(float(self.total), 1),
            "score_breakdown": {k: round(float(v), 1) for k, v in self.breakdown.items()},
            "score_labels": SCORE_LABELS,
            "reasons": self.reasons,
            "risks": self.risk_flags,
        }


def _num(row: pd.Series | dict[str, Any], key: str, default: float = float("nan")) -> float:
    try:
        value = row.get(key, default)  # type: ignore[union-attr]
        out = float(value)
    except (TypeError, ValueError):
        return default
    return out if math.isfinite(out) else default


def _fmt(value: float, suffix: str = "") -> str:
    return f"{value:.2f}{suffix}" if math.isfinite(value) else "数据不足"


def score_trend(row: pd.Series) -> tuple[float, list[str], list[str]]:
    close = _num(row, "close")
    ma5, ma10, ma20 = _num(row, "ma5"), _num(row, "ma10"), _num(row, "ma20")
    ma60 = _num(row, "ma60")
    slope = _num(row, "ma20_slope_5")
    dist = _num(row, "distance_ma20_pct")
    score = 0.0
    notes: list[str] = []
    risks: list[str] = []

    if math.isfinite(ma5) and close > ma5:
        score += 4
        notes.append("收盘价站上 MA5")
    if math.isfinite(ma5) and math.isfinite(ma10) and ma5 > ma10:
        score += 4
        notes.append("MA5 高于 MA10")
    if math.isfinite(ma10) and math.isfinite(ma20) and ma10 > ma20:
        score += 4
        notes.append("MA10 高于 MA20")
    if math.isfinite(slope) and slope > 0:
        score += 5
        notes.append(f"MA20 近 5 日斜率 +{slope:.2f}%")
    if math.isfinite(ma20) and close > ma20:
        score += 4
        notes.append("收盘价位于 MA20 上方")
    if math.isfinite(ma60) and close > ma60:
        score += 4
        notes.append("收盘价位于 MA60 上方")
    if math.isfinite(dist) and dist > 10:
        risks.append(f"股价高于 MA20 {dist:.1f}%，短线乖离偏大")

    if not notes:
        notes.append("均线趋势未形成多头结构")
    if math.isfinite(ma20) and close < ma20:
        risks.append("收盘价低于 MA20，趋势尚未转强")
    return score, notes, risks


def score_volume_price(row: pd.Series, settings: Settings | None = None) -> tuple[float, list[str], list[str]]:
    settings = settings or get_settings()
    score = 0.0
    notes: list[str] = []
    risks: list[str] = []
    amount = _num(row, "amount_ma20")
    vol_ratio = _num(row, "vol_ratio")
    pct = _num(row, "pct_chg")
    limit_pct = _num(row, "limit_pct", 10.0)
    breakout = bool(row.get("breakout20", False))

    if math.isfinite(amount):
        if amount >= 1_000_000_000:
            score += 6
            notes.append(f"20 日平均成交额 {amount / 1e8:.1f} 亿元，流动性充足")
        elif amount >= 500_000_000:
            score += 5
            notes.append(f"20 日平均成交额 {amount / 1e8:.1f} 亿元，流动性较好")
        elif amount >= 200_000_000:
            score += 4
        elif amount >= settings.min_avg_amount:
            score += 2.5
        if amount < settings.min_avg_amount:
            risks.append(f"20 日平均成交额仅 {amount / 1e8:.2f} 亿元，流动性偏弱")

    if math.isfinite(vol_ratio):
        if 1.0 <= vol_ratio <= 2.5:
            score += 6
            notes.append(f"量比（对 5 日均量）{vol_ratio:.2f}，温和放量")
        elif 0.8 <= vol_ratio < 1.0:
            score += 4
        elif 2.5 < vol_ratio <= 4.0:
            score += 3
            notes.append(f"量比 {vol_ratio:.2f}，放量但未极端")
        elif 0.6 <= vol_ratio < 0.8:
            score += 2
        elif vol_ratio > 4:
            score += 1
            risks.append(f"量比 {vol_ratio:.2f}，单日成交异常放大，需防情绪退潮")
        else:
            score += 1
            risks.append(f"量比 {vol_ratio:.2f}，成交量明显萎缩")

    if math.isfinite(pct) and math.isfinite(vol_ratio):
        if pct > 0 and vol_ratio >= 1.2:
            score += 8
            notes.append("价涨量增，量价配合较好")
        elif pct > 0 and 0.8 <= vol_ratio < 1.2:
            score += 5
            notes.append("价格上涨但量能一般")
        elif pct < 0 and vol_ratio <= 0.8:
            score += 6
            notes.append("缩量回调，抛压相对有限")
        elif pct < 0 and vol_ratio > 1.5:
            score += 1
            risks.append("价跌且明显放量，存在资金流出迹象")
        else:
            score += 3

    if breakout:
        notes.append("收盘价突破前 20 日高点（不含当日）")
    if math.isfinite(pct) and pct >= limit_pct - 0.3:
        risks.append("当日接近涨停，次日开盘溢价与回落风险较大")
    return min(score, 20.0), notes, risks


def score_momentum(row: pd.Series) -> tuple[float, list[str], list[str]]:
    score = 0.0
    notes: list[str] = []
    risks: list[str] = []
    ret5, ret10, ret20 = _num(row, "ret_5"), _num(row, "ret_10"), _num(row, "ret_20")
    rsi14 = _num(row, "rsi14")

    if math.isfinite(ret5):
        if 2 <= ret5 <= 12:
            score += 6
            notes.append(f"近 5 日上涨 {ret5:.1f}%，短线上攻但未过度")
        elif 0 < ret5 < 2:
            score += 5
        elif ret5 > 12:
            score += 4
            risks.append(f"近 5 日已涨 {ret5:.1f}%，短线获利盘较多")
        elif ret5 >= -2:
            score += 3
        else:
            score += 1
    if math.isfinite(ret10):
        if 0 < ret10 <= 20:
            score += 5
        elif ret10 > 20:
            score += 3
            risks.append(f"近 10 日涨幅 {ret10:.1f}%，存在追高风险")
    if math.isfinite(ret20):
        if 0 < ret20 <= 30:
            score += 5
        elif ret20 > 30:
            score += 3
            risks.append(f"近 20 日涨幅 {ret20:.1f}%，中期涨幅已较大")
    if math.isfinite(rsi14):
        if 50 <= rsi14 <= 70:
            score += 4
            notes.append(f"RSI14 {rsi14:.1f}，动量健康")
        elif 45 <= rsi14 < 50 or 70 < rsi14 <= 80:
            score += 2
        if rsi14 > 80:
            risks.append(f"RSI14 {rsi14:.1f}，明显超买")
        elif rsi14 < 30:
            risks.append(f"RSI14 {rsi14:.1f}，弱势超卖，趋势反转未确认")
    if not notes:
        notes.append("动量信号一般")
    return min(score, 20.0), notes, risks


def score_relative_strength(row: pd.Series) -> tuple[float, list[str], list[str]]:
    score = 0.0
    notes: list[str] = []
    risks: list[str] = []
    e5, e10, e20 = _num(row, "excess_5"), _num(row, "excess_10"), _num(row, "excess_20")

    if math.isfinite(e5) and e5 > 0:
        score += 4
        if e5 > 2:
            score += 2
    if math.isfinite(e10) and e10 > 0:
        score += 4
        if e10 > 3:
            score += 1
    if math.isfinite(e20) and e20 > 0:
        score += 5
        if e20 > 5:
            score += 1
    if all(math.isfinite(v) and v > 0 for v in (e5, e10, e20)):
        score += 3
        notes.append(f"5/10/20 日相对沪深300超额为 {e5:.1f}/{e10:.1f}/{e20:.1f} 个百分点")
    elif math.isfinite(e20) and e20 <= 0:
        risks.append(f"近 20 日跑输沪深300 {abs(e20):.1f} 个百分点")
    if not notes:
        notes.append("相对强度未形成持续优势")
    return min(score, 20.0), notes, risks


def score_risk(row: pd.Series, settings: Settings | None = None) -> tuple[float, list[str], list[str]]:
    settings = settings or get_settings()
    score = 0.0
    notes: list[str] = []
    risks: list[str] = []
    atr_pct = _num(row, "atr_pct")
    drawdown = _num(row, "drawdown60_pct")
    dist = _num(row, "distance_ma20_pct")
    vol_ratio = _num(row, "vol_ratio")
    pct = _num(row, "pct_chg")
    limit_pct = _num(row, "limit_pct", 10.0)

    if math.isfinite(atr_pct):
        if atr_pct <= 2.5:
            score += 5
            notes.append(f"ATR 波动率 {atr_pct:.2f}%，波动可控")
        elif atr_pct <= 4:
            score += 4
        elif atr_pct <= 6:
            score += 2
            risks.append(f"ATR 波动率 {atr_pct:.2f}%，波动偏高")
        else:
            risks.append(f"ATR 波动率 {atr_pct:.2f}%，波动显著偏高")
    if math.isfinite(drawdown):
        if drawdown >= -10:
            score += 4
            notes.append("距 60 日高点回撤小于 10%")
        elif drawdown >= -20:
            score += 3
        elif drawdown >= -30:
            score += 1
            risks.append(f"距 60 日高点回撤 {abs(drawdown):.1f}%")
        else:
            risks.append(f"距 60 日高点回撤 {abs(drawdown):.1f}%，弱势明显")
    if math.isfinite(dist):
        if 0 <= dist <= 5:
            score += 3
        elif 5 < dist <= 10:
            score += 2
        elif 10 < dist <= 15:
            score += 1
            risks.append(f"距 MA20 乖离 {dist:.1f}%，短线回踩概率上升")
        elif dist > 15:
            risks.append(f"距 MA20 乖离 {dist:.1f}%，严重偏离均线")
    if math.isfinite(vol_ratio):
        if vol_ratio <= 4:
            score += 2
        else:
            risks.append("成交量异常放大，稳定性不足")
    if math.isfinite(pct) and pct < limit_pct - 0.3:
        score += 1
    elif math.isfinite(pct):
        risks.append("接近涨停收盘，次日可交易性存在不确定性")

    if not notes:
        notes.append("风险指标处于中性区间")
    if atr_pct > 6 or (math.isfinite(drawdown) and drawdown < -30):
        risks.append("高波动/深回撤标的应降低仓位并设置明确止损")
    return min(score, 15.0), notes, risks


def score_stock(
    row: pd.Series,
    mode: str = "postmarket",
    settings: Settings | None = None,
) -> ScoringResult:
    if mode not in {"premarket", "postmarket"}:
        raise ValueError("mode 必须为 premarket 或 postmarket")
    trend, trend_notes, trend_risks = score_trend(row)
    volume, volume_notes, volume_risks = score_volume_price(row, settings)
    momentum, momentum_notes, momentum_risks = score_momentum(row)
    relative, relative_notes, relative_risks = score_relative_strength(row)
    risk, risk_notes, risk_risks = score_risk(row, settings)

    breakdown = {
        "trend": round(trend, 2),
        "volume_price": round(volume, 2),
        "momentum": round(momentum, 2),
        "relative_strength": round(relative, 2),
        "risk": round(risk, 2),
    }
    total = round(sum(breakdown.values()), 1)

    reasons = _select_reasons(
        mode,
        trend_notes,
        volume_notes,
        momentum_notes,
        relative_notes,
        risk_notes,
        row,
    )
    risks = _dedupe(trend_risks + volume_risks + momentum_risks + relative_risks + risk_risks)
    return ScoringResult(total=total, breakdown=breakdown, reasons=reasons, risk_flags=risks)


def _select_reasons(
    mode: str,
    trend: list[str],
    volume: list[str],
    momentum: list[str],
    relative: list[str],
    risk: list[str],
    row: pd.Series,
) -> list[str]:
    reasons: list[str] = []
    reasons.extend(trend[:3])
    reasons.extend(volume[:2])
    reasons.extend(momentum[:2])
    reasons.extend(relative[:1])
    reasons.extend(risk[:1])

    close = _num(row, "close")
    support = _num(row, "support20")
    ma10, ma20 = _num(row, "ma10"), _num(row, "ma20")
    if math.isfinite(close) and math.isfinite(support) and support > 0:
        gap = (close / support - 1) * 100
        if 0 <= gap <= 6:
            reasons.append(f"收盘价距 20 日支撑 {gap:.1f}%，回撤空间相对可衡量")
    if math.isfinite(ma10) and math.isfinite(ma20):
        near_ma = min(abs(close / ma10 - 1), abs(close / ma20 - 1)) * 100
        if near_ma <= 2:
            reasons.append("股价靠近 MA10/MA20，属于均线回踩观察形态")
    if mode == "premarket":
        reasons.insert(0, "信号仅使用上一完整交易日收盘数据，未引用当日盘中价格")
    return _dedupe(reasons)[:8]


def _dedupe(items: list[str]) -> list[str]:
    out: list[str] = []
    for item in items:
        if item and item not in out:
            out.append(item)
    return out
