"""TTL分级策略：按数据类型返回缓存有效期"""
from __future__ import annotations

from datetime import datetime, time as dtime, timedelta, timezone

# 上海时区 UTC+8
SH_TZ = timezone(timedelta(hours=8))

# 分钟级周期（盘中数据变化快）
MINUTE_PERIODS = {"MIN_1", "MIN_5", "MIN_15", "MIN_30", "MIN_60"}


def ttl_for(
    category: str,
    *,
    period: str | None = None,
    adjust: str | None = None,
    date: int | None = None,
) -> float:
    """返回 TTL（秒）。0=不过期（永久），负值=立即失效。"""
    if category == "quote":
        return 5.0
    if category == "kline":
        if period in MINUTE_PERIODS:
            return 60.0
        if adjust in ("QFQ", "HFQ"):
            return 86400.0
        return _until_next_midnight()   # 日线当天（次日00:30失效）
    if category == "transaction":
        if date is None or date == _today_ymd():
            return 60.0   # 当日逐笔变化快
        return 0.0        # 历史逐笔永久
    if category == "minute":
        return 60.0
    if category in ("list", "count", "xdxr", "finance"):
        return 86400.0
    return 60.0


def _until_next_midnight() -> float:
    """距次日00:30（上海时区）的秒数，12h兜底。"""
    now = datetime.now(SH_TZ)
    nxt = datetime.combine(now.date() + timedelta(days=1), dtime(0, 30), tzinfo=SH_TZ)
    ttl = (nxt - now).total_seconds()
    return max(0.0, min(ttl, 12 * 3600))


def _today_ymd() -> int:
    now = datetime.now(SH_TZ)
    return now.year * 10000 + now.month * 100 + now.day
