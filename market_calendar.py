"""A 股交易日历与任务日期推导。

优先使用 AKShare 的新浪交易日历；失败时使用缓存。若两者都不可用，
降级为“工作日近似”并明确标记 is_reliable=False，绝不静默把周末/节假日
当成交易日。所有时间均使用 Asia/Shanghai。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Iterable

from config import Settings, get_settings

MARKET_CLOSE_SETTLED = time(15, 10)
MARKET_OPEN = time(9, 30)
PREMARKET_DEADLINE = time(9, 0)


@dataclass(frozen=True)
class CalendarInfo:
    dates: tuple[date, ...]
    source: str
    fetched_at: str
    reliable: bool
    warning: str | None = None

    def contains(self, day: date) -> bool:
        return day in self._date_set

    @property
    def _date_set(self) -> set[date]:
        # frozen dataclass 无法缓存 property，直接构造集合，日历规模很小。
        return set(self.dates)


def _read_cache(settings: Settings) -> CalendarInfo | None:
    path = settings.calendar_path
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        dates = tuple(date.fromisoformat(x) for x in raw["dates"])
        if not dates:
            return None
        return CalendarInfo(
            dates=dates,
            source=str(raw.get("source", "cache")),
            fetched_at=str(raw.get("fetched_at", "")),
            reliable=bool(raw.get("reliable", True)),
            warning=raw.get("warning"),
        )
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _write_cache(info: CalendarInfo, settings: Settings) -> None:
    settings.ensure_dirs()
    payload = {
        "dates": [d.isoformat() for d in info.dates],
        "source": info.source,
        "fetched_at": info.fetched_at,
        "reliable": info.reliable,
        "warning": info.warning,
    }
    tmp = settings.calendar_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(settings.calendar_path)


def _fetch_akshare_calendar() -> tuple[date, ...]:
    import akshare as ak

    df = ak.tool_trade_date_hist_sina()
    if df is None or df.empty:
        raise RuntimeError("AKShare 交易日历返回空数据")
    column = "trade_date" if "trade_date" in df.columns else df.columns[0]
    values: Iterable[object] = df[column].tolist()
    parsed_set: set[date] = set()
    for value in values:
        if value is None:
            continue
        if isinstance(value, datetime):
            parsed_set.add(value.date())
        elif isinstance(value, date):
            parsed_set.add(value)
        else:
            parsed_set.add(date.fromisoformat(str(value)[:10]))
    parsed = sorted(parsed_set)
    if len(parsed) < 100:
        raise RuntimeError(f"AKShare 交易日历异常：仅 {len(parsed)} 条")
    return tuple(parsed)


def load_calendar(force: bool = False, settings: Settings | None = None) -> CalendarInfo:
    settings = settings or get_settings()
    cached = _read_cache(settings)
    now = datetime.now(settings.timezone)
    if cached and not force:
        fetched: datetime | None = None
        try:
            fetched = datetime.fromisoformat(cached.fetched_at)
            if fetched.tzinfo is None:
                fetched = fetched.replace(tzinfo=settings.timezone)
            fresh = (now - fetched).total_seconds() < settings.calendar_ttl_seconds
        except ValueError:
            fresh = False
        # 缓存覆盖到未来 15 天以上时可直接使用；降级日历只短暂使用，
        # 以便网络恢复后尽快切回真实交易日历。
        coverage_ok = bool(cached.dates) and cached.dates[-1] >= now.date() + timedelta(days=15)
        age_seconds = (now - fetched).total_seconds() if fetched is not None else float("inf")
        if fresh and coverage_ok and (cached.reliable or age_seconds < 300):
            return cached

    try:
        dates = _fetch_akshare_calendar()
        info = CalendarInfo(
            dates=dates,
            source="AKShare / 新浪交易日历",
            fetched_at=now.isoformat(timespec="seconds"),
            reliable=True,
        )
        _write_cache(info, settings)
        return info
    except Exception as exc:  # noqa: BLE001 - 需要把数据源错误反馈给界面
        warning = f"交易日历获取失败：{type(exc).__name__}: {exc}"
        if cached:
            return CalendarInfo(
                dates=cached.dates,
                source=f"{cached.source}（缓存）",
                fetched_at=cached.fetched_at,
                reliable=cached.reliable,
                warning=warning,
            )

        # 最后兜底：工作日近似，仅用于不中断页面；调用方必须展示不可靠警告。
        start = now.date() - timedelta(days=800)
        end = now.date() + timedelta(days=370)
        weekdays = []
        d = start
        while d <= end:
            if d.weekday() < 5:
                weekdays.append(d)
            d += timedelta(days=1)
        info = CalendarInfo(
            dates=tuple(weekdays),
            source="工作日近似（无真实节假日数据）",
            fetched_at=now.isoformat(timespec="seconds"),
            reliable=False,
            warning=warning + "；已降级为工作日近似，节假日可能判断错误。",
        )
        _write_cache(info, settings)
        return info


def calendar_date_set(force: bool = False) -> tuple[set[date], CalendarInfo]:
    info = load_calendar(force=force)
    return set(info.dates), info


def is_trade_day(day: date, force: bool = False) -> tuple[bool, CalendarInfo]:
    dates, info = calendar_date_set(force=force)
    return day in dates, info


def previous_trade_day(day: date, force: bool = False) -> date:
    dates, _ = calendar_date_set(force=force)
    earlier = [d for d in dates if d < day]
    if not earlier:
        raise RuntimeError(f"交易日历中没有 {day} 之前的交易日")
    return max(earlier)


def next_trade_day(day: date, force: bool = False) -> date:
    dates, _ = calendar_date_set(force=force)
    later = [d for d in dates if d > day]
    if not later:
        raise RuntimeError(f"交易日历中没有 {day} 之后的交易日")
    return min(later)


def latest_completed_trade_day(
    now: datetime | None = None,
    settings: Settings | None = None,
    force: bool = False,
) -> tuple[date, CalendarInfo]:
    """返回此刻已经收盘并完成数据结算的最近交易日。"""
    settings = settings or get_settings()
    now = (now or datetime.now(settings.timezone)).astimezone(settings.timezone)
    dates, info = calendar_date_set(force=force)
    today = now.date()
    if today in dates and now.time() >= MARKET_CLOSE_SETTLED:
        return today, info
    earlier = [d for d in dates if d < today]
    if not earlier:
        raise RuntimeError("交易日历不足以推导最近已完成交易日")
    return max(earlier), info


def target_session_for_premarket(
    now: datetime | None = None,
    settings: Settings | None = None,
) -> tuple[date, CalendarInfo]:
    """盘前任务服务的目标交易日。

    09:30 前运行时目标就是今天；收盘后手工重跑时，目标是下一个交易日。
    """
    settings = settings or get_settings()
    now = (now or datetime.now(settings.timezone)).astimezone(settings.timezone)
    dates, info = calendar_date_set()
    today = now.date()
    if today in dates and now.time() < MARKET_OPEN:
        return today, info
    later = sorted(d for d in dates if d > today)
    if later:
        return later[0], info
    raise RuntimeError("交易日历不足以推导下一交易日")


def next_session_after_signal(signal_day: date) -> tuple[date, CalendarInfo]:
    info = load_calendar()
    return next_trade_day(signal_day), info


def session_label(d: date) -> str:
    return d.strftime("%Y-%m-%d")


def is_premarket_window(now: datetime | None = None, settings: Settings | None = None) -> bool:
    settings = settings or get_settings()
    now = (now or datetime.now(settings.timezone)).astimezone(settings.timezone)
    return time(0, 0) <= now.time() < PREMARKET_DEADLINE
