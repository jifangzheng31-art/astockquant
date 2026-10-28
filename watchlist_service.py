"""自选股：持久化列表 + 最新行情 + 最近一次盘前/盘后信号。"""
from __future__ import annotations

from typing import Any

from config import Settings, get_settings
from data_provider import DataProviderError, MarketDataProvider, normalize_code
from storage import load_result, load_watchlist, save_watchlist


def add_to_watchlist(
    code: str,
    provider: MarketDataProvider | None = None,
    settings: Settings | None = None,
) -> dict[str, str]:
    settings = settings or get_settings()
    provider = provider or MarketDataProvider(settings)
    code = normalize_code(code)
    items = load_watchlist(settings)
    if any(x["code"] == code for x in items):
        existing = next(x for x in items if x["code"] == code)
        return existing
    try:
        quote = provider.get_stock_quote(code)
        name = str(quote.get("name", "")).strip()
    except DataProviderError:
        name = ""
    item = {"code": code, "name": name}
    items.append(item)
    save_watchlist(items, settings)
    return item


def remove_from_watchlist(code: str, settings: Settings | None = None) -> None:
    settings = settings or get_settings()
    code = normalize_code(code)
    items = [x for x in load_watchlist(settings) if x["code"] != code]
    save_watchlist(items, settings)


def latest_signals(settings: Settings | None = None) -> dict[str, list[dict[str, Any]]]:
    settings = settings or get_settings()
    result: dict[str, list[dict[str, Any]]] = {}
    for mode in ("premarket", "postmarket"):
        payload = load_result(mode, settings=settings)
        if not payload:
            continue
        for candidate in payload.get("candidates", []):
            code = str(candidate.get("code", ""))
            result.setdefault(code, []).append(
                {
                    "mode": mode,
                    "mode_label": payload.get("mode_label", mode),
                    "score": candidate.get("score"),
                    "rank": candidate.get("rank"),
                    "signal_date": payload.get("signal_date"),
                    "target_date": payload.get("target_date"),
                    "setup": candidate.get("setup", []),
                    "reasons": candidate.get("reasons", [])[:2],
                }
            )
    return result


def watchlist_rows(
    provider: MarketDataProvider | None = None,
    settings: Settings | None = None,
) -> list[dict[str, Any]]:
    settings = settings or get_settings()
    provider = provider or MarketDataProvider(settings)
    items = load_watchlist(settings)
    signals = latest_signals(settings)
    rows: list[dict[str, Any]] = []
    for item in items:
        code = item["code"]
        row: dict[str, Any] = {
            "code": code,
            "name": item.get("name") or code,
            "price": None,
            "pct_chg": None,
            "quote_date": None,
            "quote_time": None,
            "source": None,
            "error": None,
            "signals": signals.get(code, []),
            "signal_text": _signal_text(signals.get(code, [])),
        }
        try:
            quote = provider.get_stock_quote(code)
            row.update(
                {
                    "name": quote.get("name") or row["name"],
                    "price": quote.get("price"),
                    "pct_chg": quote.get("pct_chg"),
                    "quote_date": quote.get("quote_date"),
                    "quote_time": quote.get("quote_time"),
                    "source": quote.get("source"),
                }
            )
        except Exception as exc:  # noqa: BLE001
            row["error"] = f"{type(exc).__name__}: {exc}"
        rows.append(row)
    return rows


def _signal_text(items: list[dict[str, Any]]) -> str:
    if not items:
        return "未进入最近候选"
    labels = []
    for item in items:
        label = f"{item['mode_label']} #{item.get('rank')} / {item.get('score')}分"
        labels.append(label)
    return "；".join(labels)
