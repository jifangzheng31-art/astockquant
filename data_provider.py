"""真实行情数据访问层。

数据源策略（任何一层失败都会留下明确痕迹，绝不生成模拟行情）：

* 个股/指数日线：AKShare（东方财富）→ 东方财富直连 → 腾讯前复权 → 新浪
* 指数快照：新浪 hq → 腾讯 → AKShare
* 全市场快照：新浪 Market_Center → AKShare（东方财富）
* 交易日历：AKShare / 新浪（见 market_calendar.py）

前复权日线会随除权除息变化，缓存默认只短期复用。所有返回对象都带有
source / fetched_at / data_date / stale 元数据，供界面和结果文件展示。
"""
from __future__ import annotations

import contextlib
import io
import json
import math
import re
import socket
import threading
import time
import zlib
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlencode

import numpy as np
import pandas as pd
import requests

from config import Settings, get_settings
from market_calendar import load_calendar
from storage import atomic_write_json, cache_age_seconds, load_history_cache, save_history_cache

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

INDEX_DEFS: dict[str, dict[str, str]] = {
    "sh000001": {"name": "上证指数", "sina": "sh000001", "secid": "1.000001", "ak": "000001"},
    "sz399001": {"name": "深证成指", "sina": "sz399001", "secid": "0.399001", "ak": "399001"},
    "sz399006": {"name": "创业板指", "sina": "sz399006", "secid": "0.399006", "ak": "399006"},
    "sh000300": {"name": "沪深300", "sina": "sh000300", "secid": "1.000300", "ak": "000300"},
    "sh000688": {"name": "科创50", "sina": "sh000688", "secid": "1.000688", "ak": "000688"},
}
DEFAULT_INDICES = ["sh000001", "sz399001", "sz399006", "sh000300"]
BENCHMARK_INDEX = "sh000300"

HISTORY_COLUMNS = ["date", "open", "high", "low", "close", "volume", "amount", "pct_chg"]

# Baostock 使用全局 socket，必须串行访问。登录一次后复用连接。
_BAOSTOCK_LOCK = threading.Lock()
_BAOSTOCK_STATE = {"logged_in": False, "guard_installed": False}


def _install_baostock_guard() -> None:
    """修补 Baostock 0.9.4 的 recv 死循环。

    上游 `send_msg` 在服务端正常关闭连接（recv 返回 b""）时不会退出
    `while True`，也没有总超时，批量拉取上百只股票时会永久挂起。
    这里替换为带空包检测、socket 超时和总截止时间的等价实现。
    """
    if _BAOSTOCK_STATE["guard_installed"]:
        return
    import baostock.common.contants as cons
    import baostock.common.context as bs_context
    import baostock.util.socketutil as socketutil

    def safe_send_msg(msg: str):
        try:
            default_socket = getattr(bs_context, "default_socket", None)
            if default_socket is None:
                return None
            default_socket.send(bytes(msg + "\n", encoding="utf-8"))
            receive = b""
            deadline = time.monotonic() + 20
            while True:
                if time.monotonic() > deadline:
                    raise TimeoutError("Baostock 响应总时长超过 20 秒")
                recv = default_socket.recv(8192)
                if not recv:
                    raise ConnectionError("Baostock 服务端关闭了连接")
                receive += recv
                if receive[-13:] == b"<![CDATA[]]>\n":
                    break
            head_bytes = receive[0 : cons.MESSAGE_HEADER_LENGTH]
            head_str = bytes.decode(head_bytes)
            head_arr = head_str.split(cons.MESSAGE_SPLIT)
            if head_arr[1] in cons.COMPRESSED_MESSAGE_TYPE_TUPLE:
                head_inner_length = int(head_arr[2])
                body_str = bytes.decode(
                    zlib.decompress(
                        receive[
                            cons.MESSAGE_HEADER_LENGTH : cons.MESSAGE_HEADER_LENGTH
                            + head_inner_length
                        ]
                    )
                )
                return head_str + body_str
            return bytes.decode(receive)
        except Exception as exc:  # noqa: BLE001 - 交给上层重试/降级
            print(f"Baostock socket 错误：{exc}")
            return None

    socketutil.send_msg = safe_send_msg
    _BAOSTOCK_STATE["guard_installed"] = True


class DataProviderError(RuntimeError):
    """行情不可用。调用方应展示错误，不得用模拟数据代替。"""


class DataSourceError(DataProviderError):
    def __init__(self, source: str, operation: str, detail: str):
        self.source = source
        self.operation = operation
        self.detail = detail
        super().__init__(f"[{source}] {operation}失败：{detail}")


@dataclass
class HistoryResult:
    code: str
    frame: pd.DataFrame
    source: str
    fetched_at: str
    adjust: str
    cached: bool = False
    warnings: list[str] = field(default_factory=list)

    @property
    def latest_date(self) -> date | None:
        if self.frame.empty:
            return None
        return pd.Timestamp(self.frame["date"].iloc[-1]).date()


@dataclass
class SnapshotResult:
    frame: pd.DataFrame
    quote_date: date | None
    quote_time: str
    source: str
    fetched_at: str
    cached: bool = False
    stale: bool = False
    warnings: list[str] = field(default_factory=list)


@dataclass
class SourceStatus:
    name: str
    ok: bool
    latency_ms: int
    message: str
    checked_at: str
    data_date: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "ok": self.ok,
            "latency_ms": self.latency_ms,
            "message": self.message,
            "checked_at": self.checked_at,
            "data_date": self.data_date,
        }


def normalize_code(code: str) -> str:
    raw = str(code).strip().lower()
    raw = re.sub(r"^(sh|sz|bj)", "", raw)
    raw = re.sub(r"\D", "", raw)
    if len(raw) != 6:
        raise ValueError(f"股票代码应为 6 位数字，收到：{code!r}")
    return raw


def exchange_prefix(code: str) -> str:
    code = normalize_code(code)
    if code.startswith(("6", "9")):
        return "sh"
    if code.startswith(("0", "2", "3")):
        return "sz"
    if code.startswith(("4", "8")):
        return "bj"
    return "sh"


def sina_symbol(code: str) -> str:
    return f"{exchange_prefix(code)}{normalize_code(code)}"


def eastmoney_secid(code: str) -> str:
    code = normalize_code(code)
    market = "1" if code.startswith(("6", "9")) else "0"
    return f"{market}.{code}"


def _clean_float(value: Any) -> float:
    try:
        if value is None or (isinstance(value, str) and not value.strip()):
            return float("nan")
        return float(value)
    except (TypeError, ValueError):
        return float("nan")


def _now_iso(settings: Settings) -> str:
    return datetime.now(settings.timezone).isoformat(timespec="seconds")


class MarketDataProvider:
    def __init__(self, settings: Settings | None = None):
        self.settings = settings or get_settings()
        self.settings.ensure_dirs()
        self._local = threading.local()
        self._memory: dict[str, tuple[float, Any]] = {}
        self._memory_lock = threading.Lock()
        self._throttle_lock = threading.Lock()
        self._last_request = 0.0
        self._min_interval = 0.015
        # 数据源熔断：某源连续失败后短时间内不再逐只重试，避免 160 只股票
        # 每只都等待三次超时。健康检查仍会直接探测，不受熔断影响。
        self._circuit_lock = threading.Lock()
        self._circuit_failures: dict[str, int] = {}
        self._circuit_open_until: dict[str, float] = {}

    # ------------------------------------------------------------------
    # HTTP / 缓存工具
    # ------------------------------------------------------------------
    def _session(self) -> requests.Session:
        session = getattr(self._local, "session", None)
        if session is None:
            session = requests.Session()
            session.trust_env = self.settings.use_system_proxy
            session.headers.update(
                {
                    "User-Agent": USER_AGENT,
                    "Accept": "application/json,text/plain,*/*",
                    "Referer": "https://finance.sina.com.cn/",
                }
            )
            self._local.session = session
        return session

    def _throttle(self) -> None:
        with self._throttle_lock:
            wait = self._min_interval - (time.monotonic() - self._last_request)
            if wait > 0:
                time.sleep(wait)
            self._last_request = time.monotonic()

    def _get(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        encoding: str | None = None,
        timeout: int | None = None,
    ) -> requests.Response:
        last_exc: Exception | None = None
        for attempt in range(self.settings.request_retries + 1):
            self._throttle()
            try:
                resp = self._session().get(
                    url,
                    params=params,
                    headers=headers,
                    timeout=timeout or self.settings.request_timeout,
                )
                if resp.status_code >= 400:
                    raise requests.HTTPError(f"HTTP {resp.status_code}")
                if encoding:
                    resp.encoding = encoding
                return resp
            except Exception as exc:  # noqa: BLE001 - 统一重试后抛出数据源错误
                last_exc = exc
                if attempt < self.settings.request_retries:
                    time.sleep(0.35 * (2**attempt))
        raise DataSourceError("HTTP", url.split("?")[0], f"{type(last_exc).__name__}: {last_exc}")

    def _memo_get(self, key: str, ttl: int) -> Any | None:
        with self._memory_lock:
            item = self._memory.get(key)
            if item and item[0] > time.time():
                return item[1]
            if item:
                self._memory.pop(key, None)
        return None

    def _memo_set(self, key: str, value: Any, ttl: int) -> None:
        with self._memory_lock:
            self._memory[key] = (time.time() + ttl, value)

    def _source_available(self, source: str) -> bool:
        with self._circuit_lock:
            until = self._circuit_open_until.get(source, 0.0)
            if until <= time.time():
                self._circuit_open_until.pop(source, None)
                return True
            return False

    def _record_source_failure(self, source: str, threshold: int = 2, cooldown: int = 300) -> None:
        with self._circuit_lock:
            failures = self._circuit_failures.get(source, 0) + 1
            self._circuit_failures[source] = failures
            if failures >= threshold:
                self._circuit_open_until[source] = time.time() + cooldown

    def _record_source_success(self, source: str) -> None:
        with self._circuit_lock:
            self._circuit_failures.pop(source, None)
            self._circuit_open_until.pop(source, None)

    # ------------------------------------------------------------------
    # AKShare 懒加载
    # ------------------------------------------------------------------
    @staticmethod
    def _akshare():
        import akshare as ak  # 延迟导入，避免 Streamlit 启动阶段卡住

        return ak

    # ------------------------------------------------------------------
    # 指数行情
    # ------------------------------------------------------------------
    def get_index_quotes(
        self, codes: Iterable[str] | None = None, force: bool = False
    ) -> list[dict[str, Any]]:
        codes = [c for c in (codes or DEFAULT_INDICES)]
        key = "index_quotes:" + ",".join(codes)
        if not force:
            cached = self._memo_get(key, self.settings.index_ttl_seconds)
            if cached is not None:
                return cached

        errors: list[str] = []
        quotes: list[dict[str, Any]] = []
        try:
            quotes = self._sina_index_quotes(codes)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"新浪指数行情失败：{exc}")
            try:
                quotes = self._tencent_quotes(codes)
            except Exception as exc2:  # noqa: BLE001
                errors.append(f"腾讯指数行情失败：{exc2}")
                try:
                    quotes = self._akshare_index_spot(codes)
                except Exception as exc3:  # noqa: BLE001
                    errors.append(f"AKShare 指数行情失败：{exc3}")

        if quotes:
            self._memo_set(key, quotes, self.settings.index_ttl_seconds)
            atomic_write_json(
                self.settings.cache_dir / "last_index_quotes.json",
                {"fetched_at": _now_iso(self.settings), "quotes": quotes},
            )
            return quotes

        cached_file = self.settings.cache_dir / "last_index_quotes.json"
        if cached_file.exists():
            raw = json.loads(cached_file.read_text(encoding="utf-8"))
            cached_quotes = raw.get("quotes", [])
            for item in cached_quotes:
                item["stale"] = True
                item["warning"] = "实时指数接口不可用，显示上次成功获取的数据"
            self._memo_set(key, cached_quotes, 30)
            return cached_quotes
        raise DataProviderError("；".join(errors) or "指数行情不可用")

    def _sina_index_quotes(self, codes: Iterable[str]) -> list[dict[str, Any]]:
        symbols = ",".join(INDEX_DEFS.get(c, {}).get("sina", c) for c in codes)
        resp = self._get(
            "https://hq.sinajs.cn/",
            params={"list": symbols},
            headers={"Referer": "https://finance.sina.com.cn/"},
            encoding="gbk",
        )
        pattern = re.compile(r'var hq_str_(\w+)="([^"]*)"')
        result: list[dict[str, Any]] = []
        for match in pattern.finditer(resp.text):
            symbol, payload = match.group(1), match.group(2)
            fields = payload.split(",")
            if len(fields) < 32:
                continue
            prev_close = _clean_float(fields[2])
            price = _clean_float(fields[3])
            if not math.isfinite(price) or price <= 0:
                price = prev_close
            change = price - prev_close if math.isfinite(prev_close) else float("nan")
            pct = change / prev_close * 100 if prev_close else float("nan")
            result.append(
                {
                    "code": symbol,
                    "name": fields[0] or INDEX_DEFS.get(symbol, {}).get("name", symbol),
                    "price": price,
                    "prev_close": prev_close,
                    "open": _clean_float(fields[1]),
                    "high": _clean_float(fields[4]),
                    "low": _clean_float(fields[5]),
                    "volume": _clean_float(fields[8]),
                    "amount": _clean_float(fields[9]),
                    "change": change,
                    "pct_chg": pct,
                    "quote_date": fields[30],
                    "quote_time": fields[31],
                    "source": "新浪财经",
                    "stale": False,
                }
            )
        if not result:
            raise DataProviderError("新浪返回内容无法解析")
        return result

    def _akshare_index_spot(self, codes: Iterable[str]) -> list[dict[str, Any]]:
        ak = self._akshare()
        df = ak.stock_zh_index_spot_em(symbol="沪深重要指数")
        if df is None or df.empty:
            raise DataProviderError("AKShare 指数现货为空")
        code_col = "代码" if "代码" in df.columns else df.columns[1]
        df = df.copy()
        df["_code"] = df[code_col].astype(str).str.zfill(6)
        result = []
        now = datetime.now(self.settings.timezone)
        for code in codes:
            short = str(code)[-6:]
            row = df[df["_code"] == short]
            if row.empty:
                continue
            r = row.iloc[0]
            price = _clean_float(r.get("最新价"))
            prev = _clean_float(r.get("昨收"))
            pct = _clean_float(r.get("涨跌幅"))
            result.append(
                {
                    "code": code,
                    "name": str(r.get("名称", INDEX_DEFS.get(code, {}).get("name", code))),
                    "price": price,
                    "prev_close": prev,
                    "open": _clean_float(r.get("今开")),
                    "high": _clean_float(r.get("最高")),
                    "low": _clean_float(r.get("最低")),
                    "volume": _clean_float(r.get("成交量")),
                    "amount": _clean_float(r.get("成交额")),
                    "change": price - prev if math.isfinite(prev) else float("nan"),
                    "pct_chg": pct,
                    "quote_date": now.date().isoformat(),
                    "quote_time": now.strftime("%H:%M:%S"),
                    "source": "AKShare / 东方财富",
                    "stale": False,
                    "warning": "AKShare 指数快照未提供交易所行情日期，日期为获取日期",
                }
            )
        if not result:
            raise DataProviderError("AKShare 指数代码未匹配")
        return result

    def _tencent_quotes(self, codes: Iterable[str]) -> list[dict[str, Any]]:
        symbols = ",".join(INDEX_DEFS.get(c, {}).get("sina", c) for c in codes)
        resp = self._get("https://qt.gtimg.cn/", params={"q": symbols}, encoding="gbk")
        result: list[dict[str, Any]] = []
        for line in resp.text.splitlines():
            if "=" not in line:
                continue
            symbol, payload = line.split("=", 1)
            symbol = symbol.replace("v_", "").strip()
            fields = payload.strip().strip('";').split("~")
            if len(fields) < 35:
                continue
            price = _clean_float(fields[3])
            prev = _clean_float(fields[4])
            pct = _clean_float(fields[32])
            result.append(
                {
                    "code": symbol,
                    "name": fields[1],
                    "price": price,
                    "prev_close": prev,
                    "open": _clean_float(fields[5]),
                    "high": _clean_float(fields[33]),
                    "low": _clean_float(fields[34]),
                    "volume": _clean_float(fields[6]),
                    "amount": _clean_float(fields[37]) * 10_000 if len(fields) > 37 else float("nan"),
                    "change": _clean_float(fields[31]),
                    "pct_chg": pct,
                    "quote_date": _tencent_timestamp(fields[30])[0] if len(fields) > 30 else "",
                    "quote_time": _tencent_timestamp(fields[30])[1] if len(fields) > 30 else "",
                    "source": "腾讯行情",
                    "stale": False,
                }
            )
        if not result:
            raise DataProviderError("腾讯行情返回为空")
        return result

    def get_index_history(
        self,
        index_code: str = BENCHMARK_INDEX,
        start: date | str | None = None,
        end: date | str | None = None,
        force: bool = False,
    ) -> HistoryResult:
        meta = INDEX_DEFS.get(index_code, {"ak": normalize_code(index_code), "secid": eastmoney_secid(index_code)})
        end_date = _as_date(end) or datetime.now(self.settings.timezone).date()
        start_date = _as_date(start) or (end_date - timedelta(days=400))
        errors: list[str] = []

        cache_key = f"index_hist:{index_code}:{start_date}:{end_date}"
        if not force:
            memo = self._memo_get(cache_key, self.settings.history_ttl_seconds)
            if memo is not None:
                return memo

        try:
            frame = self._eastmoney_history_secid(meta["secid"], start_date, end_date, adjust="")
            frame = _normalise_history(frame, "date")
            if frame.empty:
                raise DataProviderError("东方财富指数日线为空")
            result = HistoryResult(
                code=index_code,
                frame=frame,
                source="东方财富（指数直连）",
                fetched_at=_now_iso(self.settings),
                adjust="不复权",
            )
            self._memo_set(cache_key, result, self.settings.history_ttl_seconds)
            return result
        except Exception as exc:  # noqa: BLE001
            errors.append(f"东方财富：{exc}")

        try:
            ak = self._akshare()
            df = ak.index_zh_a_hist(
                symbol=meta["ak"],
                period="daily",
                start_date=start_date.strftime("%Y%m%d"),
                end_date=end_date.strftime("%Y%m%d"),
            )
            frame = _normalise_history(_rename_akshare(df), "date")
            if frame.empty:
                raise DataProviderError("AKShare 指数日线为空")
            result = HistoryResult(
                code=index_code,
                frame=frame,
                source="AKShare / 东方财富（指数）",
                fetched_at=_now_iso(self.settings),
                adjust="不复权",
            )
            self._memo_set(cache_key, result, self.settings.history_ttl_seconds)
            return result
        except Exception as exc:  # noqa: BLE001
            errors.append(f"AKShare：{exc}")

        try:
            frame = self._baostock_index_history(index_code, start_date, end_date)
            frame = _normalise_history(frame, "date")
            if frame.empty:
                raise DataProviderError("Baostock 指数日线为空")
            result = HistoryResult(
                code=index_code,
                frame=frame,
                source="Baostock（指数，不复权）",
                fetched_at=_now_iso(self.settings),
                adjust="不复权",
            )
            self._memo_set(cache_key, result, self.settings.history_ttl_seconds)
            return result
        except Exception as exc:  # noqa: BLE001
            errors.append(f"Baostock：{exc}")

        try:
            frame = self._sina_index_history(index_code, start_date, end_date)
            frame = _normalise_history(frame, "date")
            if frame.empty:
                raise DataProviderError("新浪指数日线为空")
            result = HistoryResult(
                code=index_code,
                frame=frame,
                source="新浪财经（指数）",
                fetched_at=_now_iso(self.settings),
                adjust="不复权",
                warnings=["新浪指数日线不含成交额，仅用于相对强度计算"],
            )
            self._memo_set(cache_key, result, self.settings.history_ttl_seconds)
            return result
        except Exception as exc:  # noqa: BLE001
            errors.append(f"新浪：{exc}")
        raise DataProviderError(f"指数 {index_code} 日线获取失败；" + "；".join(errors))

    # ------------------------------------------------------------------
    # 个股日线
    # ------------------------------------------------------------------
    def get_stock_history(
        self,
        code: str,
        start: date | str | None = None,
        end: date | str | None = None,
        adjust: str = "qfq",
        force: bool = False,
        use_cache: bool = True,
    ) -> HistoryResult:
        code = normalize_code(code)
        end_date = _as_date(end) or datetime.now(self.settings.timezone).date()
        start_date = _as_date(start) or (end_date - timedelta(days=520))
        if start_date > end_date:
            raise ValueError("start 不能晚于 end")
        adjust = adjust or "qfq"
        cache_key = f"stock_hist:{code}:{start_date}:{end_date}:{adjust}"

        cached_result = None
        if use_cache and not force:
            memo = self._memo_get(cache_key, self.settings.history_ttl_seconds)
            if memo is not None:
                return memo
            cached_result = self._load_cached_history(code, start_date, end_date, adjust)

        if cached_result is not None:
            self._memo_set(cache_key, cached_result, self.settings.history_ttl_seconds)
            return cached_result

        errors: list[str] = []
        attempts = [
            ("AKShare / 东方财富", lambda: self._akshare_history(code, start_date, end_date, adjust)),
            ("东方财富直连", lambda: self._eastmoney_history_secid(eastmoney_secid(code), start_date, end_date, adjust)),
            ("Baostock（证券宝）", lambda: self._baostock_history(code, start_date, end_date, adjust)),
            ("腾讯前复权", lambda: self._tencent_history(code, start_date, end_date, adjust)),
            ("新浪日线（不复权兜底）", lambda: self._sina_history(code, start_date, end_date)),
        ]

        for source, func in attempts:
            if not self._source_available(source):
                errors.append(f"{source}：刚连续失败，已熔断跳过")
                continue
            try:
                frame, warnings, actual_adjust = func()
                if frame is None or frame.empty:
                    raise DataProviderError("返回空数据")
                frame = _normalise_history(frame, "date")
                if frame.empty or len(frame) < 5:
                    raise DataProviderError(f"有效日线仅 {len(frame)} 条")
                latest = pd.Timestamp(frame["date"].iloc[-1]).date()
                if latest > end_date + timedelta(days=1):
                    raise DataProviderError(f"数据日期 {latest} 超出请求截止日 {end_date}")
                result = HistoryResult(
                    code=code,
                    frame=frame,
                    source=source,
                    fetched_at=_now_iso(self.settings),
                    adjust=actual_adjust,
                    cached=False,
                    warnings=warnings,
                )
                save_history_cache(
                    code,
                    frame,
                    {
                        "source": result.source,
                        "fetched_at": result.fetched_at,
                        "adjust": result.adjust,
                        "warnings": warnings,
                        "min_date": str(frame["date"].min().date()),
                        "max_date": str(latest),
                    },
                    self.settings,
                )
                self._record_source_success(source)
                self._memo_set(cache_key, result, self.settings.history_ttl_seconds)
                return result
            except Exception as exc:  # noqa: BLE001
                self._record_source_failure(source)
                errors.append(f"{source}：{type(exc).__name__}: {exc}")

        # 网络全部失败时，只有缓存日期足够新才返回缓存，并明确 stale。
        cached_result = self._load_cached_history(
            code, start_date, end_date, adjust, allow_stale=True
        )
        if cached_result is not None:
            cached_result.warnings.append(
                "实时数据源全部失败，当前显示本地缓存；请核对行情日期后使用"
            )
            cached_result.cached = True
            return cached_result
        raise DataProviderError(f"{code} 日线获取失败；" + "；".join(errors))

    def _load_cached_history(
        self,
        code: str,
        start: date,
        end: date,
        adjust: str,
        allow_stale: bool = False,
    ) -> HistoryResult | None:
        payload = load_history_cache(code, self.settings)
        if not payload:
            return None
        frame, meta = payload
        if str(meta.get("adjust", "")) != adjust:
            return None
        frame = _normalise_history(frame, "date")
        if frame.empty:
            return None
        frame = frame[(frame["date"].dt.date >= start) & (frame["date"].dt.date <= end)]
        if frame.empty or len(frame) < 5:
            return None
        min_date = pd.Timestamp(frame["date"].min()).date()
        max_date = pd.Timestamp(frame["date"].max()).date()
        # end 可能是周末/节假日/尚未收盘的当日，因此允许最近几个自然日的缺口；
        # 结果入库前，选股器仍会强制校验“最后一根日线 == 信号日”。
        covers = min_date <= start and max_date >= (end - timedelta(days=7))
        age = _age_from_iso(str(meta.get("fetched_at", "")), self.settings)
        if not allow_stale:
            # 已收盘的历史区间不会因时间流逝改变；盘前复用前一晚缓存可减少
            # 9:00 前的请求压力。当日/未来 end 仍要求短 TTL。
            today = datetime.now(self.settings.timezone).date()
            age_limit = max(self.settings.history_ttl_seconds, 36 * 3600) if end < today else self.settings.history_ttl_seconds
            if not covers or age > age_limit:
                return None
        fetched_at = str(meta.get("fetched_at", ""))
        warnings = list(meta.get("warnings") or [])
        return HistoryResult(
            code=code,
            frame=frame.reset_index(drop=True),
            source=str(meta.get("source", "本地缓存")),
            fetched_at=fetched_at,
            adjust=adjust,
            cached=True,
            warnings=warnings,
        )

    def _akshare_history(
        self, code: str, start: date, end: date, adjust: str
    ) -> tuple[pd.DataFrame, list[str], str]:
        ak = self._akshare()
        df = ak.stock_zh_a_hist(
            symbol=code,
            period="daily",
            start_date=start.strftime("%Y%m%d"),
            end_date=end.strftime("%Y%m%d"),
            adjust=adjust,
            timeout=self.settings.request_timeout,
        )
        if df is None or df.empty:
            raise DataProviderError("AKShare 个股日线为空")
        return _rename_akshare(df), [], adjust

    def _baostock_history(
        self, code: str, start: date, end: date, adjust: str
    ) -> tuple[pd.DataFrame, list[str], str]:
        adjust_flag = {"qfq": "2", "hfq": "1", "": "3"}.get(adjust, "2")
        bs_code = f"{exchange_prefix(code)}.{normalize_code(code)}"
        fields = (
            "date,code,open,high,low,close,preclose,volume,amount,adjustflag,"
            "turn,tradestatus,pctChg,isST"
        )
        frame = self._baostock_query(bs_code, fields, start, end, adjust_flag)
        warnings: list[str] = []
        if "tradestatus" in frame.columns and (frame["tradestatus"].astype(str) == "0").any():
            warnings.append("Baostock 日线包含停牌日（tradestatus=0）")
        return frame, warnings, adjust

    def _baostock_index_history(
        self, index_code: str, start: date, end: date
    ) -> pd.DataFrame:
        symbol = INDEX_DEFS.get(index_code, {}).get("sina", index_code)
        if "." in symbol:
            bs_code = symbol
        elif symbol[:2] in {"sh", "sz", "bj"}:
            bs_code = f"{symbol[:2]}.{symbol[2:]}"
        else:
            bs_code = f"sh.{normalize_code(symbol)}"
        fields = "date,code,open,high,low,close,preclose,volume,amount,pctChg"
        return self._baostock_query(bs_code, fields, start, end, "3")

    @staticmethod
    def _baostock_query(
        bs_code: str, fields: str, start: date, end: date, adjust_flag: str
    ) -> pd.DataFrame:
        import baostock as bs

        _install_baostock_guard()
        last_error: Exception | None = None
        for attempt in range(2):
            try:
                with _BAOSTOCK_LOCK:
                    with contextlib.redirect_stdout(io.StringIO()):
                        if not _BAOSTOCK_STATE["logged_in"]:
                            old_timeout = socket.getdefaulttimeout()
                            socket.setdefaulttimeout(15)
                            try:
                                login = bs.login()
                            finally:
                                socket.setdefaulttimeout(old_timeout)
                            if login.error_code != "0":
                                raise DataProviderError(f"Baostock 登录失败：{login.error_msg}")
                            _BAOSTOCK_STATE["logged_in"] = True
                        # Baostock 自身不给 recv 设超时，单个查询可能永久阻塞；
                        # 这里给全局 socket 设上限，超时后重连。
                        import baostock.common.context as bs_context

                        sock = getattr(bs_context, "default_socket", None)
                        if sock is not None:
                            sock.settimeout(15)
                        rs = bs.query_history_k_data_plus(
                            bs_code,
                            fields,
                            start_date=start.strftime("%Y-%m-%d"),
                            end_date=end.strftime("%Y-%m-%d"),
                            frequency="d",
                            adjustflag=adjust_flag,
                        )
                    if rs.error_code != "0":
                        raise DataProviderError(f"Baostock 查询失败：{rs.error_msg}")
                    rows = []
                    while rs.next():
                        rows.append(rs.get_row_data())
                if not rows:
                    raise DataProviderError("Baostock 返回空数据")
                return pd.DataFrame(rows, columns=rs.fields)
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                with _BAOSTOCK_LOCK:
                    _BAOSTOCK_STATE["logged_in"] = False
                    try:
                        import baostock.common.context as bs_context

                        sock = getattr(bs_context, "default_socket", None)
                        if sock is not None:
                            sock.close()
                        setattr(bs_context, "default_socket", None)
                    except Exception:  # noqa: BLE001
                        pass
                time.sleep(0.4 * (attempt + 1))
        raise DataProviderError(f"Baostock 日线失败：{last_error}")

    def _sina_index_history(self, index_code: str, start: date, end: date) -> pd.DataFrame:
        symbol = INDEX_DEFS.get(index_code, {}).get("sina", index_code)
        resp = self._get(
            "https://quotes.sina.cn/cn/api/json_v2.php/CN_MarketDataService.getKLineData",
            params={"symbol": symbol, "scale": "240", "ma": "no", "datalen": "800"},
            headers={"Referer": "https://finance.sina.com.cn/"},
        )
        rows = resp.json()
        if not rows:
            raise DataProviderError("新浪指数 K 线为空")
        frame = pd.DataFrame(rows)
        frame["date"] = pd.to_datetime(frame["day"], errors="coerce").dt.date
        frame = frame[(frame["date"] >= start) & (frame["date"] <= end)]
        return frame

    def _eastmoney_history_secid(
        self, secid: str, start: date, end: date, adjust: str
    ) -> pd.DataFrame:
        fqt = {"": 0, "qfq": 1, "hfq": 2}.get(adjust, 1)
        params = {
            "secid": secid,
            "fields1": "f1,f2,f3,f4,f5,f6",
            "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
            "klt": "101",
            "fqt": str(fqt),
            "beg": start.strftime("%Y%m%d"),
            "end": end.strftime("%Y%m%d"),
            "lmt": "1200",
        }
        resp = self._get("https://push2his.eastmoney.com/api/qt/stock/kline/get", params=params)
        payload = resp.json()
        data = payload.get("data") or {}
        klines = data.get("klines") or []
        if not klines:
            raise DataProviderError("东方财富 K 线为空")
        rows = []
        for line in klines:
            parts = str(line).split(",")
            if len(parts) < 11:
                continue
            rows.append(
                {
                    "date": parts[0],
                    "open": parts[1],
                    "close": parts[2],
                    "high": parts[3],
                    "low": parts[4],
                    "volume": parts[5],
                    "amount": parts[6],
                    "pct_chg": parts[8],
                    "turnover": parts[10],
                }
            )
        return pd.DataFrame(rows)

    def _tencent_history(
        self, code: str, start: date, end: date, adjust: str
    ) -> tuple[pd.DataFrame, list[str], str]:
        symbol = sina_symbol(code)
        count = max(120, min(1000, (end - start).days * 2 + 20))
        param = f"{symbol},day,{start.strftime('%Y-%m-%d')},{end.strftime('%Y-%m-%d')},{count},{adjust or 'qfq'}"
        resp = self._get(
            "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get",
            params={"param": param},
            headers={"Referer": "https://gu.qq.com/"},
        )
        payload = resp.json()
        node = ((payload.get("data") or {}).get(symbol) or {})
        rows = node.get("qfqday") or node.get("day") or []
        if not rows:
            raise DataProviderError("腾讯 K 线为空")
        records = []
        for row in rows:
            if len(row) < 6:
                continue
            close = _clean_float(row[2])
            volume = _clean_float(row[5])
            # 腾讯成交量单位为手；成交额字段不一定存在。
            # 若缺失则以 收盘价×成交量×100 估算，并显式标记 amount_estimated。
            amount = _clean_float(row[6]) if len(row) > 6 else float("nan")
            records.append(
                {
                    "date": row[0],
                    "open": row[1],
                    "close": row[2],
                    "high": row[3],
                    "low": row[4],
                    "volume": volume,
                    "amount": amount if math.isfinite(amount) else close * volume * 100,
                    "amount_estimated": not math.isfinite(amount),
                    "pct_chg": float("nan"),
                }
            )
        frame = pd.DataFrame(records)
        warnings = ["腾讯接口未提供成交额，已用 收盘价×成交量×100 估算"] if frame["amount_estimated"].any() else []
        return frame, warnings, adjust or "qfq"

    def _sina_history(
        self, code: str, start: date, end: date
    ) -> tuple[pd.DataFrame, list[str], str]:
        symbol = sina_symbol(code)
        resp = self._get(
            "https://quotes.sina.cn/cn/api/json_v2.php/CN_MarketDataService.getKLineData",
            params={"symbol": symbol, "scale": "240", "ma": "no", "datalen": "500"},
            headers={"Referer": "https://finance.sina.com.cn/"},
        )
        rows = resp.json()
        if not rows:
            raise DataProviderError("新浪 K 线为空")
        records = []
        for row in rows:
            volume = _clean_float(row.get("volume"))
            close = _clean_float(row.get("close"))
            records.append(
                {
                    "date": row.get("day"),
                    "open": row.get("open"),
                    "high": row.get("high"),
                    "low": row.get("low"),
                    "close": row.get("close"),
                    "volume": volume,
                    "amount": close * volume,
                    "amount_estimated": True,
                    "pct_chg": float("nan"),
                }
            )
        frame = pd.DataFrame(records)
        frame = frame[
            (pd.to_datetime(frame["date"]).dt.date >= start)
            & (pd.to_datetime(frame["date"]).dt.date <= end)
        ]
        return frame, ["新浪兜底日线为不复权数据，且成交额由收盘价×成交量估算"], "不复权"

    # ------------------------------------------------------------------
    # 个股快照
    # ------------------------------------------------------------------
    def get_stock_quote(self, code: str, force: bool = False) -> dict[str, Any]:
        code = normalize_code(code)
        key = f"stock_quote:{code}"
        if not force:
            cached = self._memo_get(key, 20)
            if cached is not None:
                return cached
        symbol = sina_symbol(code)
        try:
            resp = self._get(
                "https://hq.sinajs.cn/",
                params={"list": symbol},
                headers={"Referer": "https://finance.sina.com.cn/"},
                encoding="gbk",
            )
            match = re.search(r'var hq_str_\w+="([^"]*)"', resp.text)
            if not match or not match.group(1):
                raise DataProviderError("新浪个股行情为空")
            fields = match.group(1).split(",")
            if len(fields) < 32:
                raise DataProviderError("新浪个股行情字段不完整")
            price = _clean_float(fields[3])
            prev = _clean_float(fields[2])
            if not math.isfinite(price) or price <= 0:
                price = prev
            quote = {
                "code": code,
                "name": fields[0],
                "price": price,
                "prev_close": prev,
                "open": _clean_float(fields[1]),
                "high": _clean_float(fields[4]),
                "low": _clean_float(fields[5]),
                "volume": _clean_float(fields[8]),
                "amount": _clean_float(fields[9]),
                "change": price - prev if math.isfinite(prev) else float("nan"),
                "pct_chg": (price / prev - 1) * 100 if prev else float("nan"),
                "quote_date": fields[30],
                "quote_time": fields[31],
                "source": "新浪财经",
                "stale": False,
            }
        except Exception as exc:  # noqa: BLE001
            try:
                rows = self._tencent_quotes([symbol])
                row = rows[0]
                quote = {
                    "code": code,
                    "name": row["name"],
                    "price": row["price"],
                    "prev_close": row["prev_close"],
                    "open": row["open"],
                    "high": row["high"],
                    "low": row["low"],
                    "volume": row["volume"],
                    "amount": row["amount"],
                    "change": row["change"],
                    "pct_chg": row["pct_chg"],
                    "quote_date": row["quote_date"],
                    "quote_time": row["quote_time"],
                    "source": "腾讯行情",
                    "stale": False,
                }
            except Exception as exc2:  # noqa: BLE001
                raise DataProviderError(f"{code} 行情获取失败：新浪 {exc}；腾讯 {exc2}") from exc2
        self._memo_set(key, quote, 20)
        return quote

    # ------------------------------------------------------------------
    # 全市场快照
    # ------------------------------------------------------------------
    def get_spot_snapshot(
        self,
        max_rows: int | None = None,
        force: bool = False,
        allow_disk_cache: bool = True,
    ) -> SnapshotResult:
        key = f"snapshot:{max_rows or 'all'}"
        if not force:
            cached = self._memo_get(key, self.settings.snapshot_ttl_seconds)
            if cached is not None:
                return cached

        errors: list[str] = []
        quote_date: date | None = None
        quote_time = ""
        try:
            quotes = self.get_index_quotes()
            anchor = next((q for q in quotes if q.get("code") == "sh000001"), quotes[0])
            quote_date = _parse_date(anchor.get("quote_date"))
            quote_time = str(anchor.get("quote_time", ""))
        except Exception as exc:  # noqa: BLE001
            errors.append(f"无法取得市场行情日期锚点：{exc}")

        try:
            frame, source = self._sina_snapshot(max_rows=max_rows)
            if quote_date is None:
                errors.append("无法从指数行情确认快照所属交易日，快照日期标记为未知")
            snapshot = SnapshotResult(
                frame=frame,
                quote_date=quote_date,
                quote_time=quote_time,
                source=source,
                fetched_at=_now_iso(self.settings),
                cached=False,
                stale=False,
                warnings=errors,
            )
            self._save_snapshot(snapshot)
            self._memo_set(key, snapshot, self.settings.snapshot_ttl_seconds)
            return snapshot
        except Exception as exc:  # noqa: BLE001
            errors.append(f"新浪全市场快照失败：{exc}")

        try:
            frame = self._akshare_spot()
            if max_rows:
                frame = frame.sort_values("amount", ascending=False).head(max_rows).reset_index(drop=True)
            snapshot = SnapshotResult(
                frame=frame,
                quote_date=quote_date,
                quote_time=quote_time,
                source="AKShare / 东方财富",
                fetched_at=_now_iso(self.settings),
                cached=False,
                stale=False,
                warnings=errors + ["AKShare 快照未提供逐条行情时间，行情日期以指数锚点为准"],
            )
            self._save_snapshot(snapshot)
            self._memo_set(key, snapshot, self.settings.snapshot_ttl_seconds)
            return snapshot
        except Exception as exc:  # noqa: BLE001
            errors.append(f"AKShare 全市场快照失败：{exc}")

        if allow_disk_cache:
            disk = self._load_snapshot_disk(max_rows)
            if disk is not None:
                disk.cached = True
                disk.stale = True
                disk.warnings = errors + ["实时快照不可用，显示上次成功缓存；不能当作实时行情"]
                self._memo_set(key, disk, 20)
                return disk
        raise DataProviderError("全市场快照不可用；" + "；".join(errors))

    def _sina_snapshot(self, max_rows: int | None = None) -> tuple[pd.DataFrame, str]:
        count_resp = self._get(
            "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
            "Market_Center.getHQNodeStockCount",
            params={"node": "hs_a"},
            headers={"Referer": "https://finance.sina.com.cn/"},
        )
        try:
            total = int(str(count_resp.json()).strip('"'))
        except (ValueError, TypeError):
            total = 6000
        page_size = 100
        pages = max(1, math.ceil(min(total, max_rows or total) / page_size))
        base = (
            "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
            "Market_Center.getHQNodeData"
        )

        def fetch_page(page: int) -> list[dict[str, Any]]:
            params = {
                "page": page,
                "num": page_size,
                "sort": "amount",
                "asc": 0,
                "node": "hs_a",
                "symbol": "",
                "_s_r_a": "page",
            }
            resp = self._get(
                base,
                params=params,
                headers={"Referer": "https://finance.sina.com.cn/"},
            )
            try:
                data = resp.json()
            except ValueError as exc:
                raise DataProviderError(f"第 {page} 页 JSON 解析失败") from exc
            return data or []

        rows: list[dict[str, Any]] = []
        with ThreadPoolExecutor(max_workers=min(8, self.settings.max_workers + 2)) as pool:
            futures = {pool.submit(fetch_page, p): p for p in range(1, pages + 1)}
            for future in as_completed(futures):
                try:
                    rows.extend(future.result())
                except Exception:
                    # 单页失败则整体失败，避免形成不完整的“涨跌分布”。
                    for pending in futures:
                        pending.cancel()
                    raise
        expected = min(total, max_rows or total)
        if len(rows) < max(50, expected * 0.85):
            raise DataProviderError(
                f"全市场快照仅取得 {len(rows)} 行（预期约 {expected} 行），数据不完整"
            )

        records = []
        for row in rows:
            code = str(row.get("code", "")).zfill(6)
            name = str(row.get("name", ""))
            price = _clean_float(row.get("trade"))
            prev = _clean_float(row.get("settlement"))
            if not code or not math.isfinite(price) or price <= 0:
                continue
            records.append(
                {
                    "code": code,
                    "name": name,
                    "price": price,
                    "prev_close": prev,
                    "open": _clean_float(row.get("open")),
                    "high": _clean_float(row.get("high")),
                    "low": _clean_float(row.get("low")),
                    "volume": _clean_float(row.get("volume")),
                    "amount": _clean_float(row.get("amount")),
                    "pct_chg": _clean_float(row.get("changepercent")),
                    "turnover": _clean_float(row.get("turnoverratio")),
                    "pe": _clean_float(row.get("per")),
                    "pb": _clean_float(row.get("pb")),
                    # 新浪 mktcap / nmc 单位为万元
                    "mktcap": _clean_float(row.get("mktcap")) * 10_000,
                    "float_mcap": _clean_float(row.get("nmc")) * 10_000,
                    "ticktime": str(row.get("ticktime", "")),
                }
            )
        frame = pd.DataFrame(records).drop_duplicates("code")
        frame = frame[(frame["price"] > 0) & (frame["prev_close"] > 0)].reset_index(drop=True)
        if max_rows:
            frame = frame.sort_values("amount", ascending=False).head(max_rows).reset_index(drop=True)
        return frame, "新浪财经 / Market_Center"

    def _akshare_spot(self) -> pd.DataFrame:
        ak = self._akshare()
        df = ak.stock_zh_a_spot_em()
        if df is None or df.empty:
            raise DataProviderError("AKShare 全市场快照为空")
        mapping = {
            "代码": "code",
            "名称": "name",
            "最新价": "price",
            "昨收": "prev_close",
            "今开": "open",
            "最高": "high",
            "最低": "low",
            "成交量": "volume",
            "成交额": "amount",
            "涨跌幅": "pct_chg",
            "换手率": "turnover",
            "市盈率-动态": "pe",
            "市净率": "pb",
            "总市值": "mktcap",
            "流通市值": "float_mcap",
        }
        frame = df.rename(columns=mapping)
        for column in [
            "price",
            "prev_close",
            "open",
            "high",
            "low",
            "volume",
            "amount",
            "pct_chg",
            "turnover",
            "pe",
            "pb",
            "mktcap",
            "float_mcap",
        ]:
            if column not in frame.columns:
                frame[column] = np.nan
            frame[column] = pd.to_numeric(frame[column], errors="coerce")
        frame["code"] = frame["code"].astype(str).str.zfill(6)
        frame = frame.dropna(subset=["code", "price"]).reset_index(drop=True)
        return frame

    def _snapshot_paths(self) -> tuple[Path, Path]:
        return (
            self.settings.cache_dir / "snapshot_latest.csv",
            self.settings.cache_dir / "snapshot_latest.meta.json",
        )

    def _save_snapshot(self, snapshot: SnapshotResult) -> None:
        # 仅缓存完整全市场快照；局部 top-N 快照不能覆盖它，否则行情失败时
        # 会被误当成全市场涨跌分布。
        if len(snapshot.frame) < 1000:
            return
        data_path, meta_path = self._snapshot_paths()
        tmp = data_path.with_suffix(".tmp")
        snapshot.frame.to_csv(tmp, index=False, encoding="utf-8")
        tmp.replace(data_path)
        atomic_write_json(
            meta_path,
            {
                "quote_date": snapshot.quote_date.isoformat() if snapshot.quote_date else None,
                "quote_time": snapshot.quote_time,
                "source": snapshot.source,
                "fetched_at": snapshot.fetched_at,
                "rows": len(snapshot.frame),
            },
        )

    def _load_snapshot_disk(self, max_rows: int | None = None) -> SnapshotResult | None:
        data_path, meta_path = self._snapshot_paths()
        if not data_path.exists():
            return None
        try:
            frame = pd.read_csv(data_path)
            frame["code"] = frame["code"].astype(str).str.zfill(6)
            meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else {}
        except (OSError, ValueError, pd.errors.ParserError):
            return None
        if max_rows:
            frame = frame.sort_values("amount", ascending=False).head(max_rows).reset_index(drop=True)
        return SnapshotResult(
            frame=frame,
            quote_date=_parse_date(meta.get("quote_date")),
            quote_time=str(meta.get("quote_time", "")),
            source=f"{meta.get('source', '本地缓存')}（缓存）",
            fetched_at=str(meta.get("fetched_at", "")),
            cached=True,
            stale=True,
            warnings=["本地快照缓存"],
        )

    # ------------------------------------------------------------------
    # 数据源自检
    # ------------------------------------------------------------------
    def health_check(self, force: bool = False) -> list[dict[str, Any]]:
        path = self.settings.health_path
        if not force and path.exists() and cache_age_seconds(path) < 600:
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(raw, list):
                    return raw
            except (OSError, ValueError):
                pass

        checks: list[SourceStatus] = []

        def timed(name: str, func, date_getter=None) -> None:
            start = time.perf_counter()
            try:
                value = func()
                elapsed = int((time.perf_counter() - start) * 1000)
                checks.append(
                    SourceStatus(
                        name=name,
                        ok=True,
                        latency_ms=elapsed,
                        message="可用",
                        checked_at=_now_iso(self.settings),
                        data_date=str(date_getter(value)) if date_getter else None,
                    )
                )
            except Exception as exc:  # noqa: BLE001
                elapsed = int((time.perf_counter() - start) * 1000)
                checks.append(
                    SourceStatus(
                        name=name,
                        ok=False,
                        latency_ms=elapsed,
                        message=f"{type(exc).__name__}: {exc}",
                        checked_at=_now_iso(self.settings),
                    )
                )

        timed("新浪指数行情", lambda: self.get_index_quotes(force=True), lambda x: x[0].get("quote_date"))
        end = datetime.now(self.settings.timezone).date()
        start = end - timedelta(days=20)
        timed(
            "AKShare 个股日线",
            lambda: self._akshare_history("600519", start, end, "qfq"),
            lambda x: x[0]["date"].iloc[-1],
        )
        timed(
            "东方财富个股日线（直连）",
            lambda: self._eastmoney_history_secid("1.600519", start, end, "qfq"),
            lambda x: x["date"].iloc[-1],
        )
        timed(
            "Baostock 个股日线（备用）",
            lambda: self._baostock_history("600519", start, end, "qfq"),
            lambda x: x[0]["date"].iloc[-1],
        )
        timed("新浪全市场快照", lambda: self._sina_snapshot(max_rows=100), lambda x: f"{len(x[0])} 行")
        timed("交易日历", lambda: load_calendar(force=True), lambda x: f"{x.dates[-1]}，{x.source}")

        result = [c.to_dict() for c in checks]
        atomic_write_json(path, result)
        return result


# ---------------------------------------------------------------------------
# 规范化工具
# ---------------------------------------------------------------------------
def _as_date(value: date | str | None) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


def _tencent_timestamp(value: Any) -> tuple[str, str]:
    raw = re.sub(r"\D", "", str(value or ""))
    if len(raw) >= 14:
        return (
            f"{raw[:4]}-{raw[4:6]}-{raw[6:8]}",
            f"{raw[8:10]}:{raw[10:12]}:{raw[12:14]}",
        )
    if len(raw) >= 8:
        return f"{raw[:4]}-{raw[4:6]}-{raw[6:8]}", ""
    return "", ""


def _parse_date(value: Any) -> date | None:
    if value is None:
        return None
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _age_from_iso(iso: str, settings: Settings) -> float:
    try:
        dt = datetime.fromisoformat(iso)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=settings.timezone)
        return max(0.0, (datetime.now(settings.timezone) - dt).total_seconds())
    except (ValueError, TypeError):
        return float("inf")


def _rename_akshare(df: pd.DataFrame) -> pd.DataFrame:
    mapping = {
        "日期": "date",
        "开盘": "open",
        "收盘": "close",
        "最高": "high",
        "最低": "low",
        "成交量": "volume",
        "成交额": "amount",
        "振幅": "amplitude",
        "涨跌幅": "pct_chg",
        "涨跌额": "change",
        "换手率": "turnover",
    }
    out = df.rename(columns=mapping).copy()
    for col in HISTORY_COLUMNS:
        if col not in out.columns:
            out[col] = np.nan
    return out


def _normalise_history(df: pd.DataFrame, date_col: str = "date") -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame(columns=HISTORY_COLUMNS)
    out = df.copy()
    if date_col not in out.columns:
        raise DataProviderError(f"日线缺少日期列 {date_col}")
    out["date"] = pd.to_datetime(out[date_col], errors="coerce")
    for col in ["open", "high", "low", "close", "volume", "amount", "pct_chg"]:
        if col not in out.columns:
            out[col] = np.nan
        out[col] = pd.to_numeric(out[col], errors="coerce")
    out = out.dropna(subset=["date", "open", "high", "low", "close"])
    out = out[out["close"] > 0]
    out = out[
        (out["high"] >= out["low"])
        & (out["high"] >= out["close"])
        & (out["low"] <= out["close"])
        & (out["high"] >= out["open"])
        & (out["low"] <= out["open"])
    ]
    out = out.sort_values("date").drop_duplicates("date", keep="last").reset_index(drop=True)
    out["volume"] = out["volume"].fillna(0.0).clip(lower=0)
    out["date"] = out["date"].dt.normalize()
    # 缺失涨跌幅时只按相邻收盘价计算，不改变价格数据。
    computed = out["close"].pct_change() * 100
    out["pct_chg"] = out["pct_chg"].where(out["pct_chg"].notna(), computed)
    return out[HISTORY_COLUMNS + [c for c in out.columns if c not in HISTORY_COLUMNS]]
