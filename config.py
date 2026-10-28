"""AStockQuant 全局配置。

所有路径都相对于项目根目录，可通过 ASTOCK_DATA_DIR 将持久化数据迁移到
云平台挂载卷（例如 /data）。配置集中在此处，便于回测和评分参数调整。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo

PROJECT_ROOT = Path(__file__).resolve().parent
TIMEZONE = ZoneInfo("Asia/Shanghai")
APP_TITLE = "AStockQuant · A股短线选股系统"
APP_VERSION = "1.0.0"

# 100 分制，五项权重之和必须为 100。
SCORE_WEIGHTS: dict[str, float] = {
    "trend": 25.0,
    "volume_price": 20.0,
    "momentum": 20.0,
    "relative_strength": 20.0,
    "risk": 15.0,
}
SCORE_LABELS = {
    "trend": "趋势强度",
    "volume_price": "量价配合",
    "momentum": "动量表现",
    "relative_strength": "相对强度",
    "risk": "风险控制",
}


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _env_flag(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _configure_proxy_defaults() -> bool:
    """默认忽略 Windows 注册表里的失效系统代理。

    开发机常残留一个仅本机监听的代理，requests 会自动读取注册表并走代理，
    导致 AKShare 报 ProxyError，而同一 URL 直连正常。只有显式设置了
    HTTP(S)_PROXY 或 ASTOCK_USE_SYSTEM_PROXY=1 时才沿用系统代理。
    """
    use_proxy = _env_flag("ASTOCK_USE_SYSTEM_PROXY", False)
    if use_proxy:
        return True
    explicit_proxy = any(
        os.getenv(name)
        for name in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy")
    )
    if not explicit_proxy:
        os.environ.setdefault("NO_PROXY", "*")
        os.environ.setdefault("no_proxy", "*")
    return bool(explicit_proxy)


USE_SYSTEM_PROXY = _configure_proxy_defaults()


@dataclass(frozen=True)
class Settings:
    project_root: Path = PROJECT_ROOT
    data_dir: Path = field(
        default_factory=lambda: Path(os.getenv("ASTOCK_DATA_DIR", str(PROJECT_ROOT / "data")))
    )
    timezone: ZoneInfo = TIMEZONE

    # 数据请求
    request_timeout: int = field(default_factory=lambda: _env_int("ASTOCK_TIMEOUT", 20))
    request_retries: int = field(default_factory=lambda: _env_int("ASTOCK_RETRIES", 2))
    use_system_proxy: bool = USE_SYSTEM_PROXY
    history_days: int = field(default_factory=lambda: _env_int("ASTOCK_HISTORY_DAYS", 260))
    min_history_bars: int = 60
    snapshot_ttl_seconds: int = field(
        default_factory=lambda: _env_int("ASTOCK_SNAPSHOT_TTL", 120)
    )
    history_ttl_seconds: int = field(
        default_factory=lambda: _env_int("ASTOCK_HISTORY_TTL", 6 * 3600)
    )
    index_ttl_seconds: int = field(default_factory=lambda: _env_int("ASTOCK_INDEX_TTL", 30))
    calendar_ttl_seconds: int = 12 * 3600
    max_workers: int = field(default_factory=lambda: _env_int("ASTOCK_WORKERS", 6))

    # 选股范围。全市场五千多只股票逐只拉取日线不现实，因此先按成交额
    # 选取流动性靠前的股票，再对其做历史指标筛选；该限制会写入结果元数据。
    universe_size: int = field(default_factory=lambda: _env_int("ASTOCK_UNIVERSE_SIZE", 220))
    max_history_fetch: int = field(
        default_factory=lambda: _env_int("ASTOCK_MAX_HISTORY_FETCH", 160)
    )
    max_candidates: int = field(default_factory=lambda: _env_int("ASTOCK_MAX_CANDIDATES", 15))
    min_candidate_score: float = field(
        default_factory=lambda: _env_float("ASTOCK_MIN_SCORE", 55.0)
    )
    min_price: float = field(default_factory=lambda: _env_float("ASTOCK_MIN_PRICE", 2.0))
    max_price: float = field(default_factory=lambda: _env_float("ASTOCK_MAX_PRICE", 500.0))
    min_avg_amount: float = field(
        default_factory=lambda: _env_float("ASTOCK_MIN_AMOUNT", 80_000_000.0)
    )

    # 回测成本（单边）。费率均为比例，最低佣金按笔收取。
    commission_rate: float = 0.0003
    min_commission: float = 5.0
    stamp_tax_sell: float = 0.0005  # 2023-08-28 起卖出印花税 0.05%
    transfer_fee: float = 0.00001
    slippage_bps: float = 10.0
    backtest_initial_cash: float = 100_000.0

    @property
    def cache_dir(self) -> Path:
        return self.data_dir / "cache"

    @property
    def result_dir(self) -> Path:
        return self.data_dir / "results"

    @property
    def lock_dir(self) -> Path:
        return self.data_dir / "locks"

    @property
    def state_dir(self) -> Path:
        return self.data_dir / "state"

    @property
    def watchlist_path(self) -> Path:
        return self.state_dir / "watchlist.json"

    @property
    def universe_dir(self) -> Path:
        return self.cache_dir / "universe"

    @property
    def history_dir(self) -> Path:
        return self.cache_dir / "history"

    @property
    def calendar_path(self) -> Path:
        return self.cache_dir / "trade_calendar.json"

    @property
    def health_path(self) -> Path:
        return self.cache_dir / "data_source_health.json"

    def ensure_dirs(self) -> None:
        for path in (
            self.data_dir,
            self.cache_dir,
            self.result_dir,
            self.lock_dir,
            self.state_dir,
            self.universe_dir,
            self.history_dir,
        ):
            path.mkdir(parents=True, exist_ok=True)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    settings = Settings()
    settings.ensure_dirs()
    return settings
