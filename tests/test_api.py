"""统一接口 TdxData 测试（mock 连接，不连真实网络）+ 分页调度器测试"""
from __future__ import annotations

import asyncio
import datetime
import unittest.mock as mock

import pytest

from pytdxdata.api import TdxData
from pytdxdata.models import (
    KlinePeriod,
    Market,
    MinuteBar,
    SecurityBar,
    TransactionRecord,
)
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


# ---------- Paginator 逆序偏移（服务端 start 从最新往回数） ----------

class NewestFirstConn:
    """模拟 0x122F 真实行为：start=0 返回**最新**的一页，start 越大越旧。

    实测依据（2026-10-21，sh600000）：
      start=0    -> 14:07:48 .. 15:20:16（当天最新 1000 条）
      start=4000 -> 09:15:03 .. 09:41:18（当天最早）
    每页内部按时间升序，页与页之间是倒序。
    """

    def __init__(self, total: int, page_size: int):
        self.total = total
        self.page_size = page_size
        self.executed: list[tuple[int, int]] = []

    async def execute(self, cmd):
        start, count = cmd
        self.executed.append((start, count))
        # 服务端语义：偏移 start 是从"最新"往回数，所以取倒数区间
        end_from_oldest = self.total - start
        begin_from_oldest = max(0, end_from_oldest - count)
        return [_make_bar(i) for i in range(begin_from_oldest, end_from_oldest)]


@pytest.mark.asyncio
async def test_paginator_newest_first_offset_returns_chronological():
    """服务端 start 从最新往回数时，交付列表必须仍是时间升序（BUG 回归）。"""
    conns = [NewestFirstConn(3000, 1000) for _ in range(3)]
    pool = FakePool(conns)
    pag = Paginator(pool, concurrency=3)  # type: ignore[arg-type]

    def factory(s, c):
        return (s, c)

    result = await pag.fetch_all(
        factory, PageSpec(page_size=1000, total=3000, newest_first=True))
    assert len(result) == 3000
    # 时间升序：close 随 index 单调递增
    closes = [b.close for b in result]
    assert closes == sorted(closes), "返回列表必须按时间升序拼接"
    assert result[0].close == pytest.approx(10.0)
    assert result[-1].close == pytest.approx(10.0 + 2999 * 0.01)


@pytest.mark.asyncio
async def test_paginator_newest_first_respects_start_offset():
    """newest_first + start_offset：只取最近 N 条里的后段。"""
    conns = [NewestFirstConn(3000, 1000) for _ in range(3)]
    pool = FakePool(conns)
    pag = Paginator(pool, concurrency=3)  # type: ignore[arg-type]

    def factory(s, c):
        return (s, c)

    result = await pag.fetch_all(
        factory,
        PageSpec(page_size=1000, total=1000, newest_first=True, start_offset=0))
    assert len(result) == 1000
    # 只取了最新的 1000 条 → 是 2000..2999 这一段
    assert result[0].close == pytest.approx(10.0 + 2000 * 0.01)
    assert result[-1].close == pytest.approx(10.0 + 2999 * 0.01)


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

# ---------- A股逐笔：b8 过滤 + 单连接串行游标（回归） ----------

class TickApiConn(FakeApiConn):
    """模拟 0x122F：服务端 start 从**最新**往回数，且可注入 bs_flag=8 委托记录。"""

    # 类级：[(time, price, vol, trade_count, bs_flag), ...]，时间升序（最早在前）
    rows: list[tuple] = []
    calls: list[tuple[int, int]] = []

    async def execute(self, cmd):
        if "MacTransactionCmd" not in type(cmd).__name__:
            return await super().execute(cmd)
        TickApiConn.calls.append((cmd.start, cmd.count))
        # 从最新往回数：start 偏移落在末尾，往前取 count 条
        end = len(TickApiConn.rows) - cmd.start
        begin = max(0, end - cmd.count)
        chunk = TickApiConn.rows[begin:end]
        return [TransactionRecord(market=1, code="600000", time=t, price=p,
                                  vol=v, trade_count=tc, bs_flag=bs)
                for (t, p, v, tc, bs) in chunk]


def _build_tick_rows(n_trade: int = 25, n_b8: int = 7) -> list[tuple]:
    """构造 n_trade 条正常成交 + n_b8 条竞价委托（穿插在中间）。"""
    rows: list[tuple] = []
    for i in range(n_trade):
        rows.append((f"09:{30 + i // 60:02d}:{i % 60:02d}", 10.0 + i * 0.01,
                     100.0 + i, 3 + i, 1 if i % 2 else 0))
        if i % 4 == 0 and len([r for r in rows if r[4] == 8]) < n_b8:
            # 竞价委托：(time, price, vol, tc, bs=8)，vol 是累计值、不计入成交
            rows.append((f"09:15:{i:02d}", 10.0, 9999.0, -1, 8))
    return rows


@pytest.mark.asyncio
async def test_get_ticks_filters_auction_orders():
    """bs_flag=8 是竞价**委托**快照，不是成交 → 必须从 get_ticks 结果中剔除。

    实测（2026-10-21 sh600000）：b8 的 vol 是累计撮合量（可被覆盖），
    计入后 Σvol 比快照 vol 虚高 20%~160%，且不同主机的 b8 集合不同。
    """
    TickApiConn.rows = _build_tick_rows(n_trade=25, n_b8=7)
    TickApiConn.calls = []
    assert sum(1 for r in TickApiConn.rows if r[4] == 8) == 7

    with mock.patch("pytdxdata.api.get_standard_hosts", return_value=["s1"]), \
         mock.patch("pytdxdata.api.get_mac_hosts", return_value=["m1"]), \
         mock.patch("pytdxdata.pool.pool.TdxConnection",
                    side_effect=lambda h, p=7709, t=10.0: TickApiConn(h)):
        td = TdxData(cache_dir=None, pool_min=1, pool_max=2)
        await td.start()
        try:
            out = await td.get_ticks("sh600000")
        finally:
            await td.close()

    assert out, "应返回成交记录"
    assert all(r.bs_flag != 8 for r in out), "bs_flag=8 竞价委托必须被剔除"
    assert len(out) == 25
    assert sum(r.vol for r in out) == sum(100.0 + i for i in range(25))


@pytest.mark.asyncio
async def test_get_ticks_pages_on_single_connection():
    """翻页必须走**同一连接**：并发多主机会让页边界错位（重叠 + 漏条）。"""
    TickApiConn.rows = _build_tick_rows(n_trade=25, n_b8=0)
    TickApiConn.calls = []

    with mock.patch("pytdxdata.api.get_standard_hosts", return_value=["s1"]), \
         mock.patch("pytdxdata.api.get_mac_hosts", return_value=["m1", "m2", "m3"]), \
         mock.patch("pytdxdata.pool.pool.TdxConnection",
                    side_effect=lambda h, p=7709, t=10.0: TickApiConn(h)):
        td = TdxData(cache_dir=None, pool_min=1, pool_max=4)
        await td.start()
        try:
            out = await td.get_ticks("sh600000")
        finally:
            await td.close()

    # 单页装得下 → 只发一次请求
    assert len(TickApiConn.calls) == 1
    assert len(out) == 25
    # 时间升序
    times = [r.time for r in out]
    assert times == sorted(times)

@pytest.mark.asyncio
async def test_get_ticks_multi_page_uses_one_connection_and_no_overlap():
    """多页（>_TICK_PAGE）：同一连接、游标按 len(rows) 推进、页间不重叠不漏条。

    回归：并发分页时各页来自不同主机，而 0x122F 的 start 是「从最新往回数」的
    相对锚点 —— 各主机对 bs_flag=8 收录不同，同一 start 落到不同真实成交上，
    结果页边界既重叠又漏条（实测 sh600000 冗余 116 条、13:03 附近丢 59 条）。
    """
    from pytdxdata.api import _TICK_PAGE

    n = _TICK_PAGE * 2 + 137          # 3 页，末页短页 → 触发 stop_when_short
    TickApiConn.rows = [
        (f"{9 + i // 3600:02d}:{i // 60 % 60:02d}:{i % 60:02d}",
         10.0 + i * 0.001, 1.0 + i, 1 + i, 1)
        for i in range(n)
    ]
    TickApiConn.calls = []

    with mock.patch("pytdxdata.api.get_standard_hosts", return_value=["s1"]), \
         mock.patch("pytdxdata.api.get_mac_hosts", return_value=["m1", "m2", "m3"]), \
         mock.patch("pytdxdata.pool.pool.TdxConnection",
                    side_effect=lambda h, p=7709, t=10.0: TickApiConn(h)):
        td = TdxData(cache_dir=None, pool_min=1, pool_max=4)
        await td.start()
        try:
            out = await td.get_ticks("sh600000")
        finally:
            await td.close()

    # 恰好 3 页：0 / 1000 / 2000，游标按实际返回条数推进
    assert TickApiConn.calls == [(0, _TICK_PAGE),
                                 (_TICK_PAGE, _TICK_PAGE),
                                 (_TICK_PAGE * 2, _TICK_PAGE)]
    assert len(out) == n, f"必须无重叠、无漏条：期望 {n}，实得 {len(out)}"
    vols = [r.vol for r in out]
    assert len(set(vols)) == n, "vol 全唯一（无重复记录）"
    assert vols == sorted(vols), "时间升序"

    # count 参数：游标逐页扣减
    TickApiConn.calls = []
    with mock.patch("pytdxdata.api.get_standard_hosts", return_value=["s1"]), \
         mock.patch("pytdxdata.api.get_mac_hosts", return_value=["m1", "m2", "m3"]), \
         mock.patch("pytdxdata.pool.pool.TdxConnection",
                    side_effect=lambda h, p=7709, t=10.0: TickApiConn(h)):
        td = TdxData(cache_dir=None, pool_min=1, pool_max=4)
        await td.start()
        try:
            out2 = await td.get_ticks("sh600000", count=_TICK_PAGE + 50)
        finally:
            await td.close()
    assert len(out2) == _TICK_PAGE + 50
    assert TickApiConn.calls[1][1] == 50, "末页只补足剩余 50 条"

