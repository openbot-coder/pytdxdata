"""统一接口 TdxData 测试（mock 连接，不连真实网络）+ 分页调度器测试"""
from __future__ import annotations

import asyncio
import datetime
import unittest.mock as mock

import pytest

from pytdxdata.api import TdxData
from pytdxdata.models import KlinePeriod, Market, MinuteBar, SecurityBar
from pytdxdata.protocol.commands.mac_tick import MacTickChartCmd
from pytdxdata.scheduler.paginator import PageSpec, Paginator


def _make_bar(i: int) -> SecurityBar:
    return SecurityBar(market=1, code="600000", open=10.0, high=10.1, low=9.9,
                       close=10.0 + i * 0.01, vol=1000, amount=10000,
                       year=2026, month=8, day=1 + i // 240, hour=9, minute=i % 60)


# ---------- Paginator ----------

class FakePoolConn:
    """模拟连接：返回按start切片的数据。"""
    def __init__(self, total_bars: int, page_size: int, fail_pages: set[int] | None = None):
        self.total = total_bars
        self.page_size = page_size
        self.fail_pages = fail_pages or set()
        self.executed: list[tuple[int, int]] = []

    async def execute(self, cmd):
        start, count = cmd  # cmd 即 (start, count) 元组
        self.executed.append((start, count))
        if start // self.page_size in self.fail_pages:
            raise ConnectionError("mock失败")
        return [_make_bar(i) for i in range(start, min(start + count, self.total))]


class FakePool:
    def __init__(self, conns: list[FakePoolConn]):
        self.conns = conns
        self.released: list = []
        self._acquire_idx = 0

    async def acquire_many(self, n, *, timeout=None):
        return self.conns[:n]

    async def acquire(self, *, timeout=None):
        pc = self.conns[self._acquire_idx % len(self.conns)]
        self._acquire_idx += 1
        return pc

    async def release(self, pc, *, broken=False):
        self.released.append((pc, broken))


@pytest.mark.asyncio
async def test_paginator_parallel_assembly():
    """3连接并行拉2400根（3页×800），按序拼接。"""
    conns = [FakePoolConn(2400, 800) for _ in range(3)]
    pool = FakePool(conns)
    pag = Paginator(pool, concurrency=3)  # type: ignore[arg-type]

    def factory(s, c):
        return (s, c)

    result = await pag.fetch_all(factory, PageSpec(page_size=800, total=2400))
    assert len(result) == 2400
    # 按序：前800根是0-799
    assert result[0].close == 10.0
    assert result[799].close == pytest.approx(10.0 + 799 * 0.01)
    assert result[800].close == pytest.approx(10.0 + 800 * 0.01)
    # 每个连接都用了（并发分页发生）
    assert all(len(c.executed) >= 1 for c in conns)


@pytest.mark.asyncio
async def test_paginator_retry_and_strict():
    """第1页失败→strict=True抛错。"""
    conns = [FakePoolConn(2400, 800, fail_pages={0}) for _ in range(3)]
    pool = FakePool(conns)
    pag = Paginator(pool, concurrency=3, retries=1, strict=True)  # type: ignore[arg-type]

    def factory(s, c):
        return (s, c)

    with pytest.raises(ConnectionError):
        await pag.fetch_all(factory, PageSpec(page_size=800, total=2400))


@pytest.mark.asyncio
async def test_paginator_strict_false():
    conns = [FakePoolConn(2400, 800, fail_pages={0}) for _ in range(3)]
    pool = FakePool(conns)
    pag = Paginator(pool, concurrency=3, retries=0, strict=False)  # type: ignore[arg-type]

    def factory(s, c):
        return (s, c)

    result = await pag.fetch_all(factory, PageSpec(page_size=800, total=2400))
    assert len(result) == 1600  # 第0页留空，1-2页成功


# ---------- TdxData 统一接口 ----------

class FakeApiConn:
    """模拟 execute：按命令类型返回。"""
    def __init__(self, server="s1"):
        self.server = server
        self.host = server
        self.alive_flag = True

    @property
    def alive(self):
        return self.alive_flag

    async def connect(self):
        pass

    async def execute(self, cmd):
        cname = type(cmd).__name__
        if "GetSecurityBarsCmd" in cname:
            return [_make_bar(i) for i in range(cmd.start, cmd.start + cmd.count)]
        if "GetSecurityQuotesCmd" in cname:
            return []
        if "MacTransactionCmd" in cname:
            return []
        if "GetSecurityCountCmd" in cname:
            return 100
        if "GetXdxrInfoCmd" in cname:
            return []
        if "GetFinanceInfoCmd" in cname:
            return None
        return []

    async def close(self):
        self.alive_flag = False


@pytest.mark.asyncio
async def test_tdxdata_get_kline_with_cache():
    """get_kline：第一次回源，第二次缓存命中（不重复请求）。"""
    with mock.patch("pytdxdata.api.get_standard_hosts", return_value=["s1"]), \
         mock.patch("pytdxdata.api.get_mac_hosts", return_value=["m1"]), \
         mock.patch("pytdxdata.pool.pool.TdxConnection",
                    side_effect=lambda h, p=7709, t=10.0: FakeApiConn(h)):
        td = TdxData(cache_dir=None, pool_min=1, pool_max=2)
        await td.start()
        try:
            bars1 = await td.get_kline(1, "600000", KlinePeriod.DAY, count=100)
            bars2 = await td.get_kline(1, "600000", KlinePeriod.DAY, count=100)
            assert len(bars1) == 100
            assert len(bars2) == 100
            assert bars1[0] == bars2[0]
        finally:
            await td.close()


class MinuteApiConn(FakeApiConn):
    """捕获 get_minute 实际下发的命令（类级，跨实例共享）。"""

    last_minute_cmd = None

    async def execute(self, cmd):
        if "MacTickChartCmd" in type(cmd).__name__:
            MinuteApiConn.last_minute_cmd = cmd
            return [MinuteBar(price=9.14, vol=553.0, hour=9, minute=30)]
        return await super().execute(cmd)


@pytest.mark.asyncio
async def test_get_minute_default_uses_history_cmd():
    """date=None 走 MAC 通道 MacTickChartCmd(今天)：0x122D当日分时。"""
    MinuteApiConn.last_minute_cmd = None
    with mock.patch("pytdxdata.api.get_standard_hosts", return_value=["s1"]), \
         mock.patch("pytdxdata.api.get_mac_hosts", return_value=["m1"]), \
         mock.patch("pytdxdata.pool.pool.TdxConnection",
                    side_effect=lambda h, p=7709, t=10.0: MinuteApiConn(h)):
        td = TdxData(cache_dir=None, pool_min=1, pool_max=2, ex_servers=[])
        await td.start()
        try:
            bars = await td.get_minute(0, "000001")
            assert len(bars) == 1
            assert bars[0].price == 9.14
            cmd = MinuteApiConn.last_minute_cmd
            assert isinstance(cmd, MacTickChartCmd)
            today = datetime.date.today()
            assert cmd.date == today.year * 10000 + today.month * 100 + today.day
        finally:
            await td.close()


@pytest.mark.asyncio
async def test_tdxdata_get_security_count():
    with mock.patch("pytdxdata.api.get_standard_hosts", return_value=["s1"]), \
         mock.patch("pytdxdata.api.get_mac_hosts", return_value=["m1"]), \
         mock.patch("pytdxdata.pool.pool.TdxConnection",
                    side_effect=lambda h, p=7709, t=10.0: FakeApiConn(h)):
        td = TdxData(cache_dir=None, pool_min=1, pool_max=2)
        await td.start()
        try:
            n = await td.get_security_count(0)
            assert n == 100
        finally:
            await td.close()


# ---------- Paginator start_offset ----------

@pytest.mark.asyncio
async def test_paginator_start_offset():
    """start_offset 让分页从指定偏移开始，跳过前面的数据。"""
    conns = [FakePoolConn(5000, 800) for _ in range(3)]
    pool = FakePool(conns)
    pag = Paginator(pool, concurrency=3)  # type: ignore[arg-type]

    def factory(s, c):
        return (s, c)

    # 从偏移 4000 开始拉 800 根（total=4800）
    result = await pag.fetch_all(factory, PageSpec(
        page_size=800, total=4800, start_offset=4000))
    assert len(result) == 800
    assert result[0].close == pytest.approx(10.0 + 4000 * 0.01)
    # 验证没有拉取 offset < 4000 的数据
    starts_used = [s for conn in conns for s, _ in conn.executed]
    assert all(s >= 4000 for s in starts_used)


@pytest.mark.asyncio
async def test_paginator_start_offset_default_zero():
    """不传 start_offset 时行为不变（从0开始）。"""
    conns = [FakePoolConn(2400, 800) for _ in range(3)]
    pool = FakePool(conns)
    pag = Paginator(pool, concurrency=3)  # type: ignore[arg-type]

    def factory(s, c):
        return (s, c)

    result = await pag.fetch_all(factory, PageSpec(page_size=800, total=2400))
    assert len(result) == 2400
    assert result[0].close == 10.0  # 从 offset 0 开始


# ---------- get_kline 日期过滤 ----------

class DateBarApiConn(FakeApiConn):
    """支持日期范围探测的模拟连接（按偏移返回带日期的K线）。"""
    # 类级可调：总共多少条、每条的日期序列
    total_bars: int = 500

    async def execute(self, cmd):
        cname = type(cmd).__name__
        if "GetSecurityBarsCmd" in cname:
            start, count = cmd.start, cmd.count
            result = []
            for i in range(start, min(start + count, self.total_bars)):
                # 每条间隔1天：偏移0=2026-01-01, 偏移1=2026-01-02, ...
                dt = datetime.datetime(2026, 1, 1) + datetime.timedelta(days=i)
                result.append(SecurityBar(
                    market=1, code="600000", open=10.0, high=10.1, low=9.9,
                    close=10.0 + i * 0.01, vol=1000, amount=10000,
                    year=dt.year, month=dt.month, day=dt.day,
                    hour=9, minute=30))
            return result
        return await super().execute(cmd)


@pytest.mark.asyncio
async def test_get_kline_start_date_filters():
    """get_kline(start_date=...) 只返回该日期之后的K线。"""
    DateBarApiConn.total_bars = 500
    with mock.patch("pytdxdata.api.get_standard_hosts", return_value=["s1"]), \
         mock.patch("pytdxdata.api.get_mac_hosts", return_value=["m1"]), \
         mock.patch("pytdxdata.pool.pool.TdxConnection",
                    side_effect=lambda h, p=7709, t=10.0: DateBarApiConn(h)):
        td = TdxData(cache_dir=None, pool_min=1, pool_max=2)
        await td.start()
        try:
            bars = await td.get_kline(
                1, "600000", KlinePeriod.DAY,
                start_date=datetime.datetime(2026, 3, 1))
            # 2026-01-01 偏移0 → 2026-03-01 偏移59，所以 >= 偏移59
            assert len(bars) > 0
            assert bars[0].year == 2026 and bars[0].month == 3 and bars[0].day == 1
        finally:
            await td.close()


@pytest.mark.asyncio
async def test_get_kline_date_range():
    """get_kline(start_date=..., end_date=...) 返回日期范围内的K线。"""
    DateBarApiConn.total_bars = 500
    with mock.patch("pytdxdata.api.get_standard_hosts", return_value=["s1"]), \
         mock.patch("pytdxdata.api.get_mac_hosts", return_value=["m1"]), \
         mock.patch("pytdxdata.pool.pool.TdxConnection",
                    side_effect=lambda h, p=7709, t=10.0: DateBarApiConn(h)):
        td = TdxData(cache_dir=None, pool_min=1, pool_max=2)
        await td.start()
        try:
            bars = await td.get_kline(
                1, "600000", KlinePeriod.DAY,
                start_date=datetime.datetime(2026, 2, 1),
                end_date=datetime.datetime(2026, 2, 28))
            assert len(bars) == 28
            assert bars[0].month == 2 and bars[0].day == 1
            assert bars[-1].month == 2 and bars[-1].day == 28
        finally:
            await td.close()


@pytest.mark.asyncio
async def test_get_kline_since():
    """get_kline_since 便捷方法：返回指定日期之后的所有K线。"""
    DateBarApiConn.total_bars = 500
    with mock.patch("pytdxdata.api.get_standard_hosts", return_value=["s1"]), \
         mock.patch("pytdxdata.api.get_mac_hosts", return_value=["m1"]), \
         mock.patch("pytdxdata.pool.pool.TdxConnection",
                    side_effect=lambda h, p=7709, t=10.0: DateBarApiConn(h)):
        td = TdxData(cache_dir=None, pool_min=1, pool_max=2)
        await td.start()
        try:
            bars = await td.get_kline_since(
                1, "600000", KlinePeriod.DAY, datetime.datetime(2026, 6, 1))
            assert len(bars) > 0
            assert bars[0].year == 2026 and bars[0].month == 6 and bars[0].day == 1
            # 最后一条应该是偏移499=2027-04-16
            assert bars[-1].year == 2027
        finally:
            await td.close()
