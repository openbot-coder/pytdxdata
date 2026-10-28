"""K线「按日期区间」取数的路径选择：倒推快路径 vs 二分慢路径。

背景：二分要先探总根数（翻倍 6 次）再跳 log2(N) 次，1 分钟 K 线约 36 次串行
往返 = 4 秒级；而「取今天」「增量到最新」这类贴着右端的请求，倒推只要 1~3 次。
"""

import datetime
from unittest import mock

import pytest

from pytdxdata import TdxData
from pytdxdata.models import KlinePeriod, SecurityBar

NEWEST = datetime.datetime(2026, 10, 28, 15, 0)  # noqa: DTZ001 — 全项目用裸北京时间
TOTAL_BARS = 5_000


def _bar(when: datetime.datetime, close: float) -> SecurityBar:
    return SecurityBar(
        market=1, code="600000", open=10.0, high=10.1, low=9.9, close=close,
        vol=1000, amount=10000, year=when.year, month=when.month, day=when.day,
        hour=when.hour, minute=when.minute)


class _FakeConn:
    """标准通道语义替身：**offset 0 = 最新一根**，偏移越大越老。"""

    calls = 0

    def __init__(self, host="s1", port=7709, timeout=10.0):
        self.host = host
        self.alive_flag = True

    @property
    def alive(self):
        return self.alive_flag

    async def connect(self):
        pass

    async def close(self):
        self.alive_flag = False

    async def execute_pipeline(self, cmds, **kwargs):
        return [await self.execute(c) for c in cmds]

    async def execute(self, cmd):
        type(self).calls += 1
        if "GetSecurityBarsCmd" not in type(cmd).__name__:
            return []
        rows = []
        for off in range(cmd.start, min(cmd.start + cmd.count, TOTAL_BARS)):
            rows.append(_bar(NEWEST - datetime.timedelta(days=off),
                             10.0 + (TOTAL_BARS - off) * 0.01))
        return rows


class _AscendingConn(_FakeConn):
    """方向相反的替身（MAC/EX 语义）：offset 0 = **最老**一根。"""

    async def execute(self, cmd):
        type(self).calls += 1
        if "GetSecurityBarsCmd" not in type(cmd).__name__:
            return []
        rows = []
        base = datetime.datetime(2020, 1, 1)  # noqa: DTZ001
        for off in range(cmd.start, min(cmd.start + cmd.count, TOTAL_BARS)):
            rows.append(_bar(base + datetime.timedelta(days=off), 10.0 + off * 0.01))
        return rows


def _patched(conn_cls):
    return (
        mock.patch("pytdxdata.api.get_standard_hosts", return_value=["s1"]),
        mock.patch("pytdxdata.api.get_mac_hosts", return_value=["m1"]),
        mock.patch("pytdxdata.pool.pool.TdxConnection",
                   side_effect=lambda h, p=7709, t=10.0: conn_cls(h)),
    )


async def _fetch(conn_cls, **kwargs):
    conn_cls.calls = 0
    p1, p2, p3 = _patched(conn_cls)
    with p1, p2, p3:
        td = TdxData(cache_dir=None, pool_min=1, pool_max=2)
        await td.start()
        try:
            bars = await td.get_bars("sh600000", KlinePeriod.DAY, **kwargs)
        finally:
            await td.close()
    return bars, conn_cls.calls


@pytest.mark.asyncio
async def test_recent_range_uses_backtrack():
    """右端贴最新 → 倒推，往返次数是个位数（不是二分那几十次）。"""
    bars, calls = await _fetch(
        _FakeConn, start_date=NEWEST - datetime.timedelta(days=5), end_date=NEWEST)

    # 返回按时间升序 → 偏移由老到新（5,4,...,0）
    assert [b.datetime.date() for b in bars] == [
        (NEWEST - datetime.timedelta(days=i)).date() for i in range(5, -1, -1)]
    assert calls <= 4, f"倒推路径应 1~3 次往返，实际 {calls}"


@pytest.mark.asyncio
async def test_end_before_newest_within_one_bar_still_backtracks():
    """end 比最新一根略早（K线时间戳是周期结束时刻）仍算贴最新。"""
    end = NEWEST - datetime.timedelta(minutes=30)
    bars, calls = await _fetch(
        _FakeConn, start_date=NEWEST - datetime.timedelta(days=3), end_date=end)

    # offset 0 那根（15:00）晚于 end，被裁掉
    assert bars[-1].datetime.date() == (NEWEST - datetime.timedelta(days=1)).date()
    assert calls <= 4


@pytest.mark.asyncio
async def test_middle_range_falls_back_to_bisect():
    """历史中段倒推够不着左边 → 回落二分，结果仍要正确。"""
    bars, calls = await _fetch(
        _FakeConn,
        start_date=NEWEST - datetime.timedelta(days=100),
        end_date=NEWEST - datetime.timedelta(days=90))

    assert [b.datetime.date() for b in bars] == [
        (NEWEST - datetime.timedelta(days=i)).date() for i in range(100, 89, -1)]
    assert calls > 10, f"中段应走二分（几十次往返），实际只有 {calls}"


@pytest.mark.asyncio
async def test_future_range_returns_empty_quickly():
    # 用相对 now 的未来，别用固定日期：固定日期会随运行日漂进 _BACKTRACK_GRACE
    # （默认 1 天容差覆盖长假），倒推就成立、会多取一轮，断言随日期时绿时红。
    future = datetime.datetime.now() + datetime.timedelta(days=5)  # noqa: DTZ001
    bars, calls = await _fetch(_FakeConn, start_date=future)

    assert bars == []
    assert calls <= 3


@pytest.mark.asyncio
async def test_ascending_channel_skips_backtrack():
    """offset 0 是最老（MAC/EX 语义）时倒推不成立，必须走二分且结果正确。"""
    base = datetime.datetime(2020, 1, 1)  # noqa: DTZ001
    bars, calls = await _fetch(
        _AscendingConn,
        start_date=base + datetime.timedelta(days=99),
        end_date=base + datetime.timedelta(days=109))

    assert [b.datetime.date() for b in bars] == [
        (base + datetime.timedelta(days=i)).date() for i in range(99, 110)]
    assert calls > 10


@pytest.mark.asyncio
async def test_backtrack_matches_plain_count_result():
    """倒推裁剪结果 == 直接取一大段再裁剪（两条路径口径必须一致）。"""
    start = NEWEST - datetime.timedelta(days=7)
    by_date, _ = await _fetch(_FakeConn, start_date=start, end_date=NEWEST)
    whole, _ = await _fetch(_FakeConn, count=60)

    expect = [b for b in whole if b.datetime >= start]
    assert [(b.datetime, b.close) for b in by_date] == \
           [(b.datetime, b.close) for b in expect]
