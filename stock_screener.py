"""盘前 / 盘后选股流水线。

时点纪律：
1. 先确定“信号日”= 最近一个已经收盘并完成结算的交易日；
2. 所有个股和基准日线在计算指标前都裁剪到 signal_date；
3. 盘前任务绝不会把 target_date 当天的价格、成交量或涨跌幅写入特征；
4. 若最后一根日线不是 signal_date（停牌/数据滞后），该股票不会被选入。

选股范围不是全部 5000+ 只股票。为了在合理请求量内完成，先按信号日
成交额取流动性靠前的一批股票，再逐只拉取日线。该限制会在结果的
selection_stats / limitations 中明确披露。
"""
from __future__ import annotations

import math
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Callable

import numpy as np
import pandas as pd

from config import SCORE_LABELS, SCORE_WEIGHTS, Settings, get_settings
from data_provider import (
    BENCHMARK_INDEX,
    DataProviderError,
    HistoryResult,
    MarketDataProvider,
    SnapshotResult,
)
from indicators import add_indicators, attach_relative_strength, latest_value
from market_calendar import (
    CalendarInfo,
    latest_completed_trade_day,
    load_calendar,
    next_trade_day,
    target_session_for_premarket,
)
from market_rules import price_limit_pct
from risk_manager import build_risk_plan
from scoring import score_stock
from storage import load_universe, save_result, save_universe

Progress = Callable[[str], None]

MARKET_NAME = {
    "sh000001": "上证指数",
    "sz399001": "深证成指",
    "sz399006": "创业板指",
    "sh000300": "沪深300",
    "sh000688": "科创50",
}

EXCLUDED_NAME_MARKERS = ("ST", "*ST", "退", "退市", "N", "C")


@dataclass
class SelectionDates:
    signal_date: date
    target_date: date
    calendar: CalendarInfo


class StockScreener:
    def __init__(self, provider: MarketDataProvider | None = None, settings: Settings | None = None):
        self.settings = settings or get_settings()
        self.provider = provider or MarketDataProvider(self.settings)

    # ------------------------------------------------------------------
    # 日期与交易日历
    # ------------------------------------------------------------------
    def resolve_dates(
        self,
        mode: str,
        now: datetime | None = None,
        signal_date: str | date | None = None,
    ) -> SelectionDates:
        now = now or datetime.now(self.settings.timezone)
        calendar = load_calendar()
        explicit_signal = signal_date is not None
        if signal_date:
            requested = date.fromisoformat(str(signal_date)[:10])
            if requested not in set(calendar.dates):
                raise ValueError(f"{requested} 不是交易日，不能作为信号日")
            latest, latest_cal = latest_completed_trade_day(now=now, settings=self.settings)
            if requested > latest:
                raise ValueError(
                    f"信号日 {requested} 尚未收盘/结算，最近可用完整交易日为 {latest}"
                )
            signal = requested
            calendar = latest_cal if not latest_cal.reliable else calendar
        else:
            signal, latest_cal = latest_completed_trade_day(now=now, settings=self.settings)
            if not latest_cal.reliable:
                calendar = latest_cal

        if mode == "premarket":
            target, target_cal = target_session_for_premarket(now=now, settings=self.settings)
            if explicit_signal or target <= signal:
                # 手工回放历史日期时，盘前目标应当是信号日后的下一个交易日。
                target = next_trade_day(signal)
            calendar = target_cal if not target_cal.reliable else calendar
        elif mode == "postmarket":
            target = next_trade_day(signal)
        else:
            raise ValueError("mode 必须为 premarket 或 postmarket")

        if target <= signal:
            raise ValueError("目标交易日必须晚于信号日")
        return SelectionDates(signal_date=signal, target_date=target, calendar=calendar)

    # ------------------------------------------------------------------
    # Universe
    # ------------------------------------------------------------------
    def build_universe(
        self,
        signal_date: date,
        force: bool = False,
        progress: Progress | None = None,
    ) -> tuple[pd.DataFrame, dict[str, Any], list[str]]:
        cached = None if force else load_universe(signal_date.isoformat(), self.settings)
        if cached and cached.get("records"):
            frame = pd.DataFrame(cached["records"])
            if not frame.empty:
                meta = dict(cached.get("meta") or {})
                meta["cached"] = True
                return frame, meta, list(cached.get("warnings") or [])

        progress and progress("正在获取全市场快照，构建当日可交易股票池…")
        snapshot: SnapshotResult = self.provider.get_spot_snapshot(
            max_rows=max(self.settings.universe_size * 3, 300)
        )
        warnings = list(snapshot.warnings)
        if snapshot.quote_date is None:
            raise DataProviderError(
                "无法验证全市场快照所属交易日，为避免使用未知日期数据，已中止选股"
            )
        if snapshot.quote_date > signal_date:
            raise DataProviderError(
                f"快照行情日期 {snapshot.quote_date} 晚于信号日 {signal_date}，"
                "为防止未来数据泄漏，已中止选股"
            )
        if snapshot.quote_date < signal_date:
            warnings.append(
                f"用于构建股票池的快照日期为 {snapshot.quote_date}，早于信号日 "
                f"{signal_date}；流动性排序为较早交易日数据，结果中已标注"
            )
        if snapshot.stale:
            warnings.append("全市场快照来自本地缓存，非实时数据；已明确标记")

        frame = snapshot.frame.copy()
        frame["code"] = frame["code"].astype(str).str.zfill(6)
        frame["name"] = frame["name"].astype(str)
        frame = frame[frame["code"].str.match(r"^(0|3|6)\d{5}$")]
        frame = frame[~frame["code"].str.startswith(("4", "8", "9"))]
        upper_names = frame["name"].str.upper()
        frame = frame[~upper_names.str.contains("ST|退", regex=True, na=False)]
        frame = frame[
            (frame["price"] >= self.settings.min_price)
            & (frame["price"] <= self.settings.max_price)
        ]
        frame = frame.sort_values("amount", ascending=False).head(self.settings.universe_size)
        if len(frame) < 20:
            raise DataProviderError(f"过滤后股票池仅 {len(frame)} 只，不足以进行横截面选股")

        meta = {
            "source": snapshot.source,
            "snapshot_date": snapshot.quote_date.isoformat(),
            "snapshot_time": snapshot.quote_time,
            "fetched_at": snapshot.fetched_at,
            "requested": int(len(snapshot.frame)),
            "selected": int(len(frame)),
            "rank_rule": "按信号日成交额降序，剔除 ST/退市/北交所/B 股与异常价格",
            "snapshot_is_exact_signal_date": snapshot.quote_date == signal_date,
        }
        records = frame.to_dict(orient="records")
        save_universe(
            signal_date.isoformat(),
            {"meta": meta, "records": records, "warnings": warnings},
            self.settings,
        )
        return frame.reset_index(drop=True), meta, warnings

    # ------------------------------------------------------------------
    # 主流程
    # ------------------------------------------------------------------
    def run(
        self,
        mode: str,
        *,
        now: datetime | None = None,
        signal_date: str | date | None = None,
        force: bool = False,
        progress: Progress | None = None,
        run_context: str = "manual",
        extra_warnings: list[str] | None = None,
    ) -> dict[str, Any]:
        if mode not in {"premarket", "postmarket"}:
            raise ValueError("mode 必须为 premarket 或 postmarket")
        started = time.time()
        run_time = now or datetime.now(self.settings.timezone)
        progress = progress or (lambda _: None)
        dates = self.resolve_dates(mode, now=run_time, signal_date=signal_date)
        signal = dates.signal_date
        target = dates.target_date
        warnings: list[str] = list(extra_warnings or [])
        if not dates.calendar.reliable:
            warnings.append(dates.calendar.warning or "交易日历不可靠，请人工核对节假日")
        progress(
            f"任务日期：信号日 {signal}，目标日 {target}；"
            f"交易日历：{dates.calendar.source}"
        )

        universe, universe_meta, universe_warnings = self.build_universe(
            signal, force=force, progress=progress
        )
        warnings.extend(universe_warnings)
        if not universe_meta.get("snapshot_is_exact_signal_date"):
            warnings.append(
                "股票池流动性排序并非信号日当天快照，不能视作信号日实时排名"
            )

        progress(f"股票池 {len(universe)} 只；开始获取截至 {signal} 的日线…")
        benchmark_result = self._load_benchmark(signal, force=force)
        progress(
            f"基准 {BENCHMARK_INDEX} 已加载：{benchmark_result.latest_date}，"
            f"来源 {benchmark_result.source}"
        )
        if "AKShare" not in benchmark_result.source and "东方财富" not in benchmark_result.source:
            warnings.append(
                f"基准指数日线使用备用真实源：{benchmark_result.source}；"
                "相对强度按该源收盘价计算。"
            )

        fetch_count = min(len(universe), self.settings.max_history_fetch)
        to_fetch = universe.head(fetch_count).to_dict(orient="records")
        histories, failures, skipped = self._fetch_histories(
            to_fetch, signal, benchmark_result, force=force, progress=progress
        )
        if len(histories) < 5:
            detail = "；".join(f"{k}: {v}" for k, v in list(failures.items())[:5])
            raise DataProviderError(
                f"仅成功获取 {len(histories)} 只股票日线，无法形成可靠横截面。{detail}"
            )
        source_counts: dict[str, int] = {}
        for _, history, _ in histories:
            source_counts[history.source] = source_counts.get(history.source, 0) + 1
        fallback_sources = [
            source
            for source in source_counts
            if "AKShare" not in source and "东方财富" not in source
        ]
        if fallback_sources:
            warnings.append(
                "首选 AKShare/东方财富个股日线不可用或不完整，本次使用备用真实数据源："
                + "、".join(f"{s}（{n} 只）" for s, n in sorted(source_counts.items()) if s in fallback_sources)
                + "。请结合数据日期复核。"
            )

        progress("计算指标、评分与风险参考…")
        candidates: list[dict[str, Any]] = []
        filtered: dict[str, int] = {
            "suspended_or_stale": 0,
            "insufficient_history": 0,
            "price_out_of_range": 0,
            "liquidity": 0,
            "extreme_volatility": 0,
            "below_score": 0,
            "non_qfq": 0,
            "estimated_amount": 0,
        }
        for code, result, name in histories:
            try:
                candidate = self._score_one(
                    code=code,
                    name=name,
                    result=result,
                    signal_date=signal,
                    mode=mode,
                    benchmark=benchmark_result.frame,
                    filtered=filtered,
                )
                if candidate is not None:
                    candidates.append(candidate)
            except Exception as exc:  # noqa: BLE001 - 单只失败不应拖垮整个任务
                failures[code] = f"评分失败：{type(exc).__name__}: {exc}"

        candidates.sort(
            key=lambda x: (
                -float(x["score"]),
                -float(x.get("rank_score", x["score"])),
                -float(x.get("amount_ma20") or 0),
                x["code"],
            )
        )
        selected = candidates[: self.settings.max_candidates]
        for idx, item in enumerate(selected, start=1):
            item["rank"] = idx

        market_context = self._market_context(warnings)
        elapsed = round(time.time() - started, 1)
        selected_sources = sorted(
            {item["data_source"] for item in selected if item.get("data_source")}
        )
        payload: dict[str, Any] = {
            "schema_version": 1,
            "mode": mode,
            "mode_label": "盘前选股" if mode == "premarket" else "盘后选股",
            "generated_at": run_time.astimezone(self.settings.timezone).isoformat(timespec="seconds"),
            "signal_date": signal.isoformat(),
            "target_date": target.isoformat(),
            "data_as_of": signal.isoformat(),
            "data_discipline": (
                "所有特征使用 signal_date 及之前的完整日线；盘前结果不含目标日行情。"
                if mode == "premarket"
                else "使用 signal_date 收盘后的完整日线，目标为下一交易日观察池。"
            ),
            "calendar": {
                "source": dates.calendar.source,
                "reliable": dates.calendar.reliable,
                "warning": dates.calendar.warning,
            },
            "universe": universe_meta,
            "benchmark": {
                "code": BENCHMARK_INDEX,
                "name": MARKET_NAME[BENCHMARK_INDEX],
                "latest_date": benchmark_result.latest_date.isoformat()
                if benchmark_result.latest_date
                else None,
                "source": benchmark_result.source,
                "fetched_at": benchmark_result.fetched_at,
                "cached": benchmark_result.cached,
            },
            "market": market_context,
            "candidates": selected,
            "selection_stats": {
                "universe_size": int(len(universe)),
                "history_fetch_limit": int(fetch_count),
                "history_success": int(len(histories)),
                "history_failed": int(len(failures)),
                "history_skipped": skipped,
                "history_source_counts": source_counts,
                "filtered": filtered,
                "candidates_before_top_n": int(len(candidates)),
                "selected": int(len(selected)),
                "elapsed_seconds": elapsed,
            },
            "score_weights": SCORE_WEIGHTS,
            "score_labels": SCORE_LABELS,
            "rank_rule": "按综合评分降序；同分再按模式适配分、20 日成交额和代码排序",
            "data_sources": [
                universe_meta.get("source"),
                benchmark_result.source,
                *selected_sources,
            ],
            "warnings": _dedupe(warnings),
            "failures_preview": dict(list(failures.items())[:20]),
            "limitations": [
                "第一阶段只做行情分析、选股、回测与风险管理，不连接券商、不自动下单。",
                "股票池按信号日成交额取流动性靠前样本，不是全市场 5000+ 只逐只扫描。",
                "日线使用前复权价；复权因子会随除权除息变化，历史缓存可能需刷新。",
                f"评分低于 {self.settings.min_candidate_score:g} 分的股票不会被补足；"
                "候选不足时不使用任何模拟数据填充。",
                "未纳入公告、财报、龙虎榜、新闻和盘中盘口，不能替代人工复核。",
                "价格为历史收盘/参考价，不代表下一交易日可成交价；所有结果不承诺收益。",
            ],
            "run_context": run_context,
        }
        path = save_result(payload, self.settings)
        payload["_saved_path"] = str(path)
        progress(
            f"完成：{len(selected)} 只候选，用时 {elapsed}s，结果保存至 {path.name}"
        )
        return payload

    # ------------------------------------------------------------------
    # 数据加载
    # ------------------------------------------------------------------
    def _load_benchmark(self, signal: date, force: bool = False) -> HistoryResult:
        start = signal - timedelta(days=500)
        result = self.provider.get_index_history(BENCHMARK_INDEX, start=start, end=signal, force=force)
        frame = result.frame.copy()
        frame["date"] = pd.to_datetime(frame["date"]).dt.normalize()
        frame = frame[frame["date"].dt.date <= signal].reset_index(drop=True)
        if frame.empty:
            raise DataProviderError("基准指数日线为空，无法计算相对强度")
        latest = pd.Timestamp(frame["date"].iloc[-1]).date()
        if latest != signal:
            raise DataProviderError(
                f"基准指数最新日线为 {latest}，未更新至信号日 {signal}；"
                "相对强度评分会失真，已中止选股"
            )
        result.frame = frame
        return result

    def _fetch_histories(
        self,
        rows: list[dict[str, Any]],
        signal: date,
        benchmark: HistoryResult,
        *,
        force: bool,
        progress: Progress,
    ) -> tuple[list[tuple[str, HistoryResult, str]], dict[str, str], dict[str, int]]:
        start = signal - timedelta(days=520)
        failures: dict[str, str] = {}
        skipped = {"suspended": 0, "non_qfq": 0, "too_short": 0}
        histories: list[tuple[str, HistoryResult, str]] = []
        total = len(rows)
        completed = 0
        max_workers = max(1, min(self.settings.max_workers, 8))
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = {
                pool.submit(
                    self.provider.get_stock_history,
                    str(row["code"]),
                    start,
                    signal,
                    "qfq",
                    force,
                ): row
                for row in rows
            }
            for future in as_completed(futures):
                row = futures[future]
                code = str(row["code"])
                completed += 1
                if completed % 20 == 0 or completed == total:
                    progress(f"日线进度 {completed}/{total}…")
                try:
                    result = future.result()
                except Exception as exc:  # noqa: BLE001
                    failures[code] = f"{type(exc).__name__}: {exc}"
                    continue
                frame = result.frame.copy()
                frame["date"] = pd.to_datetime(frame["date"]).dt.normalize()
                frame = frame[frame["date"].dt.date <= signal].reset_index(drop=True)
                if frame.empty:
                    failures[code] = "裁剪到信号日后无数据"
                    continue
                latest = pd.Timestamp(frame["date"].iloc[-1]).date()
                if latest != signal:
                    skipped["suspended"] += 1
                    failures[code] = f"最后一根日线 {latest} ≠ 信号日 {signal}（停牌或数据滞后）"
                    continue
                if len(frame) < self.settings.min_history_bars:
                    skipped["too_short"] += 1
                    failures[code] = f"有效日线仅 {len(frame)} 条"
                    continue
                if result.adjust != "qfq":
                    skipped["non_qfq"] += 1
                    failures[code] = f"复权方式为 {result.adjust}，为避免复权跳变误判已跳过"
                    continue
                result.frame = frame
                histories.append((code, result, str(row.get("name", ""))))
        return histories, failures, skipped

    def _score_one(
        self,
        *,
        code: str,
        name: str,
        result: HistoryResult,
        signal_date: date,
        mode: str,
        benchmark: pd.DataFrame,
        filtered: dict[str, int],
    ) -> dict[str, Any] | None:
        frame = add_indicators(result.frame)
        frame = attach_relative_strength(frame, benchmark)
        limit_pct = price_limit_pct(code, name)
        frame["limit_pct"] = limit_pct
        row = frame.iloc[-1]

        close = float(row["close"])
        if not math.isfinite(close) or close < self.settings.min_price or close > self.settings.max_price:
            filtered["price_out_of_range"] += 1
            return None
        if latest_value(frame, "volume") <= 0:
            filtered["suspended_or_stale"] += 1
            return None
        if "amount_estimated" in frame.columns and bool(
            frame["amount_estimated"].tail(20).fillna(False).any()
        ):
            # 备用源缺少真实成交额时只给出 price×volume 估算值；不能拿估算值
            # 冒充真实流动性参与横截面评分。
            filtered["estimated_amount"] = filtered.get("estimated_amount", 0) + 1
            return None
        amount_ma20 = latest_value(frame, "amount_ma20")
        if not math.isfinite(amount_ma20) or amount_ma20 < self.settings.min_avg_amount:
            # 部分备用源没有真实成交额，不能用价格×成交量冒充真实成交额。
            if not math.isfinite(amount_ma20):
                filtered["liquidity"] += 1
                return None
            filtered["liquidity"] += 1
            return None
        atr_pct = latest_value(frame, "atr_pct")
        if math.isfinite(atr_pct) and atr_pct > 10:
            filtered["extreme_volatility"] += 1
            return None

        result_score = score_stock(row, mode=mode, settings=self.settings)
        if result_score.total < self.settings.min_candidate_score:
            filtered["below_score"] += 1
            return None

        # 盘前额外剔除“收盘已封死涨停”的标的：次日无法按常规价格观察。
        if mode == "premarket" and latest_value(frame, "pct_chg") >= limit_pct - 0.3:
            filtered.setdefault("limit_up_close", 0)
            filtered["limit_up_close"] += 1
            return None

        risk = build_risk_plan(
            row,
            data_date=signal_date,
            mode=mode,
            score=result_score.total,
            settings=self.settings,
        )
        scoring_dict = result_score.to_dict()
        rr = risk.get("risk_reward_ratio")
        if rr is not None and rr < 1.2:
            scoring_dict["risks"].append(
                f"按当前收盘价计算的风险收益比仅 {rr:.2f}，现价追入的性价比偏低"
            )
        setup = _setup_tags(row)
        rank_score = result_score.total
        if mode == "premarket":
            # 盘前更看重可控波动与均线附近的可观察性，不改变对外 100 分总分。
            if math.isfinite(atr_pct):
                rank_score += max(-2.0, min(1.5, (4.0 - atr_pct) * 0.4))
            if setup and ("回踩" in setup or "趋势延续" in setup):
                rank_score += 0.5
        else:
            if "突破" in setup:
                rank_score += 0.8
            if latest_value(frame, "macd_hist") > 0:
                rank_score += 0.3

        candidate = {
            "code": code,
            "name": name or str(code),
            "score": scoring_dict["score"],
            "rank_score": round(float(rank_score), 2),
            "score_breakdown": scoring_dict["score_breakdown"],
            "reasons": scoring_dict["reasons"],
            "risks": scoring_dict["risks"],
            "setup": setup,
            "close": round(close, 2),
            "pct_chg": _round(latest_value(frame, "pct_chg"), 2),
            "limit_pct": limit_pct,
            "data_date": signal_date.isoformat(),
            "history_bars": int(len(frame)),
            "ma5": _round(latest_value(frame, "ma5"), 2),
            "ma10": _round(latest_value(frame, "ma10"), 2),
            "ma20": _round(latest_value(frame, "ma20"), 2),
            "ma60": _round(latest_value(frame, "ma60"), 2),
            "macd_dif": _round(latest_value(frame, "macd_dif"), 3),
            "macd_dea": _round(latest_value(frame, "macd_dea"), 3),
            "macd_hist": _round(latest_value(frame, "macd_hist"), 3),
            "rsi14": _round(latest_value(frame, "rsi14"), 2),
            "atr_pct": _round(atr_pct, 2),
            "vol_ratio": _round(latest_value(frame, "vol_ratio"), 2),
            "amount_ma20": _round(amount_ma20, 2),
            "excess_5": _round(latest_value(frame, "excess_5"), 2),
            "excess_10": _round(latest_value(frame, "excess_10"), 2),
            "excess_20": _round(latest_value(frame, "excess_20"), 2),
            "drawdown60_pct": _round(latest_value(frame, "drawdown60_pct"), 2),
            "risk_plan": risk,
            "data_source": result.source,
            "data_fetched_at": result.fetched_at,
            "data_cached": result.cached,
            "data_warnings": result.warnings,
        }
        return candidate

    def _market_context(self, warnings: list[str]) -> list[dict[str, Any]]:
        try:
            quotes = self.provider.get_index_quotes()
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"指数行情获取失败：{exc}")
            return []
        result = []
        for quote in quotes:
            result.append(
                {
                    "code": quote.get("code"),
                    "name": quote.get("name"),
                    "price": _round(quote.get("price"), 2),
                    "pct_chg": _round(quote.get("pct_chg"), 2),
                    "quote_date": quote.get("quote_date"),
                    "quote_time": quote.get("quote_time"),
                    "source": quote.get("source"),
                }
            )
        return result


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------
def _round(value: Any, digits: int = 2) -> float | None:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(f):
        return None
    return round(f, digits)


def _setup_tags(row: pd.Series) -> list[str]:
    tags: list[str] = []
    close = float(row["close"])
    ma5 = float(row.get("ma5", np.nan))
    ma10 = float(row.get("ma10", np.nan))
    ma20 = float(row.get("ma20", np.nan))
    if bool(row.get("breakout20", False)):
        tags.append("突破20日高点")
    if math.isfinite(ma10) and close >= ma10 * 0.98:
        low = float(row.get("low", close))
        if low <= ma10 * 1.01 and close >= ma10:
            tags.append("均线回踩确认")
    if all(math.isfinite(v) for v in (ma5, ma10, ma20)) and close > ma5 > ma10 > ma20:
        tags.append("多头趋势延续")
    dif, dea = float(row.get("macd_dif", np.nan)), float(row.get("macd_dea", np.nan))
    if math.isfinite(dif) and math.isfinite(dea) and dif > dea:
        tags.append("MACD多头")
    if bool(row.get("breakout60", False)):
        tags.append("突破60日高点")
    return _dedupe(tags)


def _dedupe(items: list[str]) -> list[str]:
    out: list[str] = []
    for item in items:
        if item and item not in out:
            out.append(item)
    return out
