"""个股分析：真实日线 + 指标 + 技术摘要 + 风险参考。"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

import pandas as pd

from config import Settings, get_settings
from data_provider import (
    BENCHMARK_INDEX,
    DataProviderError,
    MarketDataProvider,
    normalize_code,
)
from indicators import add_indicators, attach_relative_strength, technical_summary
from market_calendar import latest_completed_trade_day
from risk_manager import build_risk_plan


def analyze_stock(
    code: str,
    provider: MarketDataProvider | None = None,
    settings: Settings | None = None,
    history_days: int = 300,
) -> dict[str, Any]:
    settings = settings or get_settings()
    provider = provider or MarketDataProvider(settings)
    code = normalize_code(code)
    warnings: list[str] = []

    # K 线只取已收盘的完整日线；实时报价单独获取并标注时间，二者不混用。
    try:
        analysis_date, cal = latest_completed_trade_day(settings=settings)
        if not cal.reliable:
            warnings.append(cal.warning or "交易日历不可靠")
    except Exception as exc:  # noqa: BLE001
        analysis_date = datetime.now(settings.timezone).date()
        warnings.append(f"无法确认最近完整交易日，改用自然日截止：{exc}")

    start = analysis_date - timedelta(days=history_days)
    history = provider.get_stock_history(code, start=start, end=analysis_date, adjust="qfq")
    requested_date = analysis_date
    frame = history.frame.copy()
    frame = frame[pd.to_datetime(frame["date"]).dt.date <= analysis_date]
    if frame.empty:
        raise DataProviderError(f"{code} 在 {analysis_date} 之前没有可用日线")
    actual_date = pd.Timestamp(frame["date"].max()).date()
    if actual_date != requested_date:
        warnings.append(
            f"该股票最后一根日线为 {actual_date}，不是最近交易日 {requested_date}；"
            "可能停牌或数据源滞后，页面不会把旧数据当作最新收盘。"
        )
    analysis_date = actual_date
    frame = add_indicators(frame)
    try:
        benchmark = provider.get_index_history(
            BENCHMARK_INDEX, start=start, end=analysis_date
        )
        frame = attach_relative_strength(frame, benchmark.frame)
        benchmark_meta = {
            "code": BENCHMARK_INDEX,
            "latest_date": benchmark.latest_date.isoformat() if benchmark.latest_date else None,
            "source": benchmark.source,
        }
    except Exception as exc:  # noqa: BLE001
        warnings.append(f"基准指数加载失败，相对强度不可用：{exc}")
        frame = attach_relative_strength(frame, pd.DataFrame())
        benchmark_meta = None

    try:
        quote = provider.get_stock_quote(code)
    except Exception as exc:  # noqa: BLE001
        quote = None
        warnings.append(f"实时/最新报价获取失败：{exc}")

    if len(frame) < 20:
        raise DataProviderError(f"{code} 有效日线仅 {len(frame)} 条，无法生成技术分析")
    summary = technical_summary(frame)
    latest_row = frame.iloc[-1]
    try:
        risk = build_risk_plan(
            latest_row,
            data_date=analysis_date,
            mode="analysis",
            settings=settings,
        )
    except ValueError as exc:
        risk = {"error": str(exc), "data_date": analysis_date.isoformat()}
        warnings.append(f"风险参考不可用：{exc}")

    warnings.extend(history.warnings)
    return {
        "code": code,
        "name": (quote or {}).get("name") or code,
        "quote": quote,
        "analysis_date": analysis_date.isoformat(),
        "frame": frame,
        "history": history,
        "benchmark": benchmark_meta,
        "summary": summary,
        "risk": risk,
        "warnings": list(dict.fromkeys(warnings)),
        "disclaimer": (
            "本页只做历史行情技术分析，不预测必然涨跌，不构成投资建议。"
            "技术指标可能失效，实际交易还需考虑公告、流动性和个人风险承受能力。"
        ),
    }
