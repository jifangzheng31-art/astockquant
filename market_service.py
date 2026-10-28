"""市场总览聚合。行情完全来自 MarketDataProvider；失败时返回 error 字段。"""
from __future__ import annotations

import math
from typing import Any

import numpy as np
import pandas as pd

from config import Settings, get_settings
from data_provider import DataProviderError, MarketDataProvider
from market_rules import price_limit_pct


def limit_pct_for(code: str, name: str = "") -> float:
    """按板块估算涨跌幅限制，用于统计涨跌停家数，不用于订单校验。"""
    return price_limit_pct(code, name)


def build_market_overview(
    provider: MarketDataProvider | None = None,
    settings: Settings | None = None,
    force: bool = False,
) -> dict[str, Any]:
    settings = settings or get_settings()
    provider = provider or MarketDataProvider(settings)
    warnings: list[str] = []
    result: dict[str, Any] = {
        "indices": [],
        "breadth": None,
        "snapshot": None,
        "errors": [],
        "warnings": warnings,
        "generated_at": pd.Timestamp.now(tz=settings.timezone).isoformat(),
    }

    try:
        result["indices"] = provider.get_index_quotes(force=force)
        if any(q.get("stale") for q in result["indices"]):
            warnings.append("指数行情为上次成功获取的缓存，不是实时行情")
    except DataProviderError as exc:
        result["errors"].append(f"指数行情：{exc}")

    try:
        snapshot = provider.get_spot_snapshot(force=force)
        frame = snapshot.frame.copy()
        frame["pct_chg"] = pd.to_numeric(frame["pct_chg"], errors="coerce")
        frame["amount"] = pd.to_numeric(frame["amount"], errors="coerce").fillna(0)
        frame["price"] = pd.to_numeric(frame["price"], errors="coerce")
        frame["prev_close"] = pd.to_numeric(frame["prev_close"], errors="coerce")
        valid = frame.dropna(subset=["pct_chg", "price"]).copy()
        valid["limit_pct"] = [
            limit_pct_for(str(c), str(n)) for c, n in zip(valid["code"], valid["name"])
        ]
        advancers = int((valid["pct_chg"] > 0).sum())
        decliners = int((valid["pct_chg"] < 0).sum())
        unchanged = int((valid["pct_chg"] == 0).sum())
        total = int(len(valid))
        limit_up = int((valid["pct_chg"] >= valid["limit_pct"] - 0.35).sum())
        limit_down = int((valid["pct_chg"] <= -valid["limit_pct"] + 0.35).sum())
        up_ratio = advancers / total * 100 if total else float("nan")
        median_pct = float(valid["pct_chg"].median()) if total else float("nan")
        total_amount = float(valid["amount"].sum()) if total else float("nan")
        if up_ratio >= 70:
            temperature = "偏热"
        elif up_ratio >= 55:
            temperature = "偏强"
        elif up_ratio >= 45:
            temperature = "中性"
        elif up_ratio >= 30:
            temperature = "偏弱"
        else:
            temperature = "偏冷"
        result["breadth"] = {
            "total": total,
            "advancers": advancers,
            "decliners": decliners,
            "unchanged": unchanged,
            "up_ratio": round(up_ratio, 2) if math.isfinite(up_ratio) else None,
            "median_pct": round(median_pct, 2) if math.isfinite(median_pct) else None,
            "limit_up_est": limit_up,
            "limit_down_est": limit_down,
            "total_amount": total_amount if math.isfinite(total_amount) else None,
            "temperature": temperature,
            "limit_rule": "按主板10%、创业板/科创板20%、ST 5%估算，未覆盖新股等特殊规则",
        }
        result["snapshot"] = {
            "source": snapshot.source,
            "quote_date": snapshot.quote_date.isoformat() if snapshot.quote_date else None,
            "quote_time": snapshot.quote_time,
            "fetched_at": snapshot.fetched_at,
            "cached": snapshot.cached,
            "stale": snapshot.stale,
            "rows": int(len(frame)),
        }
        warnings.extend(snapshot.warnings)
        if snapshot.stale:
            result["errors"].append("全市场快照为缓存数据，不能视作实时行情")
    except DataProviderError as exc:
        result["errors"].append(f"市场快照：{exc}")

    return result


def format_amount_cn(value: float | None) -> str:
    if value is None or not math.isfinite(float(value)):
        return "—"
    value = float(value)
    if abs(value) >= 1e12:
        return f"{value / 1e12:.2f} 万亿"
    if abs(value) >= 1e8:
        return f"{value / 1e8:.1f} 亿"
    if abs(value) >= 1e4:
        return f"{value / 1e4:.0f} 万"
    return f"{value:.0f}"
