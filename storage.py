"""本地文件存储：结果、缓存、自选股与任务锁。

JSON 是唯一的结果交换格式，方便云端挂载卷持久化、GitHub Actions 归档
以及 Streamlit 只读加载。写文件采用临时文件 + 原子替换。
"""
from __future__ import annotations

import json
import math
import os
import time
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable

import numpy as np
import pandas as pd

from config import Settings, get_settings

RESULT_MODES = ("premarket", "postmarket")


class StorageError(RuntimeError):
    pass


def to_jsonable(value: Any) -> Any:
    """递归转换 numpy / pandas 标量，避免 json.dumps 失败。"""
    if value is None:
        return None
    if isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return None if not math.isfinite(value) else round(value, 6)
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating,)):
        f = float(value)
        return None if not math.isfinite(f) else round(f, 6)
    if isinstance(value, (np.bool_,)):
        return bool(value)
    if isinstance(value, (pd.Timestamp, datetime)):
        if pd.isna(value):
            return None
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): to_jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [to_jsonable(v) for v in value]
    if isinstance(value, np.ndarray):
        return [to_jsonable(v) for v in value.tolist()]
    if pd.isna(value):
        return None
    return str(value)


def atomic_write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
    tmp.write_text(
        json.dumps(to_jsonable(payload), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    os.replace(tmp, path)


def read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError) as exc:
        raise StorageError(f"无法读取 {path}: {exc}") from exc


# ---------------------------------------------------------------------------
# 任务结果
# ---------------------------------------------------------------------------
def result_path(mode: str, signal_date: str | date, settings: Settings | None = None) -> Path:
    if mode not in RESULT_MODES:
        raise ValueError(f"未知任务类型: {mode}")
    settings = settings or get_settings()
    d = signal_date.isoformat() if isinstance(signal_date, date) else str(signal_date)
    return settings.result_dir / d / f"{mode}.json"


def save_result(payload: dict[str, Any], settings: Settings | None = None) -> Path:
    settings = settings or get_settings()
    mode = str(payload["mode"])
    signal_date = str(payload["signal_date"])
    path = result_path(mode, signal_date, settings)
    atomic_write_json(path, payload)
    # 供其他程序读取的轻量索引；失败不影响主结果。
    index_path = settings.result_dir / f"latest_{mode}.json"
    atomic_write_json(
        index_path,
        {
            "mode": mode,
            "signal_date": signal_date,
            "target_date": payload.get("target_date"),
            "generated_at": payload.get("generated_at"),
            "candidate_count": len(payload.get("candidates", [])),
            "path": str(path.relative_to(settings.data_dir)),
        },
    )
    return path


def load_result(
    mode: str,
    signal_date: str | date | None = None,
    settings: Settings | None = None,
) -> dict[str, Any] | None:
    settings = settings or get_settings()
    if signal_date is None:
        latest = read_json(settings.result_dir / f"latest_{mode}.json")
        if latest:
            path = settings.data_dir / str(latest.get("path", ""))
            if path.exists():
                return read_json(path)
        dates = list_result_dates(mode, settings)
        if not dates:
            return None
        signal_date = dates[0]
    path = result_path(mode, signal_date, settings)
    return read_json(path)


def list_result_dates(mode: str, settings: Settings | None = None) -> list[str]:
    settings = settings or get_settings()
    if not settings.result_dir.exists():
        return []
    dates: list[str] = []
    for child in settings.result_dir.iterdir():
        if child.is_dir() and (child / f"{mode}.json").exists():
            dates.append(child.name)
    return sorted(dates, reverse=True)


def save_job_run(record: dict[str, Any], settings: Settings | None = None) -> None:
    settings = settings or get_settings()
    path = settings.state_dir / "job_runs.json"
    history = read_json(path) or []
    if not isinstance(history, list):
        history = []
    history.append(to_jsonable(record))
    atomic_write_json(path, history[-200:])


def recent_job_runs(settings: Settings | None = None) -> list[dict[str, Any]]:
    settings = settings or get_settings()
    raw = read_json(settings.state_dir / "job_runs.json")
    return raw if isinstance(raw, list) else []


# ---------------------------------------------------------------------------
# 自选股
# ---------------------------------------------------------------------------
def load_watchlist(settings: Settings | None = None) -> list[dict[str, str]]:
    settings = settings or get_settings()
    raw = read_json(settings.watchlist_path)
    if not raw:
        return []
    items = raw.get("items", []) if isinstance(raw, dict) else raw
    result: list[dict[str, str]] = []
    for item in items:
        if isinstance(item, str):
            code = item.strip()
            name = ""
        elif isinstance(item, dict):
            code = str(item.get("code", "")).strip()
            name = str(item.get("name", "")).strip()
        else:
            continue
        if code and code not in {x["code"] for x in result}:
            result.append({"code": code, "name": name})
    return result


def save_watchlist(items: Iterable[dict[str, str]], settings: Settings | None = None) -> None:
    settings = settings or get_settings()
    cleaned: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in items:
        code = str(item.get("code", "")).strip()
        name = str(item.get("name", "")).strip()
        if code and code not in seen:
            cleaned.append({"code": code, "name": name})
            seen.add(code)
    atomic_write_json(
        settings.watchlist_path,
        {"updated_at": datetime.now(settings.timezone).isoformat(timespec="seconds"), "items": cleaned},
    )


# ---------------------------------------------------------------------------
# 文件缓存
# ---------------------------------------------------------------------------
def cache_age_seconds(path: Path) -> float:
    try:
        return max(0.0, time.time() - path.stat().st_mtime)
    except OSError:
        return float("inf")


def save_history_cache(
    code: str,
    df: pd.DataFrame,
    meta: dict[str, Any],
    settings: Settings | None = None,
) -> None:
    settings = settings or get_settings()
    folder = settings.history_dir / code
    folder.mkdir(parents=True, exist_ok=True)
    tmp_data = folder / f"daily.{os.getpid()}.tmp"
    df.to_csv(tmp_data, index=False, encoding="utf-8")
    os.replace(tmp_data, folder / "daily.csv")
    atomic_write_json(folder / "meta.json", meta)


def load_history_cache(
    code: str, settings: Settings | None = None
) -> tuple[pd.DataFrame, dict[str, Any]] | None:
    settings = settings or get_settings()
    folder = settings.history_dir / code
    data_path = folder / "daily.csv"
    meta_path = folder / "meta.json"
    if not data_path.exists():
        return None
    try:
        df = pd.read_csv(data_path)
        meta = read_json(meta_path) or {}
        return df, meta
    except (OSError, ValueError, pd.errors.ParserError):
        return None


def save_universe(signal_date: str, payload: dict[str, Any], settings: Settings | None = None) -> Path:
    settings = settings or get_settings()
    path = settings.universe_dir / f"{signal_date}.json"
    atomic_write_json(path, payload)
    atomic_write_json(
        settings.universe_dir / "latest.json",
        {"signal_date": signal_date, "path": path.name},
    )
    return path


def load_universe(
    signal_date: str | None = None, settings: Settings | None = None
) -> dict[str, Any] | None:
    settings = settings or get_settings()
    if signal_date:
        return read_json(settings.universe_dir / f"{signal_date}.json")
    latest = read_json(settings.universe_dir / "latest.json")
    if not latest:
        return None
    return read_json(settings.universe_dir / str(latest.get("path")))


# ---------------------------------------------------------------------------
# 任务锁
# ---------------------------------------------------------------------------
def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            import ctypes

            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            handle = ctypes.windll.kernel32.OpenProcess(
                PROCESS_QUERY_LIMITED_INFORMATION, False, pid
            )
            if handle:
                ctypes.windll.kernel32.CloseHandle(handle)
                return True
            # 拒绝访问通常说明进程存在但属于其他用户。
            return ctypes.windll.kernel32.GetLastError() == 5
        except Exception:  # noqa: BLE001
            return True
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


@dataclass
class JobLock(AbstractContextManager):
    mode: str
    settings: Settings
    stale_seconds: int = 4 * 3600
    path: Path | None = None
    acquired: bool = False

    def __enter__(self) -> "JobLock":
        self.settings.ensure_dirs()
        self.path = self.settings.lock_dir / f"{self.mode}.lock"
        if self.path.exists():
            age = cache_age_seconds(self.path)
            lock_pid = 0
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                lock_pid = int(raw.get("pid", 0))
            except (OSError, ValueError, TypeError):
                pass
            owner_alive = _pid_alive(lock_pid)
            if age < self.stale_seconds and owner_alive:
                raise StorageError(
                    f"{self.mode} 任务已有进程在运行（锁文件 {self.path.name}，"
                    f"PID {lock_pid}，{int(age)} 秒前创建）"
                )
            self.path.unlink(missing_ok=True)
        try:
            fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError as exc:
            raise StorageError(f"{self.mode} 任务锁竞争失败") from exc
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(
                json.dumps(
                    {
                        "pid": os.getpid(),
                        "mode": self.mode,
                        "created_at": datetime.now(self.settings.timezone).isoformat(timespec="seconds"),
                    },
                    ensure_ascii=False,
                )
            )
        self.acquired = True
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self.path and self.path.exists():
            self.path.unlink(missing_ok=True)
        self.acquired = False
