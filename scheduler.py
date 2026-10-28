"""独立定时任务入口（不依赖 Streamlit 进程）。

用法：
    python scheduler.py --job premarket --once
    python scheduler.py --job postmarket --once
    python scheduler.py --job health --once
    python scheduler.py --serve

`--serve` 使用 APScheduler，时区固定 Asia/Shanghai：
* 盘前：交易日 08:15
* 盘后：交易日 15:30
每次执行都会重新核验真实交易日历；非交易日直接跳过。

云端务必让本进程常驻（Docker worker / VPS systemd），或者由外部调度器
（GitHub Actions、cron-job.org、云厂商定时任务）调用 `--once`。
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, time
from logging.handlers import RotatingFileHandler
from typing import Any

from config import APP_VERSION, Settings, get_settings
from data_provider import DataProviderError, MarketDataProvider
from market_calendar import (
    PREMARKET_DEADLINE,
    is_trade_day,
    latest_completed_trade_day,
    load_calendar,
    next_trade_day,
    target_session_for_premarket,
)
from stock_screener import StockScreener
from storage import JobLock, StorageError, load_result, recent_job_runs, save_job_run

PREMARKET_TIME = time(8, 15)
POSTMARKET_TIME = time(15, 30)
LOGGER = logging.getLogger("astockquant.scheduler")


def setup_logging(settings: Settings) -> None:
    log_dir = settings.data_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    if not LOGGER.handlers:
        file_handler = RotatingFileHandler(
            log_dir / "scheduler.log", maxBytes=5_000_000, backupCount=5, encoding="utf-8"
        )
        file_handler.setFormatter(formatter)
        stream = logging.StreamHandler(sys.stdout)
        stream.setFormatter(formatter)
        LOGGER.addHandler(file_handler)
        LOGGER.addHandler(stream)
    LOGGER.setLevel(logging.INFO)


def _parse_now(value: str | None, settings: Settings) -> datetime:
    if not value:
        return datetime.now(settings.timezone)
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=settings.timezone)
    return dt.astimezone(settings.timezone)


def _scheduled_gate(mode: str, now: datetime, settings: Settings) -> tuple[bool, str]:
    today = now.date()
    trade, cal = is_trade_day(today)
    if not trade:
        return False, f"{today} 不是 A 股交易日（{cal.source}），任务跳过"
    if mode == "premarket" and now.time() >= PREMARKET_DEADLINE:
        return True, f"盘前任务在 {now.strftime('%H:%M')} 运行，已晚于 09:00；结果仍严格使用上一完整交易日数据"
    if mode == "postmarket" and now.time() < time(15, 10):
        return True, f"盘后任务在收盘结算前运行（{now.strftime('%H:%M')}），最近完整交易日可能仍是上一交易日"
    return True, f"{today} 是交易日，任务可运行"


def run_screening_job(
    mode: str,
    *,
    force: bool = False,
    signal_date: str | None = None,
    now: datetime | None = None,
    settings: Settings | None = None,
    run_context: str = "scheduler",
    progress=print,
) -> dict[str, Any]:
    settings = settings or get_settings()
    now = now or datetime.now(settings.timezone)
    if mode not in {"premarket", "postmarket"}:
        raise ValueError("mode 必须为 premarket 或 postmarket")
    if signal_date is None:
        allowed, gate_message = _scheduled_gate(mode, now, settings)
        progress(gate_message)
        if not allowed:
            return {"status": "skipped", "reason": gate_message}
    else:
        gate_message = f"使用显式信号日 {signal_date}"

    record: dict[str, Any] = {
        "mode": mode,
        "started_at": datetime.now(settings.timezone).isoformat(timespec="seconds"),
        "run_context": run_context,
        "now": now.isoformat(timespec="seconds"),
        "status": "running",
    }
    try:
        timing_warnings: list[str] = []
        if gate_message.startswith(("盘前任务在", "盘后任务在")):
            timing_warnings.append(gate_message)
        with JobLock(mode, settings):
            screener = StockScreener(settings=settings)
            payload = screener.run(
                mode,
                now=now,
                signal_date=signal_date,
                force=force,
                progress=progress,
                run_context=run_context,
                extra_warnings=timing_warnings,
            )
        record.update(
            {
                "status": "success",
                "finished_at": datetime.now(settings.timezone).isoformat(timespec="seconds"),
                "signal_date": payload.get("signal_date"),
                "target_date": payload.get("target_date"),
                "candidate_count": len(payload.get("candidates", [])),
                "generated_at": payload.get("generated_at"),
                "warning_count": len(payload.get("warnings", [])),
                "gate_message": gate_message,
            }
        )
        save_job_run(record, settings)
        return {"status": "success", "result": payload, "run": record}
    except Exception as exc:
        record.update(
            {
                "status": "error",
                "finished_at": datetime.now(settings.timezone).isoformat(timespec="seconds"),
                "error": f"{type(exc).__name__}: {exc}",
                "gate_message": gate_message,
            }
        )
        save_job_run(record, settings)
        LOGGER.exception("%s 任务失败", mode)
        raise


def print_result_summary(payload: dict[str, Any]) -> None:
    if payload.get("status") == "skipped":
        print(f"[跳过] {payload.get('reason')}")
        return
    if "result" not in payload:
        return
    data = payload["result"]
    print("=" * 78)
    print(
        f"{data.get('mode_label')} | 信号日 {data.get('signal_date')} | "
        f"目标日 {data.get('target_date')} | 生成于 {data.get('generated_at')}"
    )
    print("=" * 78)
    candidates = data.get("candidates", [])
    if not candidates:
        print("本次没有达到阈值的候选。系统未使用模拟数据补足。")
    for item in candidates:
        print(
            f"{item['rank']:>2}. {item['code']} {item['name']:<8} "
            f"评分 {item['score']:.1f}  收盘 {item['close']:.2f}  "
            f"观察 {item['risk_plan']['observation_zone'][0]:.2f}-"
            f"{item['risk_plan']['observation_zone'][1]:.2f}  "
            f"止损 {item['risk_plan']['stop_loss']:.2f}"
        )
        print("    理由：" + "；".join(item.get("reasons", [])[:3]))
    stats = data.get("selection_stats", {})
    print(
        f"\n股票池 {stats.get('universe_size')}，成功日线 {stats.get('history_success')}，"
        f"候选 {stats.get('selected')}，耗时 {stats.get('elapsed_seconds')}s"
    )
    warnings = data.get("warnings", [])
    if warnings:
        print("\n数据警告：")
        for warning in warnings[:8]:
            print(f"  ! {warning}")
    print(f"\n结果文件：{data.get('_saved_path', '')}")


def command_health(settings: Settings) -> int:
    provider = MarketDataProvider(settings)
    print(f"AStockQuant {APP_VERSION} 数据源自检 @ {datetime.now(settings.timezone)}")
    checks = provider.health_check(force=True)
    for check in checks:
        mark = "OK" if check.get("ok") else "FAIL"
        print(
            f"[{mark:4}] {check['name']:<26} {check['latency_ms']:>6}ms "
            f"{check.get('data_date') or ''} {check.get('message', '')}"
        )
    by_name = {str(c.get("name")): c for c in checks}
    stock_history_ok = any(
        bool(by_name.get(name, {}).get("ok"))
        for name in ("AKShare 个股日线", "东方财富个股日线（直连）", "Baostock 个股日线（备用）")
    )
    core_ok = (
        stock_history_ok
        and bool(by_name.get("新浪指数行情", {}).get("ok"))
        and bool(by_name.get("新浪全市场快照", {}).get("ok"))
        and bool(by_name.get("交易日历", {}).get("ok"))
    )
    if not core_ok:
        print("\n结论：核心数据链路不完整，选股任务不应在未知数据状态下运行。")
        return 2
    unavailable = [c["name"] for c in checks if not c.get("ok")]
    if unavailable:
        print(f"\n结论：核心数据可用；以下可选/首选源不可用，已自动降级：{'、'.join(unavailable)}")
    else:
        print("\n结论：全部数据源可用。")
    return 0


def command_status(settings: Settings) -> int:
    calendar = load_calendar()
    print(f"AStockQuant {APP_VERSION} 状态")
    print(f"时区：{settings.timezone}")
    print(f"数据目录：{settings.data_dir}")
    print(f"交易日历：{calendar.source}，覆盖至 {calendar.dates[-1]}，reliable={calendar.reliable}")
    if calendar.warning:
        print(f"警告：{calendar.warning}")
    for mode in ("premarket", "postmarket"):
        result = load_result(mode)
        if not result:
            print(f"{mode}: 尚无结果")
            continue
        print(
            f"{mode}: signal={result.get('signal_date')} target={result.get('target_date')} "
            f"candidates={len(result.get('candidates', []))} generated={result.get('generated_at')}"
        )
    runs = recent_job_runs(settings)
    if runs:
        print("\n最近任务：")
        for run in runs[-5:]:
            print(
                f"  {run.get('started_at')} {run.get('mode')} {run.get('status')} "
                f"{run.get('error', '')}"
            )
    return 0


def command_serve(settings: Settings) -> int:
    try:
        from apscheduler.schedulers.blocking import BlockingScheduler
        from apscheduler.triggers.cron import CronTrigger
    except ImportError as exc:
        print("缺少 APScheduler，请运行 pip install -r requirements.txt", file=sys.stderr)
        return 2

    scheduler = BlockingScheduler(timezone=str(settings.timezone))
    tz = str(settings.timezone)
    scheduler.add_job(
        lambda: _serve_run("premarket", settings),
        CronTrigger(day_of_week="mon-fri", hour=PREMARKET_TIME.hour, minute=PREMARKET_TIME.minute, timezone=tz),
        id="premarket",
        name="盘前选股",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=1800,
    )
    scheduler.add_job(
        lambda: _serve_run("postmarket", settings),
        CronTrigger(day_of_week="mon-fri", hour=POSTMARKET_TIME.hour, minute=POSTMARKET_TIME.minute, timezone=tz),
        id="postmarket",
        name="盘后选股",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=3600,
    )
    # 失败重试：成功生成后重试任务会检查结果并直接跳过。
    scheduler.add_job(
        lambda: _serve_run("premarket", settings, retry=True),
        CronTrigger(day_of_week="mon-fri", hour=8, minute=40, timezone=tz),
        id="premarket_retry",
        name="盘前选股重试",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=900,
    )
    scheduler.add_job(
        lambda: _serve_run("postmarket", settings, retry=True),
        CronTrigger(day_of_week="mon-fri", hour=16, minute=15, timezone=tz),
        id="postmarket_retry",
        name="盘后选股重试",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=1800,
    )
    LOGGER.info(
        "调度器启动：盘前 %s（重试 08:40），盘后 %s（重试 16:15），时区 %s",
        PREMARKET_TIME.strftime("%H:%M"),
        POSTMARKET_TIME.strftime("%H:%M"),
        tz,
    )
    print(
        f"调度器运行中：盘前 {PREMARKET_TIME:%H:%M}（重试 08:40），"
        f"盘后 {POSTMARKET_TIME:%H:%M}（重试 16:15），{tz}"
    )
    print("按 Ctrl+C 停止。请让此进程在云端常驻，而不是只运行 Streamlit。")
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        LOGGER.info("调度器停止")
    return 0


def _serve_run(mode: str, settings: Settings, retry: bool = False) -> None:
    try:
        if retry and _already_generated(mode, settings):
            LOGGER.info("%s 重试检查：本次目标结果已存在，跳过", mode)
            print(f"[跳过] {mode} 已有本次结果，重试任务不再重复请求数据源。")
            return
        result = run_screening_job(mode, settings=settings, run_context="apscheduler")
        if result.get("status") == "success":
            print_result_summary(result)
    except Exception:  # noqa: BLE001 - 定时任务不能因一次失败退出调度器
        LOGGER.exception("定时任务 %s 失败，调度器继续运行", mode)


def _already_generated(mode: str, settings: Settings) -> bool:
    """仅用于重试任务：已有匹配 signal/target 的结果则跳过。"""
    payload = load_result(mode, settings=settings)
    if not payload:
        return False
    now = datetime.now(settings.timezone)
    try:
        signal, _ = latest_completed_trade_day(now=now, settings=settings)
        if mode == "premarket":
            target, _ = target_session_for_premarket(now=now, settings=settings)
        else:
            target = next_trade_day(signal)
    except Exception:  # noqa: BLE001
        return False
    return (
        payload.get("signal_date") == signal.isoformat()
        and payload.get("target_date") == target.isoformat()
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="AStockQuant 独立定时任务入口")
    parser.add_argument("--job", choices=["premarket", "postmarket", "health", "status"], help="任务类型")
    parser.add_argument("--once", action="store_true", help="执行一次后退出（默认行为）")
    parser.add_argument("--serve", action="store_true", help="启动常驻调度器，按交易日历执行")
    parser.add_argument("--force", action="store_true", help="忽略股票池缓存并强制刷新日线")
    parser.add_argument("--signal-date", help="指定信号日 YYYY-MM-DD（用于补跑/验证）")
    parser.add_argument("--now", help="覆盖当前时间（ISO 8601，用于测试）")
    return parser


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    settings = get_settings()
    setup_logging(settings)
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.serve:
        return command_serve(settings)
    if args.job == "health":
        return command_health(settings)
    if args.job == "status":
        return command_status(settings)
    if not args.job:
        parser.print_help()
        return 2
    now = _parse_now(args.now, settings)
    try:
        result = run_screening_job(
            args.job,
            force=args.force,
            signal_date=args.signal_date,
            now=now,
            settings=settings,
            run_context="cli",
        )
        print_result_summary(result)
        if result.get("status") == "skipped":
            return 0
        return 0
    except (DataProviderError, StorageError, ValueError) as exc:
        print(f"\n任务失败：{exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001
        LOGGER.exception("未预期错误")
        print(f"\n任务失败：{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
