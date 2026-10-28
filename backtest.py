"""短线选股策略回测。

已实现的 A 股约束：
* 收盘产生信号，下一交易日开盘成交（信号日不使用未来数据）；
* T+1：买入当日不可能卖出，所有卖出至少在买入后的下一交易日；
* 佣金（含最低 5 元）、卖出印花税、过户费、双边滑点；
* 涨跌停：按主板 10%、创业板/科创板 20%、ST 5% 估算，开盘封板则拒绝成交；
* 停牌/缺失日线：无法交易，持仓顺延。

明确未精确实现（结果中会列出）：
* 新股上市初期无涨跌幅限制、退市整理期等特殊规则；
* ST 身份在历史日期上的变更（仅能按当前名称/代码规则近似）；
* 集合竞价撮合、盘口深度、冲击成本、分红税；
* 前复权价格在回测期内包含未来复权因子变化（数据层限制）。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Callable

import numpy as np
import pandas as pd

from config import Settings, get_settings
from data_provider import BENCHMARK_INDEX, MarketDataProvider, normalize_code
from indicators import add_indicators, attach_relative_strength
from market_calendar import latest_completed_trade_day
from market_rules import limit_price, price_limit_pct
from risk_manager import build_risk_plan
from scoring import score_stock

Progress = Callable[[str], None]


@dataclass(frozen=True)
class BacktestConfig:
    start_date: date
    end_date: date
    initial_cash: float = 100_000.0
    max_positions: int = 5
    holding_days: int = 3
    rebalance_days: int = 1
    min_score: float = 55.0
    min_avg_amount: float = 80_000_000.0
    min_price: float = 2.0
    max_price: float = 500.0
    max_position_pct: float = 0.30
    commission_rate: float = 0.0003
    min_commission: float = 5.0
    stamp_tax_sell: float = 0.0005
    transfer_fee: float = 0.00001
    slippage_bps: float = 10.0
    use_stop_loss: bool = True
    use_take_profit: bool = True


@dataclass
class Position:
    code: str
    name: str
    shares: int
    entry_price: float
    entry_date: date
    entry_idx: int
    entry_cost: float
    stop: float
    target: float
    last_close: float


@dataclass
class BacktestResult:
    metrics: dict[str, Any]
    equity_curve: pd.DataFrame
    trades: pd.DataFrame
    config: dict[str, Any]
    warnings: list[str] = field(default_factory=list)
    limitations: list[str] = field(default_factory=list)
    implemented_rules: list[str] = field(default_factory=list)
    diagnostics: dict[str, Any] = field(default_factory=dict)


class BacktestEngine:
    def __init__(
        self,
        provider: MarketDataProvider | None = None,
        settings: Settings | None = None,
    ):
        self.settings = settings or get_settings()
        self.provider = provider or MarketDataProvider(self.settings)

    def run(
        self,
        codes: list[str],
        config: BacktestConfig,
        *,
        names: dict[str, str] | None = None,
        progress: Progress | None = None,
    ) -> BacktestResult:
        progress = progress or (lambda _: None)
        codes = list(dict.fromkeys(normalize_code(c) for c in codes))
        if not codes:
            raise ValueError("回测股票池不能为空")
        if len(codes) > 60:
            raise ValueError("单次回测最多 60 只股票，避免触发数据源限流")
        if config.start_date >= config.end_date:
            raise ValueError("回测开始日期必须早于结束日期")
        today = datetime.now(self.settings.timezone).date()
        if config.end_date >= today:
            latest_complete, _ = latest_completed_trade_day(settings=self.settings)
            if config.end_date > latest_complete:
                raise ValueError(
                    f"回测结束日期 {config.end_date} 晚于最近完整交易日 {latest_complete}；"
                    "禁止用尚未发生的行情回测"
                )
        names = names or {}

        progress("加载基准指数日线…")
        warmup_start = config.start_date - timedelta(days=260)
        bench_result = self.provider.get_index_history(
            BENCHMARK_INDEX, start=warmup_start, end=config.end_date
        )
        bench = bench_result.frame.copy()
        bench["date"] = pd.to_datetime(bench["date"]).dt.normalize()
        bench = bench[bench["date"].dt.date <= config.end_date].sort_values("date")
        bench = bench.drop_duplicates("date").set_index("date")
        if bench.empty:
            raise ValueError("基准指数日线为空")
        master_dates = [
            d.date()
            for d in bench.index
            if config.start_date <= d.date() <= config.end_date
        ]
        if len(master_dates) < 20:
            raise ValueError("回测区间内交易日不足 20 天")

        progress(f"加载 {len(codes)} 只个股日线…")
        prepared: dict[str, pd.DataFrame] = {}
        row_maps: dict[str, dict[date, pd.Series]] = {}
        failures: dict[str, str] = {}
        sources: set[str] = {bench_result.source}
        for idx, code in enumerate(codes, start=1):
            try:
                result = self.provider.get_stock_history(
                    code, start=warmup_start, end=config.end_date, adjust="qfq"
                )
                frame = result.frame.copy()
                frame["date"] = pd.to_datetime(frame["date"]).dt.normalize()
                frame = frame[frame["date"].dt.date <= config.end_date]
                if result.adjust != "qfq":
                    raise ValueError(f"复权方式为 {result.adjust}，回测仅接受前复权")
                if "amount_estimated" in frame.columns and bool(
                    frame["amount_estimated"].fillna(False).any()
                ):
                    raise ValueError("成交额由备用源估算，不能用于流动性回测")
                if len(frame) < 60:
                    raise ValueError(f"有效日线仅 {len(frame)} 条")
                frame = add_indicators(frame)
                frame = attach_relative_strength(frame, bench.reset_index())
                frame["limit_pct"] = price_limit_pct(code, names.get(code, ""))
                frame = frame.sort_values("date").reset_index(drop=True)
                prepared[code] = frame
                sources.add(result.source)
                row_maps[code] = {
                    pd.Timestamp(r["date"]).date(): r for _, r in frame.iterrows()
                }
            except Exception as exc:  # noqa: BLE001
                failures[code] = f"{type(exc).__name__}: {exc}"
            if idx % 10 == 0 or idx == len(codes):
                progress(f"日线进度 {idx}/{len(codes)}…")
        if not prepared:
            raise ValueError("所有股票日线加载失败，无法回测：" + "；".join(failures.values()))

        progress("逐日模拟交易…")
        result = self._simulate(
            master_dates=master_dates,
            bench=bench,
            rows=row_maps,
            names=names,
            config=config,
        )
        result.warnings.insert(0, "股票池为当前可获取的标的，存在幸存者偏差；结果不代表全市场历史表现。")
        result.warnings.insert(
            1,
            f"{len(failures)} 只股票因数据失败被排除；" + (
                "示例：" + "；".join(list(failures.items())[:3]) if failures else "无"
            ),
        )
        result.diagnostics["data_failures"] = failures
        result.diagnostics["benchmark_source"] = bench_result.source
        result.diagnostics["data_sources"] = sorted(sources)
        return result

    # ------------------------------------------------------------------
    # 模拟主循环
    # ------------------------------------------------------------------
    def _simulate(
        self,
        *,
        master_dates: list[date],
        bench: pd.DataFrame,
        rows: dict[str, dict[date, pd.Series]],
        names: dict[str, str],
        config: BacktestConfig,
    ) -> BacktestResult:
        cash = float(config.initial_cash)
        positions: dict[str, Position] = {}
        pending_sells: dict[str, str] = {}
        pending_buys: list[dict[str, Any]] = []
        closed: list[dict[str, Any]] = []
        equity_rows: list[dict[str, Any]] = []
        blocked = {"buys_limit_up": 0, "buys_suspended": 0, "sells_limit_down": 0, "sells_suspended": 0}
        rebalance_count = 0
        total_fees = 0.0
        last_equity = config.initial_cash

        for i, day in enumerate(master_dates):
            # 1) 先执行上一交易日收盘后决定的卖单，再执行买单。
            for code in list(pending_sells):
                reason = pending_sells.get(code, "")
                pos = positions.get(code)
                if pos is None:
                    continue
                bar = rows.get(code, {}).get(day)
                if bar is None or _num(bar.get("volume")) <= 0:
                    blocked["sells_suspended"] += 1
                    continue
                prev_close = self._previous_close(rows[code], day)
                limit_down = _limit_price(prev_close, code, pos.name, down=True)
                open_price = _num(bar.get("open"))
                if not math.isfinite(open_price) or open_price <= 0:
                    blocked["sells_suspended"] += 1
                    continue
                if open_price <= limit_down + 0.005:
                    blocked["sells_limit_down"] += 1
                    continue
                price = open_price * (1 - config.slippage_bps / 10_000)
                shares = pos.shares
                commission = max(config.min_commission, price * shares * config.commission_rate)
                transfer = price * shares * config.transfer_fee
                stamp = price * shares * config.stamp_tax_sell
                fees = commission + transfer + stamp
                proceeds = price * shares - fees
                cash += proceeds
                total_fees += fees
                pnl = proceeds - pos.entry_cost
                closed.append(
                    {
                        "code": code,
                        "name": pos.name,
                        "entry_date": pos.entry_date.isoformat(),
                        "exit_date": day.isoformat(),
                        "entry_price": round(pos.entry_price, 3),
                        "exit_price": round(price, 3),
                        "shares": shares,
                        "pnl": round(pnl, 2),
                        "return_pct": round(pnl / pos.entry_cost * 100, 2),
                        "holding_trading_days": i - pos.entry_idx,
                        "exit_reason": reason,
                        "fees": round(fees, 2),
                    }
                )
                del positions[code]
            pending_sells.clear()

            for idx, order in enumerate(pending_buys):
                code = order["code"]
                if code in positions:
                    continue
                if len(positions) >= config.max_positions:
                    break
                bar = rows.get(code, {}).get(day)
                if bar is None or _num(bar.get("volume")) <= 0:
                    blocked["buys_suspended"] += 1
                    continue
                prev_close = self._previous_close(rows[code], day)
                limit_up = _limit_price(prev_close, code, names.get(code, ""), down=False)
                open_price = _num(bar.get("open"))
                if not math.isfinite(open_price) or open_price <= 0:
                    blocked["buys_suspended"] += 1
                    continue
                if open_price >= limit_up - 0.005:
                    blocked["buys_limit_up"] += 1
                    continue
                price = open_price * (1 + config.slippage_bps / 10_000)
                remaining = max(1, len(pending_buys) - idx)
                budget = min(
                    cash * 0.98,
                    last_equity * config.max_position_pct,
                    cash / remaining,
                )
                shares = int(budget // (price * 100)) * 100
                if shares <= 0:
                    continue
                commission = max(config.min_commission, price * shares * config.commission_rate)
                transfer = price * shares * config.transfer_fee
                fees = commission + transfer
                cost = price * shares + fees
                if cost > cash:
                    shares -= 100
                    if shares <= 0:
                        continue
                    commission = max(config.min_commission, price * shares * config.commission_rate)
                    transfer = price * shares * config.transfer_fee
                    fees = commission + transfer
                    cost = price * shares + fees
                if cost > cash:
                    continue
                cash -= cost
                total_fees += fees
                stop = float(order.get("stop") or price * 0.92)
                target = float(order.get("target") or price * 1.08)
                positions[code] = Position(
                    code=code,
                    name=names.get(code, code),
                    shares=shares,
                    entry_price=price,
                    entry_date=day,
                    entry_idx=i,
                    entry_cost=cost,
                    stop=stop,
                    target=target,
                    last_close=_num(bar.get("close")),
                )
            pending_buys = []

            # 2) 按当日收盘价盯市；停牌沿用最后收盘价。
            for code, pos in positions.items():
                bar = rows.get(code, {}).get(day)
                if bar is not None:
                    close = _num(bar.get("close"))
                    if math.isfinite(close) and close > 0:
                        pos.last_close = close
            market_value = sum(p.shares * p.last_close for p in positions.values())
            last_equity = cash + market_value
            benchmark_close = float(bench.loc[pd.Timestamp(day), "close"])
            equity_rows.append(
                {
                    "date": day.isoformat(),
                    "equity": round(last_equity, 2),
                    "cash": round(cash, 2),
                    "market_value": round(market_value, 2),
                    "positions": len(positions),
                    "benchmark_close": benchmark_close,
                }
            )

            if i == len(master_dates) - 1:
                break

            # 3) 收盘后做卖出决策：止损/目标/持有期。
            for code, pos in list(positions.items()):
                bar = rows.get(code, {}).get(day)
                if bar is None:
                    continue
                close = _num(bar.get("close"))
                if not math.isfinite(close):
                    continue
                held = i - pos.entry_idx
                if config.use_stop_loss and close <= pos.stop:
                    pending_sells[code] = f"收盘跌破止损 {pos.stop:.2f}"
                elif config.use_take_profit and close >= pos.target:
                    pending_sells[code] = f"收盘触及目标参考 {pos.target:.2f}"
                elif held + 1 >= config.holding_days:
                    # 买入日记为第 1 个持有日；收盘决策、下一交易日开盘卖出，
                    # 因此持有 1 日 = 买入次日开盘卖出，满足 T+1。
                    pending_sells[code] = f"持有满 {config.holding_days} 个交易日"

            # 4) 调仓日选择下一交易日开盘买入的标的。
            if i % max(1, config.rebalance_days) == 0:
                free_slots = config.max_positions - len(positions) + len(pending_sells)
                if free_slots > 0:
                    rebalance_count += 1
                    selected = self._select(
                        day=day,
                        rows=rows,
                        positions=set(positions),
                        limit=free_slots,
                        config=config,
                    )
                    pending_buys = selected

        equity = pd.DataFrame(equity_rows)
        if not equity.empty and "benchmark_close" in equity.columns:
            base = float(equity["benchmark_close"].iloc[0])
            equity["benchmark_equity"] = (
                equity["benchmark_close"] / base * config.initial_cash if base else np.nan
            ).round(2)
        trades = pd.DataFrame(closed)
        metrics = self._metrics(equity, trades, bench, master_dates, config)
        metrics.update(
            {
                "blocked_buys_limit_up": blocked["buys_limit_up"],
                "blocked_buys_suspended": blocked["buys_suspended"],
                "blocked_sells_limit_down": blocked["sells_limit_down"],
                "blocked_sells_suspended": blocked["sells_suspended"],
                "rebalance_count": rebalance_count,
                "total_fees": round(total_fees, 2),
                "open_positions": len(positions),
            }
        )
        warnings = [
            "回测使用前复权日线；复权因子包含回测结束后的信息，价格路径与当时实际报价可能存在差异。",
            "涨跌停按板块和当前名称近似，无法还原历史 ST 状态；开盘封板一律视为无法成交，偏保守。",
            "停牌或缺失日线时不能交易，持仓顺延；未模拟集合竞价和盘口冲击成本。",
            "止损/目标均在收盘确认、下一交易日开盘执行，不是盘中实时止损。",
            "最大回撤按每日收盘权益计算，不包含盘中最大浮亏。",
            "未计分红税、融资融券、申购赎回等；滑点仅按固定 bps 估算。",
        ]
        limitations = [
            "幸存者偏差：股票池来自当前仍可交易的标的，已退市/长期停牌股票未纳入。",
            "日线级回测无法反映盘中流动性、排队和实际成交概率。",
            "策略未使用公告、财报、行业轮动、龙虎榜等场外信息。",
            "ST、科创板/创业板特殊规则、上市首日无涨跌幅限制未完整还原。",
        ]
        implemented = [
            "收盘信号 → 下一交易日开盘成交，杜绝同 bar 未来函数",
            "T+1：没有当日买入当日卖出的路径",
            "佣金最低 5 元、卖出印花税 0.05%、过户费、双边滑点",
            "涨停开盘拒绝买入；跌停开盘拒绝卖出；停牌缺失日线不成交",
            "固定持有期、收盘止损、收盘目标、按 100 股整手交易",
        ]
        return BacktestResult(
            metrics=metrics,
            equity_curve=equity,
            trades=trades,
            config={
                "start_date": config.start_date.isoformat(),
                "end_date": config.end_date.isoformat(),
                "initial_cash": config.initial_cash,
                "max_positions": config.max_positions,
                "holding_days": config.holding_days,
                "rebalance_days": config.rebalance_days,
                "min_score": config.min_score,
                "costs": {
                    "commission_rate": config.commission_rate,
                    "min_commission": config.min_commission,
                    "stamp_tax_sell": config.stamp_tax_sell,
                    "transfer_fee": config.transfer_fee,
                    "slippage_bps": config.slippage_bps,
                },
            },
            warnings=warnings,
            limitations=limitations,
            implemented_rules=implemented,
            diagnostics={"blocked": blocked},
        )

    def _select(
        self,
        *,
        day: date,
        rows: dict[str, dict[date, pd.Series]],
        positions: set[str],
        limit: int,
        config: BacktestConfig,
    ) -> list[dict[str, Any]]:
        candidates: list[dict[str, Any]] = []
        for code, mapping in rows.items():
            if code in positions:
                continue
            row = mapping.get(day)
            if row is None:
                continue
            close = _num(row.get("close"))
            amount = _num(row.get("amount_ma20"))
            if not math.isfinite(close) or close < config.min_price or close > config.max_price:
                continue
            if not math.isfinite(amount) or amount < config.min_avg_amount:
                continue
            if _num(row.get("volume")) <= 0:
                continue
            atr_pct = _num(row.get("atr_pct"))
            if math.isfinite(atr_pct) and atr_pct > 10:
                continue
            scored = score_stock(row, mode="postmarket", settings=self.settings)
            if scored.total < config.min_score:
                continue
            try:
                risk = build_risk_plan(
                    row, data_date=day, mode="backtest", score=scored.total, settings=self.settings
                )
            except ValueError:
                continue
            candidates.append(
                {
                    "code": code,
                    "score": scored.total,
                    "stop": risk.get("stop_loss"),
                    "target": risk.get("target"),
                }
            )
        candidates.sort(key=lambda x: (-x["score"], x["code"]))
        return candidates[:limit]

    @staticmethod
    def _previous_close(mapping: dict[date, pd.Series], day: date) -> float:
        earlier = [d for d in mapping if d < day]
        if not earlier:
            return float("nan")
        return _num(mapping[max(earlier)].get("close"))

    def _metrics(
        self,
        equity: pd.DataFrame,
        trades: pd.DataFrame,
        bench: pd.DataFrame,
        dates: list[date],
        config: BacktestConfig,
    ) -> dict[str, Any]:
        if equity.empty:
            return {}
        eq = equity.copy()
        eq["date"] = pd.to_datetime(eq["date"])
        start_equity = config.initial_cash
        end_equity = float(eq["equity"].iloc[-1])
        total_return = end_equity / start_equity - 1
        n = len(eq)
        annualized = (1 + total_return) ** (252 / max(1, n)) - 1 if total_return > -1 else -1
        eq["peak"] = eq["equity"].cummax()
        eq["drawdown"] = eq["equity"] / eq["peak"] - 1
        max_dd = float(eq["drawdown"].min())
        daily_ret = eq["equity"].pct_change().dropna()
        vol = float(daily_ret.std(ddof=0) * math.sqrt(252)) if len(daily_ret) > 1 else 0.0
        sharpe = (
            float(daily_ret.mean() / daily_ret.std(ddof=0) * math.sqrt(252))
            if len(daily_ret) > 1 and daily_ret.std(ddof=0) > 0
            else 0.0
        )

        bench_dates = [pd.Timestamp(d) for d in dates if pd.Timestamp(d) in bench.index]
        if bench_dates:
            bench_slice = bench.loc[bench_dates]
            b0 = float(bench_slice["close"].iloc[0])
            b1 = float(bench_slice["close"].iloc[-1])
            benchmark_return = b1 / b0 - 1
        else:
            benchmark_return = float("nan")

        closed = trades
        if closed is not None and not closed.empty:
            wins = closed[closed["pnl"] > 0]
            losses = closed[closed["pnl"] < 0]
            win_rate = len(wins) / len(closed)
            profit_factor = (
                float(wins["pnl"].sum() / abs(losses["pnl"].sum()))
                if not losses.empty and losses["pnl"].sum() != 0
                else None
            )
            trade_count = len(closed)
            avg_holding = float(closed["holding_trading_days"].mean())
        else:
            win_rate = None
            profit_factor = None
            trade_count = 0
            avg_holding = None

        return {
            "initial_cash": config.initial_cash,
            "final_equity": round(end_equity, 2),
            "total_return": round(total_return * 100, 2),
            "annualized_return": round(annualized * 100, 2),
            "max_drawdown": round(max_dd * 100, 2),
            "volatility_annualized": round(vol * 100, 2),
            "sharpe": round(sharpe, 2),
            "win_rate": round(win_rate * 100, 2) if win_rate is not None else None,
            "trade_count": trade_count,
            "avg_holding_trading_days": round(avg_holding, 2) if avg_holding is not None else None,
            "profit_factor": round(profit_factor, 2) if profit_factor is not None else None,
            "benchmark_return": round(benchmark_return * 100, 2)
            if math.isfinite(benchmark_return)
            else None,
            "excess_return": round((total_return - benchmark_return) * 100, 2)
            if math.isfinite(benchmark_return)
            else None,
            "trading_days": n,
        }


def _num(value: Any) -> float:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return float("nan")
    return f if math.isfinite(f) else float("nan")


def _limit_pct(code: str, name: str = "") -> float:
    return price_limit_pct(code, name)


def _limit_price(prev_close: float, code: str, name: str, down: bool) -> float:
    if not math.isfinite(prev_close) or prev_close <= 0:
        return float("-inf") if down else float("inf")
    return limit_price(prev_close, code, name, down=down)


def default_backtest_window(settings: Settings | None = None) -> tuple[date, date]:
    settings = settings or get_settings()
    end, _ = latest_completed_trade_day(settings=settings)
    return end - timedelta(days=365), end
