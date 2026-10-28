"""AStockQuant Streamlit 主界面。

页面只读取真实行情/结果文件；任何数据源失败都以错误提示呈现，不填充
模拟价格。核心计算在 data_provider / stock_screener / backtest 等模块，
Streamlit 进程不承担定时任务。
"""
from __future__ import annotations

import hashlib
import hmac
import html
import math
import os
import re
from datetime import datetime, timedelta
from typing import Any

import pandas as pd
import streamlit as st

from backtest import BacktestConfig, BacktestEngine, BacktestResult
from charts import breadth_chart, equity_chart, stock_chart
from config import APP_TITLE, APP_VERSION, SCORE_LABELS, SCORE_WEIGHTS, get_settings
from data_provider import MarketDataProvider, normalize_code
from market_calendar import (
    PREMARKET_DEADLINE,
    latest_completed_trade_day,
    load_calendar,
)
from market_service import build_market_overview, format_amount_cn
from risk_manager import position_size
from scheduler import run_screening_job
from stock_analysis import analyze_stock
from storage import (
    list_result_dates,
    load_result,
    load_watchlist,
    recent_job_runs,
)
from watchlist_service import (
    add_to_watchlist,
    remove_from_watchlist,
    watchlist_rows,
)

st.set_page_config(
    page_title=APP_TITLE,
    page_icon="📈",
    layout="wide",
    initial_sidebar_state="collapsed",
)

CHART_CONFIG = {"displayModeBar": False, "responsive": True}
NAV_ITEMS = [
    "市场总览",
    "盘前选股",
    "盘后选股",
    "个股分析",
    "自选股",
    "策略回测",
    "风险与系统",
]


# ---------------------------------------------------------------------------
# 样式与基础工具
# ---------------------------------------------------------------------------
def inject_css() -> None:
    st.markdown(
        """
<style>
  .block-container {max-width: 1180px; padding-top: 1.1rem; padding-bottom: 3.5rem;}
  .hero {padding: .3rem 0 .9rem 0;}
  .hero h1 {font-size: 1.65rem; margin: 0 0 .2rem 0; letter-spacing: -.02em;}
  .eyebrow {font-size: .72rem; letter-spacing: .16em; text-transform: uppercase; color: #f6c85f; font-weight: 700;}
  .subtle {color: #94a3b8; font-size: .84rem; line-height: 1.55;}
  .idx-card {border: 1px solid rgba(148,163,184,.18); background: linear-gradient(145deg, rgba(246,200,95,.055), rgba(20,26,36,.75));
            border-radius: 14px; padding: .8rem .85rem; margin-bottom: .55rem;}
  .idx-name {font-size: .83rem; color: #a8b3c4;}
  .idx-price {font-size: 1.42rem; font-weight: 700; margin: .12rem 0;}
  .idx-meta {font-size: .72rem; color: #7f8a9c;}
  .up {color: #ff6b63;} .down {color: #39c994;} .flat {color: #a8b3c4;}
  .zone {border-left: 3px solid #f6c85f; padding: .25rem 0 .25rem .65rem; color: #d7dde8; font-size: .86rem;}
  .pill {display:inline-block; padding:.1rem .45rem; border-radius:999px; background:rgba(246,200,95,.12);
         color:#f6c85f; font-size:.72rem; margin:.1rem .2rem .1rem 0;}
  .tiny {font-size:.72rem;color:#7f8a9c;}
  div[data-testid="stMetricValue"] {font-size: 1.35rem;}
  div[data-testid="stDataFrame"] {border: 1px solid rgba(148,163,184,.15); border-radius: 10px;}
  .stButton > button, .stDownloadButton > button {min-height: 42px; border-radius: 9px;}
  @media (max-width: 700px) {
    .block-container {padding: .55rem .55rem 3rem;}
    .hero h1 {font-size: 1.35rem;}
    .idx-price {font-size: 1.2rem;}
    div[data-testid="stHorizontalBlock"] {gap: .45rem;}
    div[data-testid="stMetricValue"] {font-size: 1.05rem;}
    div[data-testid="stMetricLabel"] {font-size: .72rem;}
    .stButton > button, .stDownloadButton > button {width: 100%;}
    .subtle {font-size: .78rem;}
  }
</style>
""",
        unsafe_allow_html=True,
    )


def fmt(value: Any, digits: int = 2, default: str = "—") -> str:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(f):
        return default
    return f"{f:,.{digits}f}"


def fmt_pct(value: Any, digits: int = 2) -> str:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return "—"
    if not math.isfinite(f):
        return "—"
    return f"{f:+.{digits}f}%"


def fmt_dt(value: str | None, with_time: bool = True) -> str:
    if not value:
        return "—"
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return str(value)
    return dt.strftime("%Y-%m-%d %H:%M") if with_time else dt.strftime("%Y-%m-%d")


def color_class(value: Any) -> str:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return "flat"
    return "up" if f > 0 else "down" if f < 0 else "flat"


def page_header(title: str, subtitle: str, kicker: str = "ASTOCKQUANT") -> None:
    st.markdown(
        f"""<div class="hero"><div class="eyebrow">{html.escape(kicker)}</div>
        <h1>{html.escape(title)}</h1><div class="subtle">{html.escape(subtitle)}</div></div>""",
        unsafe_allow_html=True,
    )


def flash() -> None:
    message = st.session_state.pop("flash", None)
    if message:
        level, text = message
        if level == "success":
            st.success(text)
        elif level == "warning":
            st.warning(text)
        else:
            st.error(text)


def app_password() -> str:
    """读取访问密码。

    优先环境变量；否则读取 data/state/app_password.txt（start_public.ps1 会生成）。
    两者都不存在时不启用登录，仅适合本机开发。
    """
    env_value = os.getenv("ASTOCK_APP_PASSWORD", "").strip()
    if env_value:
        return env_value
    try:
        secret_value = str(st.secrets.get("ASTOCK_APP_PASSWORD", "")).strip()
        if secret_value:
            return secret_value
    except Exception:  # noqa: BLE001 - 本地没有 secrets.toml 时正常降级
        pass
    try:
        path = get_settings().state_dir / "app_password.txt"
        if path.exists():
            return path.read_text(encoding="utf-8").strip()
    except OSError:
        pass
    return ""


# 公网托管（例如 Streamlit Community Cloud）未配置 Secret 时的兜底密码哈希。
# 只存 SHA-256，不存明文；设置 ASTOCK_APP_PASSWORD 会覆盖它。
DEFAULT_PASSWORD_SHA256 = "39adf92b27f41684aa5b0ab38b1505ba6003cecf8d1689ce18381c497aa2af65"


def require_auth() -> None:
    """公网访问前的密码门禁。

    优先使用环境变量/本地密码文件；都没有时使用默认密码的哈希兜底，
    避免部署后出现“忘记配置 Secret 导致完全公开”的窗口。生产环境仍建议
    在反向代理层再加一层 Basic Auth / Cloudflare Access。
    """
    expected = app_password()
    expected_hash = os.getenv("ASTOCK_APP_PASSWORD_SHA256", "").strip().lower()
    if not expected and not expected_hash:
        expected_hash = DEFAULT_PASSWORD_SHA256
    if not expected and not expected_hash:
        return
    if st.session_state.get("astock_authenticated") is True:
        return

    st.markdown(
        """<div style="max-width:420px;margin:9vh auto 0 auto;">
        <div class="eyebrow">ASTOCKQUANT · PRIVATE</div>
        <h1 style="font-size:1.55rem;margin:.25rem 0 .5rem;">需要访问密码</h1>
        <div class="subtle">这是私有研究页面。请输入部署时设置的密码。</div>
        </div>""",
        unsafe_allow_html=True,
    )
    with st.form("astock_login"):
        entered = st.text_input("访问密码", type="password", autocomplete="current-password")
        submit = st.form_submit_button("进入系统", type="primary", width="stretch")
    if submit:
        valid = bool(expected) and hmac.compare_digest(entered, expected)
        if not valid and expected_hash:
            valid = hmac.compare_digest(
                hashlib.sha256(entered.encode("utf-8")).hexdigest(), expected_hash
            )
        if valid:
            st.session_state["astock_authenticated"] = True
            st.rerun()
        else:
            st.error("密码错误。")
    st.stop()


# ---------------------------------------------------------------------------
# 缓存数据入口
# ---------------------------------------------------------------------------
@st.cache_resource(show_spinner=False)
def get_provider() -> MarketDataProvider:
    return MarketDataProvider(get_settings())


@st.cache_data(ttl=300, show_spinner=False)
def startup_check() -> dict[str, Any]:
    provider = MarketDataProvider()
    result: dict[str, Any] = {"quote_ok": False, "calendar_ok": False, "error": None}
    try:
        quotes = provider.get_index_quotes()
        result["quote_ok"] = bool(quotes)
        result["quote_date"] = quotes[0].get("quote_date") if quotes else None
        result["quote_time"] = quotes[0].get("quote_time") if quotes else None
        result["quote_source"] = quotes[0].get("source") if quotes else None
    except Exception as exc:  # noqa: BLE001
        result["error"] = f"{type(exc).__name__}: {exc}"
    try:
        cal = load_calendar()
        result["calendar_ok"] = True
        result["calendar_source"] = cal.source
        result["calendar_reliable"] = cal.reliable
        result["calendar_warning"] = cal.warning
        result["calendar_end"] = cal.dates[-1].isoformat()
    except Exception as exc:  # noqa: BLE001
        result["calendar_error"] = f"{type(exc).__name__}: {exc}"
    # 启动时额外探测一次真实个股日线，避免“指数能通就以为选股数据可用”。
    try:
        end = datetime.now(get_settings().timezone).date()
        history = provider.get_stock_history(
            "600519", start=end - timedelta(days=20), end=end, force=True
        )
        result["history_ok"] = True
        result["history_source"] = history.source
        result["history_date"] = history.latest_date.isoformat() if history.latest_date else None
    except Exception as exc:  # noqa: BLE001
        result["history_ok"] = False
        result["history_error"] = f"{type(exc).__name__}: {exc}"
    return result


@st.cache_data(ttl=75, show_spinner=False)
def cached_overview(nonce: int = 0) -> dict[str, Any]:
    return build_market_overview(get_provider(), get_settings())


@st.cache_data(ttl=300, show_spinner=False)
def cached_analysis(code: str) -> dict[str, Any]:
    return analyze_stock(code, get_provider(), get_settings())


@st.cache_data(ttl=600, show_spinner=False)
def cached_health() -> list[dict[str, Any]]:
    return MarketDataProvider().health_check()


# ---------------------------------------------------------------------------
# 侧边栏
# ---------------------------------------------------------------------------
def render_sidebar() -> str:
    settings = get_settings()
    with st.sidebar:
        st.markdown(
            f"<div class='eyebrow'>A股短线研究台</div><div style='font-size:1.15rem;font-weight:750;'>AStockQuant</div>"
            f"<div class='tiny'>v{APP_VERSION} · Asia/Shanghai</div>",
            unsafe_allow_html=True,
        )
        page = st.radio("功能导航", NAV_ITEMS, label_visibility="collapsed")
        st.divider()
        check = startup_check()
        if check.get("quote_ok"):
            st.caption(f"● 指数行情可用 · {check.get('quote_source')}")
            st.caption(f"行情日期 {check.get('quote_date')} {check.get('quote_time') or ''}")
        else:
            st.error("● 指数行情不可用")
            if check.get("error"):
                st.caption(check["error"][:240])
        if check.get("history_ok"):
            st.caption(f"● 个股日线可用 · {check.get('history_source')}")
        else:
            st.error("● 个股日线不可用")
            if check.get("history_error"):
                st.caption(check["history_error"][:240])
        if check.get("calendar_ok"):
            if check.get("calendar_reliable"):
                st.caption(f"● 交易日历可用 · 至 {check.get('calendar_end')}")
            else:
                st.warning("● 交易日历降级：节假日可能判断错误")
        else:
            st.warning("● 交易日历不可用")
        st.divider()
        st.caption(f"当前时间 {datetime.now(settings.timezone):%m-%d %H:%M}")
        st.caption("手机端：点左上角 ☰ 打开导航。数据失败会明确提示，绝不使用模拟行情。")
        if app_password():
            if st.button("退出登录", width="stretch"):
                st.session_state.clear()
                st.rerun()
    return page


# ---------------------------------------------------------------------------
# 市场总览
# ---------------------------------------------------------------------------
def render_index_card(quote: dict[str, Any]) -> None:
    pct = quote.get("pct_chg")
    source = html.escape(str(quote.get("source", "")))
    stale = " · 缓存" if quote.get("stale") else ""
    date_text = html.escape(str(quote.get("quote_date") or "日期未知"))
    time_text = html.escape(str(quote.get("quote_time") or ""))
    st.markdown(
        f"""<div class="idx-card">
          <div class="idx-name">{html.escape(str(quote.get('name', '')))}</div>
          <div class="idx-price {color_class(pct)}">{fmt(quote.get('price'), 2)}</div>
          <div class="{color_class(pct)}" style="font-size:.9rem;">{fmt_pct(pct)}</div>
          <div class="idx-meta">{date_text} {time_text} · {source}{stale}</div>
        </div>""",
        unsafe_allow_html=True,
    )


def page_market() -> None:
    page_header(
        "市场总览",
        "上证、深证、创业板与沪深300的最新真实行情；涨跌分布来自全市场快照。",
        "REAL MARKET DATA",
    )
    top = st.columns([1, 1, 1.2])
    with top[0]:
        if st.button("刷新行情", width="stretch"):
            get_provider.clear()
            st.cache_data.clear()
            st.rerun()
    with top[2]:
        st.caption(
            "数据获取失败时页面会显示错误，不会用 0 或模拟值填充。行情日期与获取时间分开标注。"
        )
    try:
        overview = cached_overview(st.session_state.get("overview_nonce", 0))
    except Exception as exc:  # noqa: BLE001
        st.error(f"市场总览不可用：{type(exc).__name__}: {exc}")
        return

    for err in overview.get("errors", []):
        st.error(err)
    for warning in overview.get("warnings", []):
        st.warning(warning)

    indices = overview.get("indices", [])
    if indices:
        for start in range(0, len(indices), 2):
            cols = st.columns(2)
            for col, quote in zip(cols, indices[start : start + 2]):
                with col:
                    render_index_card(quote)
    else:
        st.error("指数行情未获取到，无法展示上证/深证/创业板数据。")

    breadth = overview.get("breadth")
    if breadth:
        st.markdown("### 市场宽度与成交")
        chart_col, stat_col = st.columns([1.35, 1])
        with chart_col:
            st.plotly_chart(
                breadth_chart(breadth),
                width="stretch",
                config=CHART_CONFIG,
            )
        with stat_col:
            c1, c2 = st.columns(2)
            c1.metric("上涨", f"{breadth['advancers']} 只")
            c2.metric("下跌", f"{breadth['decliners']} 只")
            c3, c4 = st.columns(2)
            c3.metric("上涨占比", f"{fmt(breadth.get('up_ratio'), 1)}%")
            c4.metric("市场中位数", fmt_pct(breadth.get("median_pct")))
            c5, c6 = st.columns(2)
            c5.metric("涨停估算", f"{breadth.get('limit_up_est', 0)} 只")
            c6.metric("跌停估算", f"{breadth.get('limit_down_est', 0)} 只")
            st.caption(f"全市场成交额：{format_amount_cn(breadth.get('total_amount'))}")
            st.caption(f"温度：{breadth.get('temperature')}（仅按上涨占比划分）")
            st.caption(breadth.get("limit_rule", ""))
        snapshot = overview.get("snapshot") or {}
        st.caption(
            f"快照来源：{snapshot.get('source', '—')} · 行情日期 "
            f"{snapshot.get('quote_date') or '未知'} {snapshot.get('quote_time') or ''} · "
            f"获取时间 {fmt_dt(snapshot.get('fetched_at'))} · 行数 {snapshot.get('rows', 0)}"
        )
        if snapshot.get("stale"):
            st.error("这是缓存快照，不是实时行情。请勿据此判断当前盘面。")
    else:
        st.warning("涨跌分布不可用：全市场快照获取失败。上方指数行情可能是独立数据源。")

    if not indices and not breadth:
        st.error("所有市场行情均不可用。请检查网络、数据源或稍后重试。")


# ---------------------------------------------------------------------------
# 选股结果
# ---------------------------------------------------------------------------
def candidates_table(candidates: list[dict[str, Any]]) -> pd.DataFrame:
    rows = []
    for item in candidates:
        plan = item.get("risk_plan") or {}
        zone = plan.get("observation_zone") or [None, None]
        rows.append(
            {
                "排名": item.get("rank"),
                "代码": item.get("code"),
                "名称": item.get("name"),
                "评分": item.get("score"),
                "收盘": item.get("close"),
                "涨跌幅%": item.get("pct_chg"),
                "形态": " / ".join(item.get("setup") or []),
                "观察区间": f"{fmt(zone[0])} - {fmt(zone[1])}",
                "止损参考": plan.get("stop_loss"),
                "盈亏比": plan.get("risk_reward_ratio"),
            }
        )
    return pd.DataFrame(rows)


def render_candidate_cards(candidates: list[dict[str, Any]], result: dict[str, Any]) -> None:
    st.markdown("### 候选详情")
    for item in candidates:
        plan = item.get("risk_plan") or {}
        setup = " · ".join(item.get("setup") or []) or "趋势观察"
        with st.expander(
            f"#{item.get('rank')}  {item.get('code')} {item.get('name')}   "
            f"评分 {fmt(item.get('score'), 1)} · {setup}",
            expanded=False,
        ):
            c1, c2, c3 = st.columns(3)
            c1.metric("收盘", fmt(item.get("close")))
            c2.metric("涨跌幅", fmt_pct(item.get("pct_chg")))
            c3.metric("RSI14", fmt(item.get("rsi14"), 1))
            c4, c5, c6 = st.columns(3)
            c4.metric("量比", fmt(item.get("vol_ratio")))
            c5.metric("ATR波动", f"{fmt(item.get('atr_pct'), 2)}%")
            c6.metric("20日超额", fmt_pct(item.get("excess_20")))
            st.markdown(
                f"<div class='zone'>观察区间 <b>{fmt(plan.get('observation_zone', [None, None])[0])} - "
                f"{fmt(plan.get('observation_zone', [None, None])[1])}</b>　"
                f"止损参考 <b>{fmt(plan.get('stop_loss'))}</b>　"
                f"目标参考 <b>{fmt(plan.get('target'))}</b>　"
                f"风险收益比 <b>{fmt(plan.get('risk_reward_ratio'))}</b></div>",
                unsafe_allow_html=True,
            )
            st.caption(
                f"风险等级：{plan.get('risk_level', '—')} · 数据日期 {plan.get('data_date', item.get('data_date'))} · "
                f"止损距离 {fmt(plan.get('stop_distance_pct'))}%"
            )
            left, right = st.columns(2)
            with left:
                st.markdown("**入选理由**")
                for reason in item.get("reasons", []):
                    st.markdown(f"- {reason}")
            with right:
                st.markdown("**风险提示**")
                for risk in item.get("risks", []):
                    st.markdown(f"- {risk}")
                if not item.get("risks"):
                    st.markdown("- 未触发预设风险阈值，但仍可能波动")
            st.markdown("**评分拆解**")
            for key, label in SCORE_LABELS.items():
                weight = float(SCORE_WEIGHTS[key])
                value = float(item.get("score_breakdown", {}).get(key, 0) or 0)
                st.progress(
                    max(0.0, min(1.0, value / weight)),
                    text=f"{label} {value:.1f} / {weight:.0f}",
                )
            st.caption("总分 100 分，只代表规则内的相对强度，不代表上涨概率。")
            plan_basis = plan.get("basis", [])
            if plan_basis:
                st.markdown("**价格计算依据**")
                for basis in plan_basis:
                    st.caption(f"· {basis}")
            st.caption(
                f"数据来源：{item.get('data_source', '—')} · 获取时间 {fmt_dt(item.get('data_fetched_at'))}"
            )
            cols = st.columns(2)
            with cols[0]:
                if st.button("加入自选", key=f"watch_{result.get('mode')}_{item.get('code')}"):
                    try:
                        added = add_to_watchlist(item["code"], get_provider(), get_settings())
                        st.toast(f"已加入自选：{added.get('name') or added['code']}")
                    except Exception as exc:  # noqa: BLE001
                        st.error(f"加入自选失败：{exc}")
            if plan.get("disclaimer"):
                st.caption(plan["disclaimer"])


def render_result_warnings(result: dict[str, Any]) -> None:
    warnings = result.get("warnings") or []
    limitations = result.get("limitations") or []
    if warnings:
        st.warning(f"本次任务有 {len(warnings)} 条数据/时点警告，详情见下方。")
    if result.get("calendar", {}).get("reliable") is False:
        st.error("交易日历不可靠：节假日判断可能错误，请人工核对交易所日历。")
    with st.expander("数据警告、时点纪律与已知限制"):
        st.markdown("**数据与运行时点**")
        st.markdown(
            f"- 信号日：`{result.get('signal_date')}`；目标观察日：`{result.get('target_date')}`\n"
            f"- 数据截止：`{result.get('data_as_of')}`；生成时间：`{result.get('generated_at')}`\n"
            f"- 时点纪律：{result.get('data_discipline')}"
        )
        if warnings:
            st.markdown("**警告**")
            for warning in warnings:
                st.markdown(f"- {warning}")
        if limitations:
            st.markdown("**限制**")
            for limitation in limitations:
                st.markdown(f"- {limitation}")
        stats = result.get("selection_stats") or {}
        if stats:
            st.markdown("**筛选统计**")
            st.json(
                {
                    "股票池": stats.get("universe_size"),
                    "成功日线": stats.get("history_success"),
                    "失败": stats.get("history_failed"),
                    "跳过": stats.get("history_skipped"),
                    "过滤": stats.get("filtered"),
                    "候选": stats.get("selected"),
                    "耗时秒": stats.get("elapsed_seconds"),
                }
            )
        if result.get("failures_preview"):
            st.markdown("**失败样本**")
            st.json(result["failures_preview"])


def run_job_from_ui(mode: str) -> None:
    label = "盘前选股" if mode == "premarket" else "盘后选股"
    st.caption("提示：页面内运行会占用当前请求，云端请优先使用独立 scheduler。")
    if not st.button(f"立即运行{label}", type="primary", width="stretch"):
        return
    with st.status(f"正在运行{label}…", expanded=True) as status:
        try:
            outcome = run_screening_job(
                mode,
                run_context="streamlit",
                progress=lambda msg: status.update(label=msg),
            )
            if outcome.get("status") == "skipped":
                status.update(label="任务跳过", state="complete")
                st.session_state["flash"] = ("warning", str(outcome.get("reason")))
            else:
                status.update(label=f"{label}完成", state="complete")
                result = outcome.get("result") or {}
                st.session_state["flash"] = (
                    "success",
                    f"{label}已生成，信号日 {result.get('signal_date')}，候选 {len(result.get('candidates', []))} 只。",
                )
        except Exception as exc:  # noqa: BLE001
            status.update(label="任务失败", state="error")
            st.error(f"{label}失败：{type(exc).__name__}: {exc}")
            return
    st.cache_data.clear()
    st.rerun()


def page_screening(mode: str) -> None:
    is_pre = mode == "premarket"
    page_header(
        "盘前选股" if is_pre else "盘后选股",
        (
            "使用上一完整交易日收盘数据，为目标交易日生成候选；不读取目标日盘中价格。"
            if is_pre
            else "收盘后使用完整日线，筛选下一交易日重点观察池；含均线、MACD、RSI、量能、突破/回踩与相对强度。"
        ),
        "PREMARKET" if is_pre else "POSTMARKET",
    )
    flash()

    dates = list_result_dates(mode)
    control = st.columns([1.4, 1])
    with control[0]:
        if dates:
            selected_date = st.selectbox(
                "查看结果（信号日）", dates, index=0, key=f"{mode}_result_date"
            )
        else:
            selected_date = None
            st.info("还没有已保存结果。可点击右侧按钮运行，或由定时任务生成。")
    with control[1]:
        run_job_from_ui(mode)

    result = load_result(mode, selected_date) if selected_date else None
    if not result:
        st.markdown(
            f"<div class='subtle'>盘前任务应在交易日 09:00 前完成；盘后任务应在 15:00 收盘后执行。"
            f"两者结果分别保存，不在页面内混用。</div>",
            unsafe_allow_html=True,
        )
        return

    if result.get("mode") != mode:
        st.error("结果文件模式与当前页面不一致，已拒绝展示。")
        return

    st.divider()
    top = st.columns([1.3, 1, 1])
    top[0].metric("信号日", result.get("signal_date"))
    top[1].metric("目标观察日", result.get("target_date"))
    top[2].metric("候选数量", len(result.get("candidates", [])))
    st.caption(
        f"生成时间 {fmt_dt(result.get('generated_at'))} · 数据截止 {result.get('data_as_of')} · "
        f"股票池来源 {result.get('universe', {}).get('source', '—')}"
    )
    st.info(result.get("data_discipline", ""))
    render_result_warnings(result)

    market = result.get("market") or []
    if market:
        with st.expander("任务运行时的指数行情（非候选评分依据）"):
            for quote in market:
                st.markdown(
                    f"- {quote.get('name')} {fmt(quote.get('price'))} "
                    f"{fmt_pct(quote.get('pct_chg'))} · {quote.get('quote_date')} "
                    f"{quote.get('quote_time')} · {quote.get('source')}"
                )

    candidates = result.get("candidates") or []
    if not candidates:
        st.warning(
            "本次没有达到评分阈值的候选。系统没有用随机/模拟数据补足，这本身是有效结果。"
        )
        return

    scores = [float(c.get("score", 0)) for c in candidates]
    c1, c2, c3 = st.columns(3)
    c1.metric("最高分", fmt(max(scores), 1))
    c2.metric("平均分", fmt(sum(scores) / len(scores), 1))
    c3.metric("最低分", fmt(min(scores), 1))

    df = candidates_table(candidates)
    st.markdown("### 候选列表")
    st.caption("手机端表格可左右滑动；点击下方卡片查看完整理由与风险。")
    st.dataframe(
        df,
        hide_index=True,
        width="stretch",
        column_config={
            "评分": st.column_config.NumberColumn(format="%.1f"),
            "收盘": st.column_config.NumberColumn(format="%.2f"),
            "涨跌幅%": st.column_config.NumberColumn(format="%.2f"),
            "止损参考": st.column_config.NumberColumn(format="%.2f"),
            "盈亏比": st.column_config.NumberColumn(format="%.2f"),
        },
    )
    st.download_button(
        "下载候选 CSV",
        data=df.to_csv(index=False).encode("utf-8-sig"),
        file_name=f"astockquant_{mode}_{result.get('signal_date')}.csv",
        mime="text/csv",
    )
    render_candidate_cards(candidates, result)
    st.caption(
        "风险提示：候选仅是基于历史行情的规则筛选结果，不构成投资建议，不保证收益。"
    )


# ---------------------------------------------------------------------------
# 个股分析
# ---------------------------------------------------------------------------
def page_stock_analysis() -> None:
    page_header(
        "个股分析",
        "输入 6 位 A 股代码，查看真实前复权 K 线、成交量、MA、MACD、RSI 与风险参考。",
        "SINGLE STOCK",
    )
    with st.form("stock_form"):
        col1, col2 = st.columns([1.3, 1])
        code_input = col1.text_input("股票代码", value=st.session_state.get("analysis_code", "603986"), max_chars=8)
        col2.markdown("<div style='height:1.72rem'></div>", unsafe_allow_html=True)
        submitted = col2.form_submit_button("分析", type="primary", width="stretch")
    if submitted:
        try:
            st.session_state["analysis_code"] = normalize_code(code_input)
        except ValueError as exc:
            st.error(str(exc))
            return

    code = st.session_state.get("analysis_code")
    if not code:
        return
    try:
        data = cached_analysis(code)
    except Exception as exc:  # noqa: BLE001
        st.error(f"{code} 分析失败：{type(exc).__name__}: {exc}")
        st.info("常见原因：代码不存在、网络不可用、数据源限流，或该股票有效日线不足。")
        return

    quote = data.get("quote")
    summary = data.get("summary") or {}
    st.divider()
    header_cols = st.columns([1.3, 1, 1])
    header_cols[0].markdown(
        f"### {html.escape(str(data.get('name')))} · {data.get('code')}<div class='subtle'>"
        f"K线截止 {data.get('analysis_date')}（完整交易日）</div>",
        unsafe_allow_html=True,
    )
    if quote:
        header_cols[1].metric("最新报价", fmt(quote.get("price")), fmt_pct(quote.get("pct_chg")))
        header_cols[2].metric("报价时间", f"{quote.get('quote_date', '—')} {quote.get('quote_time', '')}")
    else:
        header_cols[1].metric("最新报价", "—")
        header_cols[2].caption("报价获取失败")

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("趋势", summary.get("trend", "—"))
    c2.metric("RSI14", fmt(summary.get("rsi14"), 1), summary.get("rsi_state", ""))
    c3.metric("ATR波动", f"{fmt(summary.get('atr_pct'), 2)}%")
    c4.metric("20日支撑/压力", f"{fmt(summary.get('support20'))} / {fmt(summary.get('resistance20'))}")

    st.plotly_chart(
        stock_chart(data["frame"], data["name"], data["code"]),
        width="stretch",
        config=CHART_CONFIG,
    )
    if quote and quote.get("quote_date") and quote.get("quote_date") > data.get("analysis_date", ""):
        st.info("上方 K 线只使用完整交易日；实时/盘中报价单独显示，避免把未完成日线当成收盘数据。")

    tab1, tab2, tab3 = st.tabs(["技术面", "风险与支撑压力", "数据说明"])
    with tab1:
        tech = pd.DataFrame(
            [
                ("收盘价", fmt(summary.get("close"))),
                ("MA5", fmt(summary.get("ma5"))),
                ("MA10", fmt(summary.get("ma10"))),
                ("MA20", fmt(summary.get("ma20"))),
                ("MA60", fmt(summary.get("ma60"))),
                ("MACD DIF", fmt(summary.get("macd_dif"), 3)),
                ("MACD DEA", fmt(summary.get("macd_dea"), 3)),
                ("MACD 柱", fmt(summary.get("macd_hist"), 3)),
                ("RSI14", f"{fmt(summary.get('rsi14'), 1)}（{summary.get('rsi_state')}）"),
                ("距 MA20", fmt_pct(summary.get("distance_ma20_pct"))),
                ("60日回撤", fmt_pct(summary.get("drawdown60_pct"))),
                ("量比（5日）", fmt(summary.get("vol_ratio"))),
            ],
            columns=["指标", "数值"],
        )
        st.dataframe(tech, hide_index=True, width="stretch")
        st.markdown(f"**趋势判断**：{summary.get('trend')}；**MACD**：{summary.get('macd_state')}。")
        st.caption("指标为规则计算，不构成涨跌预测。")
    with tab2:
        risk = data.get("risk") or {}
        if risk.get("error"):
            st.error(f"风险参考不可用：{risk['error']}")
        else:
            r1, r2, r3 = st.columns(3)
            zone = risk.get("observation_zone") or [None, None]
            r1.metric("观察区间", f"{fmt(zone[0])} - {fmt(zone[1])}")
            r2.metric("止损参考", fmt(risk.get("stop_loss")), f"距离 {fmt(risk.get('stop_distance_pct'))}%")
            r3.metric("风险收益比", fmt(risk.get("risk_reward_ratio")))
            st.markdown(f"**目标参考**：{fmt(risk.get('target'))}（{risk.get('target_rule')}）")
            st.markdown(f"**风险等级**：{risk.get('risk_level')}")
            for note in risk.get("risk_notes", []):
                st.markdown(f"- {note}")
            st.markdown("**计算依据**")
            for basis in risk.get("basis", []):
                st.caption(f"· {basis}")
            st.caption(risk.get("disclaimer", ""))
        if summary.get("risk_notes"):
            st.markdown("**技术风险提示**")
            for note in summary["risk_notes"]:
                st.markdown(f"- {note}")
    with tab3:
        st.markdown(
            f"- 行情来源：`{data.get('history').source if data.get('history') else '—'}`\n"
            f"- 获取时间：`{fmt_dt(data.get('history').fetched_at if data.get('history') else None)}`\n"
            f"- 复权方式：`{data.get('history').adjust if data.get('history') else '—'}`\n"
            f"- 基准：`{data.get('benchmark')}`\n"
            f"- 实时报价来源：`{(quote or {}).get('source', '—')}`"
        )
        for warning in data.get("warnings", []):
            st.warning(warning)
        st.download_button(
            "下载日线 CSV",
            data=data["frame"].to_csv(index=False).encode("utf-8-sig"),
            file_name=f"astockquant_{data['code']}_daily.csv",
            mime="text/csv",
        )
        st.caption(data.get("disclaimer", ""))


# ---------------------------------------------------------------------------
# 自选股
# ---------------------------------------------------------------------------
def page_watchlist() -> None:
    page_header(
        "自选股",
        "保存关注列表，查看最新可获取行情与最近一次盘前/盘后选股信号。",
        "WATCHLIST",
    )
    settings = get_settings()
    flash()
    with st.form("add_watch"):
        cols = st.columns([2, 1])
        code_input = cols[0].text_input("添加股票代码", placeholder="例如 603986")
        submitted = cols[1].form_submit_button("添加", type="primary", width="stretch")
    if submitted:
        try:
            item = add_to_watchlist(code_input, get_provider(), settings)
            st.session_state["flash"] = ("success", f"已加入自选：{item.get('name') or item['code']}")
            st.rerun()
        except Exception as exc:  # noqa: BLE001
            st.error(f"添加失败：{type(exc).__name__}: {exc}")

    items = load_watchlist(settings)
    if not items:
        st.info("自选股为空。添加后会保存在 data/state/watchlist.json；云端需挂载持久化卷。")
        return

    options = [f"{x['code']} {x.get('name') or ''}".strip() for x in items]
    with st.form("remove_watch"):
        cols = st.columns([2, 1])
        selected = cols[0].selectbox("移除股票", options)
        remove = cols[1].form_submit_button("移除", width="stretch")
    if remove:
        try:
            remove_from_watchlist(selected.split()[0], settings)
            st.session_state["flash"] = ("success", f"已移除 {selected}")
            st.rerun()
        except Exception as exc:  # noqa: BLE001
            st.error(f"移除失败：{exc}")

    try:
        rows = watchlist_rows(get_provider(), settings)
    except Exception as exc:  # noqa: BLE001
        st.error(f"自选行情加载失败：{type(exc).__name__}: {exc}")
        return
    table = pd.DataFrame(
        [
            {
                "代码": row["code"],
                "名称": row["name"],
                "最新价": row.get("price"),
                "涨跌幅%": row.get("pct_chg"),
                "行情日期": row.get("quote_date"),
                "时间": row.get("quote_time"),
                "最近信号": row.get("signal_text"),
                "数据错误": row.get("error"),
            }
            for row in rows
        ]
    )
    st.dataframe(
        table,
        hide_index=True,
        width="stretch",
        column_config={
            "最新价": st.column_config.NumberColumn(format="%.2f"),
            "涨跌幅%": st.column_config.NumberColumn(format="%.2f"),
        },
    )
    st.caption("表格可左右滑动。“最近信号”只引用已保存的盘前/盘后结果，不是实时买卖建议。")
    for row in rows:
        if row.get("error"):
            st.error(f"{row['code']}：{row['error']}")
    st.caption(
        f"保存位置：{settings.watchlist_path}。Streamlit Community Cloud 的文件系统可能重启丢失，"
        "生产部署请使用持久化磁盘。"
    )


# ---------------------------------------------------------------------------
# 回测
# ---------------------------------------------------------------------------
def _codes_from_result(mode: str) -> tuple[list[str], dict[str, str]]:
    result = load_result(mode)
    if not result:
        return [], {}
    codes = []
    names = {}
    for item in result.get("candidates", []):
        code = str(item.get("code"))
        codes.append(code)
        names[code] = str(item.get("name") or "")
    return codes, names


def _parse_codes(text: str) -> list[str]:
    found = re.findall(r"(?<!\d)(\d{6})(?!\d)", text or "")
    result: list[str] = []
    for code in found:
        try:
            code = normalize_code(code)
        except ValueError:
            continue
        if code not in result:
            result.append(code)
    return result


def page_backtest() -> None:
    page_header(
        "策略回测",
        "收盘信号、次日开盘成交，计入 A 股 T+1、涨跌停、停牌、手续费与滑点；已知限制会明确标注。",
        "BACKTEST",
    )
    settings = get_settings()
    try:
        default_end, _ = latest_completed_trade_day(settings=settings)
    except Exception:
        default_end = datetime.now(settings.timezone).date()
    default_start = default_end - timedelta(days=365)

    post_codes, post_names = _codes_from_result("postmarket")
    pre_codes, pre_names = _codes_from_result("premarket")
    watch = load_watchlist(settings)
    watch_codes = [x["code"] for x in watch]
    watch_names = {x["code"]: x.get("name", "") for x in watch}

    source = st.radio(
        "回测股票池",
        ["最近盘后候选", "最近盘前候选", "自选股", "手动输入"],
        horizontal=True,
    )
    if source == "最近盘后候选":
        base_codes, names = post_codes, post_names
    elif source == "最近盘前候选":
        base_codes, names = pre_codes, pre_names
    elif source == "自选股":
        base_codes, names = watch_codes, watch_names
    else:
        base_codes, names = [], {}

    with st.form("backtest_form"):
        manual = st.text_area(
            "手动股票池（逗号/空格分隔，最多 60 只）" if source == "手动输入" else "股票池预览（可修改）",
            value=", ".join(base_codes),
            height=90 if source != "手动输入" else 120,
        )
        c1, c2, c3 = st.columns(3)
        start_date = c1.date_input("开始日期", value=default_start)
        end_date = c2.date_input("结束日期", value=default_end)
        initial_cash = c3.number_input("初始资金（元）", value=float(settings.backtest_initial_cash), min_value=10_000.0, step=10_000.0)
        c4, c5, c6 = st.columns(3)
        max_positions = c4.slider("最大持仓数", 1, 10, 5)
        holding_days = c5.slider("持有交易日", 1, 10, 3)
        rebalance_days = c6.slider("调仓间隔（交易日）", 1, 5, 1)
        c7, c8 = st.columns(2)
        min_score = c7.slider("最低评分", 0, 90, 55)
        max_position_pct = c8.slider("单票最大仓位 %", 10, 50, 30)
        run = st.form_submit_button("开始回测", type="primary", width="stretch")

    if run:
        codes = _parse_codes(manual)
        if not codes:
            st.error("股票池为空或没有有效 6 位代码。")
            return
        if len(codes) > 60:
            st.error("单次最多 60 只，请缩减股票池。")
            return
        if start_date >= end_date:
            st.error("开始日期必须早于结束日期。")
            return
        config = BacktestConfig(
            start_date=start_date,
            end_date=end_date,
            initial_cash=float(initial_cash),
            max_positions=int(max_positions),
            holding_days=int(holding_days),
            rebalance_days=int(rebalance_days),
            min_score=float(min_score),
            min_avg_amount=settings.min_avg_amount,
            min_price=settings.min_price,
            max_price=settings.max_price,
            max_position_pct=max_position_pct / 100,
            commission_rate=settings.commission_rate,
            min_commission=settings.min_commission,
            stamp_tax_sell=settings.stamp_tax_sell,
            transfer_fee=settings.transfer_fee,
            slippage_bps=settings.slippage_bps,
        )
        with st.status("回测运行中：加载基准与个股日线…", expanded=True) as status:
            try:
                engine = BacktestEngine(get_provider(), settings)
                result = engine.run(
                    codes,
                    config,
                    names=names,
                    progress=lambda msg: status.update(label=msg),
                )
                st.session_state["backtest_result"] = result
                status.update(label="回测完成", state="complete")
            except Exception as exc:  # noqa: BLE001
                status.update(label="回测失败", state="error")
                st.error(f"回测失败：{type(exc).__name__}: {exc}")
                return

    result: BacktestResult | None = st.session_state.get("backtest_result")
    if not result:
        st.info(
            "默认使用最近盘后候选作为股票池。候选数量少时回测统计意义有限；"
            "结果会披露幸存者偏差、复权与涨跌停近似等限制。"
        )
        return

    metrics = result.metrics
    st.divider()
    st.markdown("### 核心指标")
    c1, c2, c3 = st.columns(3)
    c1.metric("策略收益", fmt_pct(metrics.get("total_return")))
    c2.metric("基准收益（沪深300）", fmt_pct(metrics.get("benchmark_return")))
    c3.metric("超额收益", fmt_pct(metrics.get("excess_return")))
    c4, c5, c6 = st.columns(3)
    c4.metric("最大回撤", fmt_pct(metrics.get("max_drawdown")))
    c5.metric("胜率", fmt_pct(metrics.get("win_rate")))
    c6.metric("已平仓交易", f"{metrics.get('trade_count', 0)} 笔")
    c7, c8, c9 = st.columns(3)
    c7.metric("年化收益", fmt_pct(metrics.get("annualized_return")))
    c8.metric("夏普（无风险=0）", fmt(metrics.get("sharpe")))
    c9.metric("期末权益", f"{fmt(metrics.get('final_equity'))} 元")
    st.caption(
        f"回测区间共 {metrics.get('trading_days')} 个交易日 · 总手续费 {fmt(metrics.get('total_fees'))} 元 · "
        f"未平仓 {metrics.get('open_positions', 0)} 只 · 因涨停/停牌错过买入 "
        f"{metrics.get('blocked_buys_limit_up', 0)}/{metrics.get('blocked_buys_suspended', 0)} 次"
    )
    if not result.equity_curve.empty:
        st.plotly_chart(
            equity_chart(result.equity_curve),
            width="stretch",
            config=CHART_CONFIG,
        )
    st.markdown("### 交易记录")
    if result.trades.empty:
        st.warning("回测期内没有完成任何交易。这可能是阈值过严或数据不足，不是收益证明。")
    else:
        display = result.trades.copy()
        st.dataframe(display, hide_index=True, width="stretch")
        st.download_button(
            "下载交易记录 CSV",
            data=display.to_csv(index=False).encode("utf-8-sig"),
            file_name="astockquant_backtest_trades.csv",
            mime="text/csv",
        )
    warnings = result.warnings
    if warnings:
        st.warning("请先阅读以下回测限制，再解读收益指标。")
        for warning in warnings:
            st.markdown(f"- {warning}")
    with st.expander("已实现 / 未完全实现的市场规则"):
        st.markdown("**已实现**")
        for rule in result.implemented_rules:
            st.markdown(f"- {rule}")
        st.markdown("**未完全实现（不能假装已实现）**")
        for limitation in result.limitations:
            st.markdown(f"- {limitation}")
        st.caption("回测不构成未来收益承诺。历史表现不能保证未来结果。")


# ---------------------------------------------------------------------------
# 风险与系统
# ---------------------------------------------------------------------------
def page_risk_system() -> None:
    page_header(
        "风险管理与系统状态",
        "候选风险参考、仓位计算器、数据源自检、交易日历与独立定时任务说明。",
        "RISK & OPS",
    )
    settings = get_settings()
    tabs = st.tabs(["候选风险参考", "仓位计算器", "数据源与定时任务"])

    with tabs[0]:
        mode = st.radio("结果来源", ["postmarket", "premarket"], horizontal=True, format_func=lambda x: "盘后" if x == "postmarket" else "盘前")
        result = load_result(mode)
        if not result:
            st.info("还没有选股结果。请先在盘前/盘后页面运行任务。")
        else:
            candidates = result.get("candidates", [])
            if not candidates:
                st.warning("最近结果没有候选。")
            else:
                rows = []
                for c in candidates:
                    plan = c.get("risk_plan") or {}
                    zone = plan.get("observation_zone") or [None, None]
                    rows.append(
                        {
                            "代码": c.get("code"),
                            "名称": c.get("name"),
                            "评分": c.get("score"),
                            "收盘": c.get("close"),
                            "观察低": zone[0],
                            "观察高": zone[1],
                            "止损": plan.get("stop_loss"),
                            "目标": plan.get("target"),
                            "盈亏比": plan.get("risk_reward_ratio"),
                            "风险等级": plan.get("risk_level"),
                            "数据日期": plan.get("data_date"),
                        }
                    )
                st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")
                st.caption(f"信号日 {result.get('signal_date')}，目标日 {result.get('target_date')}，结果生成于 {fmt_dt(result.get('generated_at'))}。")
                st.caption("观察区间/止损/目标均由信号日收盘价、ATR、20日支撑压力按固定规则计算，不保证成交或收益。")
    with tabs[1]:
        candidates = []
        for mode in ("postmarket", "premarket"):
            payload = load_result(mode)
            if payload:
                for c in payload.get("candidates", []):
                    candidates.append((mode, c))
        options = [f"{c['code']} {c.get('name', '')} ({'盘后' if m == 'postmarket' else '盘前'})" for m, c in candidates]
        selected = st.selectbox("从候选中带入（可选）", ["手动输入"] + options) if options else "手动输入"
        chosen = None
        if selected != "手动输入":
            idx = options.index(selected)
            chosen = candidates[idx][1]
        with st.form("position_form"):
            c1, c2 = st.columns(2)
            account = c1.number_input("账户总资产（元）", min_value=1000.0, value=100000.0, step=10000.0)
            risk_pct = c2.number_input("单笔最大亏损占总资产 %", min_value=0.1, max_value=10.0, value=1.0, step=0.1)
            c3, c4 = st.columns(2)
            default_entry = float(chosen.get("close")) if chosen and chosen.get("close") else 10.0
            default_stop = 0.0
            if chosen and chosen.get("risk_plan", {}).get("stop_loss"):
                default_stop = float(chosen["risk_plan"]["stop_loss"])
            entry = c3.number_input("买入参考价", min_value=0.01, value=default_entry, step=0.01, format="%.2f")
            stop = c4.number_input("止损参考价", min_value=0.01, value=default_stop if default_stop > 0 else round(default_entry * 0.92, 2), step=0.01, format="%.2f")
            calc = st.form_submit_button("计算参考仓位", type="primary", width="stretch")
        if calc:
            try:
                sizing = position_size(account_value=account, risk_pct=risk_pct, entry_price=entry, stop_price=stop)
                c1, c2, c3 = st.columns(3)
                c1.metric("参考股数", f"{sizing['suggested_shares']:,} 股")
                c2.metric("参考市值", f"{sizing['position_value']:,.2f} 元")
                c3.metric("最大亏损", f"{sizing['max_loss_amount']:,.2f} 元")
                st.caption(
                    f"约占总资产 {sizing['position_pct']}% · 实际最大亏损比例 {sizing['max_loss_pct']}% · "
                    f"每股风险 {sizing['per_share_risk']} 元"
                )
                st.caption(sizing["rule"])
            except ValueError as exc:
                st.error(str(exc))
        st.warning("A 股 T+1、涨跌停和停牌都可能让参考仓位无法按计划成交；本工具不保证收益。")

    with tabs[2]:
        col1, col2 = st.columns(2)
        with col1:
            st.markdown("### 数据源自检")
            st.caption("启动时已检查指数接口与交易日历；这里可执行完整自检。")
            if st.button("执行完整自检", width="stretch"):
                st.session_state["health_force"] = True
            try:
                force_health = bool(st.session_state.pop("health_force", False))
                if force_health:
                    health = MarketDataProvider().health_check(force=True)
                    cached_health.clear()
                else:
                    health = cached_health()
                for check in health:
                    if check.get("ok"):
                        st.success(
                            f"{check['name']} · {check.get('latency_ms')}ms · {check.get('data_date') or ''}"
                        )
                    else:
                        st.error(f"{check['name']} · {check.get('message')}")
                if any(
                    c.get("name") == "Baostock 个股日线（备用）" and c.get("ok")
                    for c in health
                ) and not all(
                    c.get("ok")
                    for c in health
                    if c.get("name") in {"AKShare 个股日线", "东方财富个股日线（直连）"}
                ):
                    st.info(
                        "AKShare/东方财富个股日线当前不可用，系统已自动切换到 Baostock "
                        "真实日线（含真实成交额）；选股结果会附加数据源警告。"
                    )
            except Exception as exc:  # noqa: BLE001
                st.error(f"自检失败：{type(exc).__name__}: {exc}")
        with col2:
            st.markdown("### 交易日历与任务")
            try:
                cal = load_calendar()
                st.markdown(
                    f"- 来源：`{cal.source}`\n- 覆盖至：`{cal.dates[-1]}`\n"
                    f"- 可靠：`{cal.reliable}`\n- 盘前截止：`{PREMARKET_DEADLINE}`"
                )
                if cal.warning:
                    st.warning(cal.warning)
            except Exception as exc:  # noqa: BLE001
                st.error(f"日历不可用：{exc}")
            runs = recent_job_runs(settings)
            if runs:
                for run in runs[-4:]:
                    st.caption(
                        f"{run.get('started_at')} · {run.get('mode')} · {run.get('status')} "
                        f"{run.get('error', '')}"
                    )
            st.caption(f"数据目录：{settings.data_dir}")

        st.divider()
        st.markdown("### 独立定时任务（不依赖 Streamlit 进程）")
        st.code(
            "# Windows PowerShell\n"
            ".\\.venv\\Scripts\\python.exe scheduler.py --job premarket --once\n"
            ".\\.venv\\Scripts\\python.exe scheduler.py --job postmarket --once\n"
            ".\\.venv\\Scripts\\python.exe scheduler.py --serve\n\n"
            "# 云端常驻或外部调度器只是调用 --once 命令\n"
            "# 盘前 08:15（Asia/Shanghai），盘后 15:30（Asia/Shanghai）",
            language="powershell",
        )
        st.caption(
            "云平台若会休眠或禁用后台进程，请用 GitHub Actions / 云厂商 Cron / cron-job.org 调用 "
            "scheduler.py --once；必须让结果目录位于持久化卷。"
        )


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------
def main() -> None:
    inject_css()
    require_auth()
    page = render_sidebar()
    if page == "市场总览":
        page_market()
    elif page == "盘前选股":
        page_screening("premarket")
    elif page == "盘后选股":
        page_screening("postmarket")
    elif page == "个股分析":
        page_stock_analysis()
    elif page == "自选股":
        page_watchlist()
    elif page == "策略回测":
        page_backtest()
    elif page == "风险与系统":
        page_risk_system()
    st.markdown(
        f"<div class='tiny' style='margin-top:2rem;'>AStockQuant v{APP_VERSION} · "
        f"数据时间与运行时间严格区分 · 仅用于研究与风险管理，不构成投资建议。</div>",
        unsafe_allow_html=True,
    )


if __name__ == "__main__":
    main()
