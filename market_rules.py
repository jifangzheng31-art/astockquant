"""A 股交易规则中可安全近似的部分。

涨跌幅限制是近似值：历史 ST 状态、新股上市初期、退市整理期等无法仅凭
代码/当前名称完整还原，调用方必须在结果中披露该限制。
"""
from __future__ import annotations


def price_limit_pct(code: str, name: str = "") -> float:
    code = str(code or "")
    if "ST" in str(name or "").upper():
        return 5.0
    if code.startswith(("300", "301", "688", "689")):
        return 20.0
    if code.startswith(("4", "8", "92")):
        return 30.0
    return 10.0


def limit_price(prev_close: float, code: str, name: str = "", down: bool = False) -> float:
    pct = price_limit_pct(code, name)
    factor = (1 - pct / 100) if down else (1 + pct / 100)
    return round(prev_close * factor + 1e-8, 2)
